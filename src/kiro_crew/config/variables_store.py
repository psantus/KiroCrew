"""Storage for user-defined ``{{name}}`` variables, in their own file.

WHY THIS IS NOT IN ``config.json``
==================================

It was, and that placement was the root cause of most of this feature's review
history. ``KiroCrewConfig.save()`` serializes the MERGED config and replaces the
whole file, and ``to_dict()`` builds an explicit dict, so the file is a lossy
whole-document rewrite of exactly the keys the dataclass models. For a map whose
only legitimate writer is a dedicated endpoint, that produced a genuine trilemma —
every possible behaviour for the variables slot during an unrelated ``save()`` is
wrong in a different way:

* serialize the merged value  -> overwrites a base value the overlay shadowed, and
  the shadowed value is not in the merged view at all, so it is unrecoverable;
* preserve it while holding the config lock -> ``save()`` is a sync method called
  from 13 async call sites, so a contended POSIX flock stalls the event loop;
* preserve it with an unlocked read -> the read-then-write window silently drops a
  variables write that already returned 200 to its caller.

Moving the data out deletes the trilemma rather than choosing among its three
positions. ``save()`` no longer serializes variables at all, so there is nothing to
preserve, no lock to interact with, and no window. It also removes the overlay
subtraction problem, the overlay-owned-key refusal, and the deleted-workspace
resurrection window — all of which existed only because this map lived inside a
document with a second overlay layer and a whole-file writer.

The cost, stated plainly: variables are no longer part of ``config.json``, so they
are not covered by whatever backs that file up, and a hand-edit goes here instead.
There is no migration path because no released version stored them anywhere.

SHAPE
=====

One flat document, one writer, three scopes::

    {
      "global":     {"NAME": "value"},
      "workspaces": {"ops": {"NAME": "value"}},
      "crews":      {"reviewer": {"NAME": "value"}}
    }

Session scope is deliberately absent: it is per-turn state, never persisted.

READ is tolerant, WRITE is strict. An unreadable or malformed store resolves to no
variables rather than raising, because a broken store must not take the gateway down
over an optional feature. A WRITE refuses a malformed container instead of replacing
it, because the operator's hand-written value is the only copy there is.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

SCOPE_GLOBAL = "global"
SCOPE_WORKSPACE = "workspace"
SCOPE_CREW = "crew"

# The scope's key in the stored document. Global is a flat map; the other two are
# maps of name -> map, so they need a container key.
_CONTAINER = {SCOPE_WORKSPACE: "workspaces", SCOPE_CREW: "crews"}

_STORE_DIR = "variables"
_STORE_NAME = "variables.json"

# Per-workspace dotenv files live in a subdirectory of the store dir, so they are
# covered by the SAME `security.py` fence entry -- the agent can neither read nor
# write them. That containment is the whole reason these files are here rather than
# in the workspace working directory, which is the tree the agent actively edits: a
# file the agent can write is a file the agent can use to choose what gets
# substituted into its own next prompt. Fencing one filename inside the working
# directory would not do -- `.env.local` is an ordinary project file, so refusing it
# would break an agent scaffolding any framework that ships one.
_WORKSPACE_ENV_DIR = "workspaces"
_WORKSPACE_ENV_SUFFIX = ".env"

# A workspace name becomes a FILENAME here, so it is constrained rather than
# sanitized: a name that cannot be spelled safely gets no file at all. Rejecting is
# safe because the workspace still resolves -- it simply has no file layer.
#
# LOWERCASE ONLY, and that is the load-bearing part. macOS and Windows are
# case-insensitive by default, so `Ops` and `ops` -- two distinct workspaces as far as
# the config is concerned -- would name one physical file and silently share their
# variables with each other. Refusing the mixed-case spelling is the same rule this
# module already applies to every other unspellable name: never guess a canonical form
# on the operator's behalf, because two distinct workspaces folding onto one file is
# exactly the leak the guess would cause.
_SAFE_WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# mtime-keyed read cache. KiroCrewConfig.load() applies the store on every call and
# load() runs on the event loop, so an uncached read meant a file read plus a JSON
# parse per load -- for a large store, a measurable stall. Keyed on the same signature
# shape config.json's own cache uses (mtime_ns + size + mode), so any edit,
# truncation or replacement busts it, and a missing file has a distinct sentinel so
# create and delete bust it too.
#
# The residual is one stat() per config load on the loop. That is the same class of
# cost load() already pays to read config.json itself, so this adds no new kind of
# blocking work -- but it is not zero, and a caller that needs a guaranteed-fresh read
# without a stat does not exist today.
_cache: tuple[tuple, dict] | None = None

# Same shape, one entry per workspace: name -> (fingerprint, pairs). The dotenv layer
# is read on EVERY resolution, and resolutions happen per turn, per cron dispatch and
# per nudge -- several of them on the event loop. Uncached, that was a file read plus a
# parse each time, which is strictly worse than the store read beside it and is the
# residual `_store_layer` documents made unconditional. Keyed on the same signature, so
# an edit, a truncation or a replacement busts it and a missing file has its own
# sentinel.
_env_cache: dict[str, tuple[tuple, dict[str, str]]] = {}

# A variables file holds pairs whose values are capped at 4096 characters. A file past
# this is not a variables file -- it is a mistake or a wrong path -- and parsing it on
# the event loop is the cost. Refused whole rather than truncated: half a file silently
# resolves half a workspace's variables, and the operator sees tokens survive with no
# reason given.
_MAX_ENV_BYTES = 256 * 1024

# Bumped by every invalidation. A reader captures this BEFORE it reads and refuses to
# publish if it changed, which closes a race the fingerprint alone cannot: a reader
# that started before a write can finish after it, and would otherwise install its
# pre-write document under a signature that now matches the post-write file — so every
# later reader would be served stale values indefinitely, not just once.
_generation = 0


def _fingerprint(path: Path) -> tuple:
    """Cheap signature of the store file; changes whenever it is edited."""
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size, st.st_mode)
    except OSError:
        return (str(path), None)


def invalidate_cache() -> None:
    """Drop the read cache and advance the generation.

    Called by ``patch_store`` after a write rather than relying on the fingerprint
    alone: an atomic replace can land inside the same mtime granularity as the read
    that preceded it, and a stale hit would then serve the pre-write document.

    The generation bump is the second half of that, and it covers the harder case —
    a reader already IN FLIGHT when the write lands. Clearing ``_cache`` does nothing
    about a reader that is about to assign to it.
    """
    global _cache, _generation
    _cache = None
    _env_cache.clear()
    _generation += 1


class UntrustedStoreLocation(Exception):
    """The store directory or file is a link, so its bytes are not ours to trust.

    Separate from :class:`MalformedStore` because the remedy is different and belongs
    to the operator, not to the writer: nothing about the document is wrong, and no
    retry or repair of its contents helps. The link itself has to be removed, by
    someone outside the agent.
    """


class StaleBaseline(Exception):
    """The scope changed between the editor rendering and the write arriving.

    Raised from INSIDE the locked mutation, not from a read before it. Checking
    outside the lock is a TOCTOU: two bulk applies can both read the same "current",
    both agree with their own baseline, and the second still clobbers the first. The
    lock is the only place where "what I saw" and "what I am replacing" are the same
    document.
    """


class MalformedStore(Exception):
    """A container the write would have to replace holds a non-mapping.

    Refused rather than coerced: replacing it would discard whatever the operator
    hand-wrote, and there is no second copy to restore from. Carries the dotted path
    so the caller can tell the operator what to repair.
    """

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


def store_location_is_trusted() -> bool:
    """Is the store reachable without following a link out of the fenced directory?

    The fence in ``security.py`` stops the agent CREATING this link, but it cannot
    un-plant one that predates the fence: on any install that ran before ``variables``
    was fenced, ``variables/`` may already be a symlink into agent-writable space, and
    every read then loads attacker-chosen values straight into an operator's prompt.
    A path-name fence protects a name, not the inode it currently resolves to.

    So the location is verified rather than assumed, on both the read and the write
    path. Checked with ``lstat`` semantics at each level -- resolving first would
    follow exactly the link being looked for -- and the resolved directory must still
    be the one derived from ``config_path()``.

    Answers, never raises: this is called from ``read_store``, whose contract is that a
    variables file can never take down a turn. An untrusted location yields no
    variables, which surfaces as unresolved ``{{tokens}}`` -- this feature's visible
    failure mode everywhere else -- rather than as silently substituted attacker text.
    """
    from kiro_crew.config.loader import config_path

    try:
        base = config_path().parent
        store_dir = base / _STORE_DIR
        # `os.path.lexists`, NOT `Path.exists()`. `exists()` FOLLOWS the link, so a
        # DANGLING one -- planted at a target that does not exist yet -- reported
        # "nothing here", took this early return as trusted, and the writer then
        # created the attacker's target through it. The same trap as resolving first,
        # one predicate over: never ask a link-following question about a path whose
        # being-a-link is the thing under test.
        if not os.path.lexists(store_dir):
            return True  # nothing planted at all; the writer creates it fenced
        if platform_compat.is_link_or_junction(str(store_dir)):
            logger.error(
                "the variables store directory %s is a link; refusing to read through "
                "it. A link here predates the sensitive-path fence and points at "
                "storage the agent can write.",
                store_dir,
            )
            return False
        leaf = store_dir / _STORE_NAME
        # A HARD link is invisible to every check above: there is no link at the path
        # level, `lstat` reports an ordinary regular file, and only the link COUNT
        # differs. It matters most on the write path, which is a read-modify-write:
        # `update_config_locked` reads the shared inode, merges the operator's patch
        # onto whatever the attacker put there, and writes the union -- so the
        # attacker's keys become trusted substitutions carrying the operator's
        # authority. (The atomic replace then severs the link, which is why the leak
        # does not run the other way and why this is easy to miss.)
        #
        # Checked HERE rather than only in the reader because this is the predicate the
        # writer consults; hardening `read_store` alone left the write path reading
        # through a path nobody had validated.
        try:
            if os.lstat(leaf).st_nlink > 1:
                logger.error(
                    "the variables store file %s is hardlinked (st_nlink=%d); refusing "
                    "to read or write through it. Another name for the same bytes means "
                    "the agent can supply what the operator's next write merges onto.",
                    leaf,
                    os.lstat(leaf).st_nlink,
                )
                return False
        except OSError:
            pass  # absent is fine; the writer creates it fenced
        # Unconditional, for the same reason: gating on `leaf.exists()` hid a dangling
        # leaf link exactly as it hid a dangling directory link.
        if platform_compat.is_link_or_junction(str(leaf)):
            logger.error("the variables store file %s is a link; refusing to read it", leaf)
            return False
        if store_dir.resolve() != (base.resolve() / _STORE_DIR):
            logger.error("the variables store directory %s resolves outside the fence", store_dir)
            return False
    except OSError:
        logger.debug("could not verify the variables store location", exc_info=True)
        return False
    return True


def store_path() -> Path:
    """Location of the store, in its OWN directory under the config root.

    A directory rather than a bare file beside ``config.json``, because the fence in
    ``security.py`` protects a path by name and this file is not written alone:
    ``update_config_locked`` creates a predictable ``<path>.lock`` sidecar, and
    ``write_config_atomically`` stages a temp inode in the same directory before
    renaming. A leaf entry covers the target and leaves both of those unfenced, so an
    agent watching the directory could write the staging inode or the lock. Fencing
    the whole directory covers the target, the lock and the temp files together —
    the same reason the ``.vault`` entry is a directory entry.

    Derived from ``config_path()`` rather than hardcoded so a relocated or
    test-redirected config root carries the store with it. Imported lazily because
    this module is a leaf and ``loader`` imports it.
    """
    from kiro_crew.config.loader import config_path

    return config_path().parent / _STORE_DIR / _STORE_NAME


def workspace_env_dir() -> Path:
    """Directory holding the per-workspace dotenv files.

    Inside ``store_path().parent`` so it inherits that directory's fence entry rather
    than needing one of its own -- a second entry is a second thing to keep in step
    with ``security.py``, and the one that gets forgotten is the one that matters.
    """
    return store_path().parent / _WORKSPACE_ENV_DIR


def _fenced_workspaces_dir() -> Path:
    """The one true location of the dotenv files, anchored on the config directory.

    The config directory is resolved and the two names are appended UNRESOLVED. That
    asymmetry is the entire guard: the config directory is the trust root (a link there
    relocates the whole data home and breaks ``security.py``'s fence globally, which is
    a different problem), while everything beneath it is exactly what an attacker would
    relocate. Appending the names without resolving them means the anchor cannot be
    moved by a link inside the store.
    """
    from kiro_crew.config.loader import config_path

    return config_path().parent.resolve() / _STORE_DIR / _WORKSPACE_ENV_DIR


def _is_inside_the_fence(candidate: Path) -> bool:
    """Whether *candidate*'s real bytes live in the fenced directory.

    ONE comparison, on the fully-resolved candidate, against an anchor that nothing
    inside the store can move. A link anywhere in the chain -- ``variables/``,
    ``variables/workspaces/``, or the file itself -- relocates the resolved path out
    from under the anchor and is refused, at any nesting depth, with nothing to
    enumerate and keep in step.

    Two earlier shapes of this check shipped and were both wrong, which is why the
    reasoning is written down rather than the rule alone:

    * ``candidate.resolve().parent == base.resolve()`` resolved BOTH sides, so a
      symlinked ``workspaces`` directory made the two agree and the check passed while
      pointing outside the fence.
    * Walking the chain component-by-component asking "is this a link" worked, but
      re-derived what containment means and had to name every component -- so a nesting
      level added later would silently escape it.

    ``security.py``'s own matchers are deliberately NOT reused here: they answer "should
    the agent be blocked from this path?" and return True if EITHER the resolved or the
    lexical form matches, because over-matching is safe when blocking. This question has
    the opposite polarity -- "are these bytes really mine?" -- where over-matching is
    the bug. ``test_the_fence_actually_covers_this_path`` ties the two together instead.
    """
    # TOTAL, deliberately. Every way this can fail -- a missing component, a permission
    # error, a symlink CYCLE (`Path.resolve` raises `RuntimeError`, not `OSError`), an
    # exotic filesystem -- has the same correct answer: we could not establish that
    # these bytes are inside the fence, so there is no file layer. Narrowing this to
    # the exception types thought of so far is what produced three separate rounds of
    # findings in this one function; the next unlisted type would be the fourth.
    #
    # Consistent with the module's siblings rather than a shortcut: `read_store` and
    # `workspace_env_values` both document "never raises" for the same reason -- a
    # variables file is an optional convenience, and no failure to read one may take
    # down a turn, a config load, or `/api/variables`.
    try:
        return candidate.resolve().parent == _fenced_workspaces_dir()
    except Exception:
        logger.debug("could not establish fence containment for %s", candidate, exc_info=True)
        return False


def workspace_env_path(name: str) -> Path | None:
    """Path of *name*'s dotenv file, or ``None`` when the name cannot be a filename.

    Two independent guards, because either alone has a known bypass. The pattern
    rejects the obvious traversal spellings and anything exotic enough to behave
    differently across filesystems; the containment check then re-derives the resolved
    path and refuses it if it escaped anyway, which catches what the pattern cannot
    see -- a case-folding or Unicode-normalising filesystem, or a symlinked
    ``workspaces`` directory pointing somewhere else entirely.

    ``None`` is not an error: the workspace still resolves from the JSON store and
    simply has no file layer. Refusing to name a file is always safe; guessing a
    sanitized name for the operator is not, because two distinct workspaces could
    sanitize onto one file and silently share their variables.
    """
    if not _SAFE_WORKSPACE_RE.match(name or ""):
        if name and name.lower() != name and _SAFE_WORKSPACE_RE.match(name.lower()):
            # Worth its own message: the operator has a perfectly ordinary workspace
            # name and would otherwise see no file layer with no reason given.
            logger.warning(
                "workspace %r has no variables file: file-backed names must be "
                "lowercase, because a case-insensitive filesystem would fold %r and "
                "%r onto one file and share their variables.",
                name,
                name,
                name.lower(),
            )
        return None
    candidate = workspace_env_dir() / f"{name}{_WORKSPACE_ENV_SUFFIX}"
    if not _is_inside_the_fence(candidate):
        logger.warning(
            "workspace variables file for %r is not inside the fenced %s directory; "
            "ignoring it.",
            name,
            _WORKSPACE_ENV_DIR,
        )
        return None
    return candidate


def workspace_env_block_reason(name: str) -> str:
    """Why *name* can have no dotenv file, or ``""`` when it can.

    Exists so the PANEL can say it. A workspace that silently shows no file rows, with
    the reason only in a gateway log the operator will never open, is the invisible
    failure this feature refuses everywhere else -- it is why an unknown ``{{token}}``
    is left standing rather than blanked.

    Codes, not sentences: the message is the frontend's to translate.
    """
    if workspace_env_path(name) is not None:
        return ""
    if name and name.lower() != name and _SAFE_WORKSPACE_RE.match(name.lower()):
        return "name_not_lowercase"
    return "name_unusable"


def workspace_env_values(name: str) -> dict[str, str]:
    """Pairs from *name*'s dotenv file. Never raises; an unreadable file yields none.

    TOLERANT by design, unlike the bulk-edit endpoint that shares the parser. Nobody
    is watching when this runs, so one malformed line must not blank a whole
    workspace's variables -- the good pairs load and the rest are logged with their
    line numbers. The unresolved ``{{token}}`` that results is this feature's visible
    failure mode, the same reason ``expand`` leaves an unknown name in place.
    """
    path = workspace_env_path(name)
    if path is None:
        return {}

    fingerprint = _fingerprint(path)
    cached = _env_cache.get(name)
    if cached is not None and cached[0] == fingerprint:
        return dict(cached[1])

    # Captured BEFORE the read, for the same reason the store's cache does it: a read
    # in flight when a write lands is pre-write, and publishing it under a signature
    # that now matches the post-write file serves stale values to every later reader.
    generation = _generation
    pairs = _read_env_uncached(path)
    if generation == _generation:
        _env_cache[name] = (fingerprint, pairs)
    return dict(pairs)


def _read_env_uncached(path: Path) -> dict[str, str]:
    """The dotenv read itself, split out so the cache wrapper stays legible.

    Read through ``safe_read_file_bytes_nolink`` rather than ``Path.read_text``, which
    is the repository's existing answer to two problems this file has:

    * **Hard links.** A hard link planted inside the fenced directory shares its inode
      with a file the agent can write. ``lstat`` reports a perfectly ordinary regular
      file -- the symlink checks upstream cannot see it -- and only ``st_nlink > 1``
      distinguishes it. The agent then rewrites ITS path and this read follows the
      shared inode into an operator-authored prompt.
    * **Check-to-use.** ``stat()`` for the size and then ``read_text()`` by name are
      two resolutions of the same name, and the file can be swapped between them. The
      helper opens ``O_NOFOLLOW`` first and ``fstat``s the descriptor, so the inode
      validated is exactly the inode read.

    ``hooks.py`` and ``onboarding_import.py`` already guard their reads this way; this
    one simply did not, which made it the weakest reader of a fenced path in the tree.
    """
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

    try:
        raw = safe_read_file_bytes_nolink(str(path), max_bytes=_MAX_ENV_BYTES)
    except FileTooLargeError:
        # The helper RAISES on oversize; this module never does. Same outcome as every
        # other refusal here -- no variables, and the turn continues.
        logger.warning(
            "workspace variables file %s is over the %d-byte limit; resolving none "
            "from it. Split it or remove what does not belong.",
            path.name,
            _MAX_ENV_BYTES,
        )
        return {}
    except OSError:
        logger.debug("workspace variables file %s is unreadable", path.name, exc_info=True)
        return {}
    if raw is None:
        # One answer for every refusal, matching this module's contract that a
        # variables file can never take down a turn: absent, oversized, hardlinked,
        # non-regular, or unreadable all resolve to no variables, which surfaces as
        # unresolved ``{{tokens}}``. The helper logs the specific cause.
        if path.exists():
            logger.warning(
                "workspace variables file %s was refused (hardlinked, non-regular, or "
                "unreadable); resolving none from it.",
                path.name,
            )
        return {}

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning(
            "workspace variables file %s is not valid UTF-8; resolving none from it.",
            path.name,
        )
        return {}

    from kiro_crew.variables import parse_dotenv

    pairs, problems = parse_dotenv(text)
    for lineno, reason in problems:
        logger.warning("%s line %d: %s", path.name, lineno, reason)
    return pairs


def read_store() -> dict[str, Any]:
    """Read the raw store document. Never raises.

    Every failure resolves to an empty document, which resolves to no variables. A
    malformed store must not break a gateway boot over an optional feature; the
    write path is where a malformed value is reported, because that is where it can
    be acted on and where silence would destroy data.
    Cached on the file's signature (see ``_fingerprint``): this runs on every config
    load, and those run on the event loop, so an uncached read meant a file read plus
    a JSON parse per load. Returns a deep copy so a caller cannot mutate the cached
    document — ``_apply_variables_store`` hands these maps to config objects, and an
    alias would let one session's edit leak into every later reader.
    """
    global _cache
    # Before the fingerprint, so a planted link is refused rather than cached.
    if not store_location_is_trusted():
        return {}
    path = store_path()
    fingerprint = _fingerprint(path)
    if _cache is not None and _cache[0] == fingerprint:
        return copy.deepcopy(_cache[1])
    # Captured BEFORE the read. If a write invalidates while this read is in flight,
    # the document in hand is pre-write and must not be published — otherwise it lands
    # under a signature matching the post-write file and every later reader is served
    # stale values. Dropping the publish costs one re-read; publishing costs
    # correctness until the next write.
    generation = _generation
    doc = _read_uncached(path)
    if generation == _generation:
        _cache = (fingerprint, doc)
    return copy.deepcopy(doc)


def _read_uncached(path: Path) -> dict[str, Any]:
    """The read itself, split out so the cache wrapper stays legible.

    Read through ``safe_read_file_bytes_nolink`` for the same two reasons the dotenv
    reader is: a **hard link** planted here shares its inode with a file the agent can
    write and is invisible to every path-level check (``lstat`` sees an ordinary
    regular file; only ``st_nlink > 1`` differs), and opening by name after stating by
    name leaves a check-to-use window. The helper opens ``O_NOFOLLOW`` and ``fstat``s
    the descriptor, so the inode validated is the inode read.

    Both readers of this fenced directory now go through it. Hardening one and not the
    other is how the weaker path becomes the one that gets used.
    """
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

    try:
        data = safe_read_file_bytes_nolink(str(path))
    except (FileTooLargeError, OSError):
        logger.warning(
            "variables store at %s could not be read safely; resolving no variables.",
            path.name,
        )
        return {}
    if data is None:
        # Absent is the ordinary case -- no variables configured yet -- and must stay
        # silent or every fresh install warns on every config load. Anything else means
        # the bytes ARE there and we refused them, which the operator has to hear: their
        # variables just stopped resolving and nothing else would tell them why.
        #
        # `lexists`, not `exists`: a dangling link is present-and-refused, not absent.
        if os.path.lexists(path):
            logger.warning(
                "variables store at %s is unreadable (hardlinked, non-regular, or "
                "permission-denied); resolving no variables. Repair or remove that "
                "file to restore them.",
                path.name,
            )
        return {}

    try:
        raw = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning(
            "variables store at %s is unreadable (%s); resolving no variables. "
            "Repair or remove that file to restore them.",
            path.name,
            exc.__class__.__name__,
        )
        return {}
    if not isinstance(raw, dict):
        logger.warning(
            "variables store at %s is not a JSON object; resolving no variables.",
            path.name,
        )
        return {}
    return raw


def _clean_pairs(raw: object, where: str) -> dict[str, str]:
    """Coerce one scope's map to validated str->str pairs.

    Delegates to the loader's ``coerce_variables`` so validation lives in exactly one
    place — the same name grammar and value-length cap the write path enforces, and
    the same drop-one-pair-not-the-scope tolerance. Imported lazily: the loader
    imports this module, so a module-level import here would close a cycle.
    """
    from kiro_crew.config.loader import coerce_variables

    return coerce_variables(raw, where)


def global_values(doc: dict[str, Any] | None = None) -> dict[str, str]:
    """Global-scope pairs."""
    doc = read_store() if doc is None else doc
    return _clean_pairs(doc.get(SCOPE_GLOBAL), SCOPE_GLOBAL)


def scoped_values(scope: str, doc: dict[str, Any] | None = None) -> dict[str, dict[str, str]]:
    """All named maps for ``workspace`` or ``crew`` scope."""
    container = _CONTAINER[scope]
    doc = read_store() if doc is None else doc
    raw = doc.get(container)
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning("variables store: %s is not an object; ignoring it", container)
        return {}
    return {
        name: _clean_pairs(pairs, f"{container}.{name}")
        for name, pairs in raw.items()
        if isinstance(name, str)
    }


def _mutate(
    doc: dict[str, Any],
    *,
    scope: str,
    name: str,
    values: dict[str, str],
    removals: list[str],
    expect: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Apply a per-KEY patch to the document read under the lock.

    Named keys only: a key nobody mentioned is never read and never rewritten, so
    two concurrent writers touching different keys cannot lose each other's edits,
    and there is no whole-scope echo to go stale.

    A container that is ABSENT is created — that is the legitimate first write. A
    container that is PRESENT but not a mapping is refused, because replacing it
    would destroy the only copy of what the operator wrote.

    "Absent" means the key is MISSING, tested by membership. ``.get()`` returning None
    conflates a missing key with an explicit ``null``, and a hand-edited
    ``{"global": null}`` is present operator data — treating it as absent overwrote
    it, which is exactly what the malformed refusal exists to prevent. All three
    container levels use membership, for the same reason.
    """
    if scope == SCOPE_GLOBAL:
        if SCOPE_GLOBAL not in doc:
            target: dict = {}
            doc[SCOPE_GLOBAL] = target
        elif isinstance(doc[SCOPE_GLOBAL], dict):
            target = doc[SCOPE_GLOBAL]
        else:
            raise MalformedStore(SCOPE_GLOBAL)
    else:
        container = _CONTAINER[scope]
        if container not in doc:
            holder: dict = {}
            doc[container] = holder
        elif isinstance(doc[container], dict):
            holder = doc[container]
        else:
            raise MalformedStore(container)
        if name not in holder:
            target = {}
            holder[name] = target
        elif isinstance(holder[name], dict):
            target = holder[name]
        else:
            raise MalformedStore(f"{container}.{name}")

    if expect is not None and dict(target) != expect:
        # Compared on VALUES, not just key presence: a bulk apply replaces the whole
        # scope, so a value another writer changed since this editor rendered would be
        # reverted by text its operator never saw -- a lost update, not the deliberate
        # overwrite bulk edit is for.
        raise StaleBaseline(f"{scope}:{name}" if name else scope)

    for key, value in values.items():
        target[key] = value
    for key in removals:
        target.pop(key, None)
    return doc


