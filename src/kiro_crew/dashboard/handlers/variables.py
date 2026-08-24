"""``GET``/``PUT /api/variables`` — read the variable cascade, or patch one scope.

``GET`` reports what this endpoint can EDIT — the pairs the variables store holds
for each scope — alongside the resolved map and where each winning value came from.

``PUT`` applies a PER-KEY patch: ``set`` names pairs to write, ``delete`` names keys
to remove. It deliberately does not replace a whole scope. The replace form drew a
data-loss finding in three consecutive review rounds, all from one root: the client
had to echo back a map it had read, so two clients replacing the same scope clobbered
each other's unrelated edits and a value the client could not see got dropped.
Touching only the named keys removes that: a key nobody named is never read, never
rewritten, and cannot be lost.

Deleting is therefore an explicit verb rather than "absence from the map", which also
keeps the empty string unambiguous — it is a legal value that still overrides a
broader scope, so it cannot share an encoding with "unset".

Validation refuses rather than drops. The loader deliberately drops a bad pair with a
warning so one hand-edited mistake cannot cost the rest of a scope or fail a load,
but a dashboard write is interactive: silently discarding a pair the user just typed
would look like a save that worked.

WHAT THIS ENDPOINT NO LONGER HAS TO DO
======================================

Variables used to live in ``config.json``, which has a ``config.local.json`` overlay
and a whole-file writer (``KiroCrewConfig.save()``). That placement, not this
endpoint, generated most of this feature's review history, and moving the data to its
own store deleted four mechanisms outright:

* the ``variables_overlay_owned`` refusal — the store has no overlay layer, so there
  is no class of key this endpoint can read but not write;
* the deleted-workspace resurrection guard — a store entry naming a workspace the
  config does not define is inert (the loader skips it) instead of materializing one;
* the malformed-workspace-entry and merged-vs-base distinction — the store holds only
  variables, so there is no neighbouring config shape to misread;
* ``save()`` interaction entirely — nothing but this endpoint writes the store.

See ``config/variables_store.py`` for why the three in-config alternatives were each
wrong. Malformed-container refusal survives the move and lives in the store, because
"do not replace a value whose shape you cannot interpret" is a property of the data,
not of where it is kept.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.config import variables_store as vstore
from kiro_crew.config.loader import ConfigReadError, KiroCrewConfig, resolve_variables
from kiro_crew.dashboard.chat_utils import run_config_write
from kiro_crew.sel import sel
from kiro_crew.variables import parse_dotenv, validate_pair

logger = logging.getLogger(__name__)

SCOPE_GLOBAL = vstore.SCOPE_GLOBAL
SCOPE_WORKSPACE = vstore.SCOPE_WORKSPACE
_WRITABLE_SCOPES = (SCOPE_GLOBAL, SCOPE_WORKSPACE)


def _view_inputs() -> tuple[KiroCrewConfig, dict]:
    """The merged config and the raw store document.

    Both are needed because they answer different questions: the config says what a
    session RESOLVES and which workspaces and crews exist, the store says what this
    endpoint can EDIT. Blocking; call from a thread.
    """
    return KiroCrewConfig.load(), vstore.read_store()


def _view_blocking() -> dict:
    """The rendered view, inputs included -- the whole blocking span in one thread hop.

    ``_view`` is not merely a projection of ``_view_inputs``: it calls
    ``resolve_variables``, whose per-scope ``_store_layer`` reads the store on a cache
    miss. Offloading only the inputs left that read on the event loop, reachable
    whenever a concurrent PUT invalidates the cache between the two calls -- which on
    THIS endpoint is the ordinary traffic pattern, not a rare interleaving.
    """
    return _view(*_view_inputs())


def _view(cfg: KiroCrewConfig, doc: dict) -> dict:
    """What this endpoint can edit, plus the resolved map for the active context.

    The per-scope maps come from the STORE, not from the resolved cascade. Reporting
    resolved values as editable is what let the panel show a pair it did not own.

    Workspace and crew maps are keyed off the CONFIG's names, not the store's, so a
    stale store entry for a deleted workspace is not advertised as editable. Those
    names are also what the loader resolves against, so the two agree.
    """
    resolution = resolve_variables(cfg)
    ws_stored = vstore.scoped_values(vstore.SCOPE_WORKSPACE, doc)
    crew_stored = vstore.scoped_values(vstore.SCOPE_CREW, doc)
    return {
        "global": vstore.global_values(doc),
        "workspaces": {name: dict(ws_stored.get(name, {})) for name in cfg.workspaces},
        "crews": {name: dict(crew_stored.get(name, {})) for name in cfg.agents},
        # Read-only: this endpoint does not write the dotenv files, so the panel shows
        # them without edit controls. Reported at all because a value the operator
        # cannot see is a value they cannot debug -- a file-defined key that a panel
        # key shadows would otherwise look like the panel edit had no effect.
        "workspace_files": {name: vstore.workspace_env_values(name) for name in cfg.workspaces},
        "workspace_file_dir": str(vstore.workspace_env_dir()),
        # Why a workspace has no file layer, when it cannot have one. Reported so the
        # panel can say it: a workspace silently showing no file rows, with the reason
        # only in a log, is the invisible failure this feature avoids elsewhere.
        "workspace_file_blocked": {
            name: reason
            for name in cfg.workspaces
            if (reason := vstore.workspace_env_block_reason(name))
        },
        "effective": dict(resolution.values),
        "winning_scope": dict(resolution.winning_scope),
        "shadowed": {key: list(scopes) for key, scopes in resolution.shadowed.items()},
        "active_workspace": resolution.workspace_name,
        "active_agent": resolution.agent_name,
    }


async def api_variables(request: web.Request) -> web.Response:
    """GET/PUT /api/variables — read the cascade, or patch one scope."""
    caller = request.get("user", "dashboard")

    # Refused for BOTH verbs, and before the read, because each direction is its own
    # breach: GET discloses every operator variable at every scope, and PUT rewrites
    # what expands into the operator's own next prompt. An app reaching either one is
    # the hazard the fenced store exists to prevent, so an allowlisted path in a
    # manifest must not be a way back in.
    #
    # Rejected here rather than by trusting `request_is_operator` downstream: that
    # predicate decides whether text may EXPAND, which is a different question from
    # whether this caller may read or write the store at all.
    if request.get("app", ""):
        sel().log_api_access(
            caller=f"app:{request.get('app', '')}",
            operation="variables.access",
            outcome="denied",
            resources=request.path,
            error="app tokens may not read or write operator variables",
        )
        return web.json_response(
            {"error": "app tokens may not access variables", "code": "app_forbidden"},
            status=403,
        )

    if request.method != "PUT":
        return web.json_response(await asyncio.to_thread(_view_blocking))

    def _deny(code: str, error: str) -> web.Response:
        """Refuse a malformed request.

        The status is a literal rather than a parameter: the error-code contract gate
        counts a computed ``status=`` separately precisely because hoisting it into a
        variable would defeat the static check, and every refusal here is a 400.
        """
        sel().log_api_access(
            caller=caller,
            operation="variables.update",
            outcome="denied",
            error=error,
        )
        return web.json_response({"error": error, "code": code}, status=400)

    try:
        body = await request.json()
    except Exception:
        return _deny("variables_invalid_json", "invalid JSON")
    if not isinstance(body, dict):
        return _deny("variables_invalid_body", "body must be an object")

    scope = body.get("scope")
    if scope not in _WRITABLE_SCOPES:
        return _deny(
            "variables_invalid_scope",
            f"scope must be one of {', '.join(_WRITABLE_SCOPES)}",
        )

    raw_set = body.get("set")
    raw_delete = body.get("delete")
    raw_bulk = body.get("bulk")
    # Only a bulk write carries a baseline. A per-key `set`/`delete` names its own
    # keys, so it cannot lose an edit it never mentioned and needs no compare.
    expect: dict[str, str] | None = None
    if raw_bulk is not None and (raw_set is not None or raw_delete is not None):
        return _deny(
            "variables_conflicting_change",
            "'bulk' replaces the whole scope, so it cannot be combined with " "'set' or 'delete'",
        )
    if raw_bulk is None and raw_set is None and raw_delete is None:
        return _deny(
            "variables_invalid_values",
            "body must carry 'set' (object), 'delete' (array of names), or " "'bulk' (dotenv text)",
        )
    if raw_bulk is not None and not isinstance(raw_bulk, str):
        return _deny("variables_invalid_values", "bulk must be a string of dotenv text")
    if raw_set is not None and not isinstance(raw_set, dict):
        return _deny("variables_invalid_values", "set must be an object")
    if raw_delete is not None and not isinstance(raw_delete, list):
        return _deny("variables_invalid_values", "delete must be an array of names")

    values: dict[str, str] = {}
    for key, value in (raw_set or {}).items():
        name, outcome = validate_pair(key, value)
        if name is None:
            sel().log_api_access(
                caller=caller,
                operation="variables.update",
                outcome="denied",
                error=f"invalid variable: {outcome}",
            )
            return web.json_response(
                {"error": outcome, "code": "variables_invalid_pair", "key": str(key)},
                status=400,
            )
        values[name] = outcome

    removals: list[str] = []
    for key in raw_delete or []:
        # A delete names a key rather than carrying a value, so it is validated by the
        # same grammar with a throwaway value: an unparseable name could not have been
        # stored by this endpoint in the first place.
        name, outcome = validate_pair(key, "")
        if name is None:
            # Through the audited helper, like every sibling refusal: the set-side
            # rejection above logs one, so a delete that returned 400 with no SEL
            # entry would leave a refusal invisible in the audit trail.
            sel().log_api_access(
                caller=caller,
                operation="variables.update",
                outcome="denied",
                error=f"invalid variable name in delete: {outcome}",
            )
            return web.json_response(
                {"error": outcome, "code": "variables_invalid_pair", "key": str(key)},
                status=400,
            )
        removals.append(name)

    overlapping = sorted(set(values) & set(removals))
    if overlapping:
        return _deny(
            "variables_conflicting_change",
            f"a key cannot be set and deleted in one request: {', '.join(overlapping)}",
        )

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    workspace = body.get("workspace") or ""
    if scope == SCOPE_WORKSPACE:
        if not isinstance(workspace, str) or workspace not in cfg.workspaces:
            # Refused for the caller's sake rather than for data integrity: a store
            # entry for an unknown workspace is inert (the loader skips names the
            # config does not define), so accepting it would report a save that can
            # never take effect.
            return _deny(
                "variables_unknown_workspace",
                f"unknown workspace: {workspace!r}",
            )

    if raw_bulk is not None:
        # STRICT, unlike the same parser reading a file on disk: the operator is
        # looking at this text right now, so the fixable moment is now. A file has
        # nobody watching and keeps its good pairs instead.
        parsed, problems = parse_dotenv(raw_bulk)
        if problems:
            lineno, reason = problems[0]
            sel().log_api_access(
                caller=caller,
                operation="variables.update",
                outcome="denied",
                error=f"bulk line {lineno}: {reason}",
            )
            return web.json_response(
                {
                    "error": f"line {lineno}: {reason}",
                    "code": "variables_invalid_bulk",
                    "line": lineno,
                },
                status=400,
            )
        # The scope as the editor rendered it, VALUES included, compared inside the
        # locked mutation rather than here. A check before the lock is a TOCTOU: two
        # applies can both read the same "current", both agree with their own baseline,
        # and the second still clobbers the first. Comparing on values and not just key
        # presence is the other half — a value another writer changed since this editor
        # rendered would otherwise be reverted by text its operator never saw.
        #
        # A missing baseline is refused rather than defaulted, because a client that
        # omits it would get the destructive path for free.
        raw_base = body.get("base")
        if not isinstance(raw_base, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in raw_base.items()
        ):
            return _deny(
                "variables_invalid_values",
                "bulk must carry 'base' (an object of the name/value pairs the editor "
                "was opened on)",
            )
        expect = dict(raw_base)

        # Still a per-key diff, so deletion stays "absence" with no second verb — but
        # derived from the BASELINE, which the locked mutation proves identical to the
        # stored scope before it applies anything. A pre-lock read would reintroduce
        # exactly the window the `expect` check exists to close.
        values = dict(parsed)
        removals = [name for name in expect if name not in parsed]

    # Routed through run_config_write, which holds BOTH config locks: the loop-side
    # asyncio lock and, inside the worker thread, the store's own advisory flock via
    # update_config_locked. The blocking wait therefore never happens on the event
    # loop — the defect that made the in-config version of this write untenable.
    try:
        await run_config_write(
            vstore.patch_store,
            scope=scope,
            name=workspace,
            values=values,
            removals=removals,
            expect=expect,
        )
    except vstore.StaleBaseline:
        sel().log_api_access(
            caller=caller,
            operation="variables.update",
            outcome="denied",
            error="bulk baseline is stale",
        )
        return web.json_response(
            {
                "error": (
                    "the variables in this scope changed while the bulk editor was "
                    "open. Reopen it to pick up the current values, then re-apply."
                ),
                "code": "variables_stale_bulk_base",
            },
            status=409,
        )
    except vstore.MalformedStore as exc:
        return _deny(
            "variables_malformed_container",
            f"{exc.path} in the variables store is not an object; refusing to "
            "replace it. Repair or remove that value by hand, then retry.",
        )
    except vstore.UntrustedStoreLocation:
        # A security refusal, so it is AUDITED and answered rather than allowed to
        # become a bare 500: an operator who sees "internal error" retries, and this
        # one never succeeds until they remove the link by hand. 409 rather than 500
        # for the same reason -- nothing is broken in the service, the on-disk state
        # conflicts with what the store requires.
        sel().log_api_access(
            caller=caller,
            operation="variables.update",
            outcome="denied",
            error="the variables store directory is a link",
        )
        return web.json_response(
            {
                "error": (
                    "the variables store directory is a symlink and will not be "
                    "written through. Remove the link and retry."
                ),
                "code": "store_untrusted_location",
            },
            status=409,
        )
    except ConfigReadError:
        sel().log_api_access(
            caller=caller,
            operation="variables.update",
            outcome="error",
            error="the variables store is corrupt",
        )
        return web.json_response(
            {"error": "the variables store is corrupt", "code": "config_corrupt"},
            status=500,
        )

    sel().log_api_access(
        caller=caller,
        operation="variables.update",
        outcome="ok",
        resources=f"{scope}:{workspace}" if scope == SCOPE_WORKSPACE else scope,
    )
    return web.json_response({"ok": True, **(await asyncio.to_thread(_view_blocking))})