def patch_store(
    *,
    scope: str,
    name: str = "",
    values: dict[str, str] | None = None,
    removals: list[str] | None = None,
    expect: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Apply a per-key patch under the store's own lock. Blocking; call off-loop.

    Routed through ``update_config_locked`` so the read and the write are one
    transaction against the store's advisory lock. That helper is reused rather than
    re-implemented so this file inherits its atomic replace, its mode preservation,
    and its symlink handling.

    This is the ONLY writer. ``KiroCrewConfig.save()`` does not touch this file,
    which is the entire point of the file existing.
    """
    if scope not in (SCOPE_GLOBAL, SCOPE_WORKSPACE, SCOPE_CREW):
        raise ValueError(f"unknown variables scope: {scope!r}")
    if scope != SCOPE_GLOBAL and not name:
        raise ValueError(f"{scope} scope requires a name")

    from kiro_crew.config.loader import update_config_locked

    vals = dict(values or {})
    dels = list(removals or [])

    def _apply(current: dict) -> dict:
        return _mutate(current, scope=scope, name=name, values=vals, removals=dels, expect=expect)

    # The directory is created here, not at import: it must exist before
    # update_config_locked places its lock sidecar, and creating it on a read path
    # would make a plain resolution write to disk. 0o700 so the fenced directory is
    # not world-listable either.
    if not store_location_is_trusted():
        raise UntrustedStoreLocation(
            "the variables store is not at a location we can trust: its directory or "
            "file is a link, or the file has another name pointing at the same bytes. "
            "It will not be written through. The specific cause is logged."
        )
    path = store_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        logger.debug("could not create the variables store directory", exc_info=True)

    # on_corrupt="fail": a corrupt store must NOT be reset to {} by a write, which
    # would delete every variable at every scope to service one patch. read_store()
    # is the tolerant path; this one refuses and the caller reports it.
    #
    # stamp_meta=False: this is not a config document and must not grow config's
    # bookkeeping keys — the shape here is exactly the three scope containers.
    result = update_config_locked(store_path(), mutate=_apply, stamp_meta=False, on_corrupt="fail")
    # Before anything else: a reader arriving after this write must not take a cache
    # hit on the pre-write document.
    invalidate_cache()
    try:
        os.chmod(store_path(), 0o600)
    except OSError:
        # Mode is defence in depth, not the security boundary — values are declared
        # non-secret. A filesystem that refuses chmod must not fail the write.
        logger.debug("could not tighten mode on the variables store", exc_info=True)
    return result if isinstance(result, dict) else {}
