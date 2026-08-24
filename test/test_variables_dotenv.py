"""The dotenv grammar, and the per-workspace file layer built on it.

Two halves, because the parser and the layer fail in different ways:

* The grammar itself -- what counts as a line, how a value is unquoted, and the fact
  that every pair still goes through ``validate_pair``. A dotenv file must not become
  a way in for a value the API would refuse.
* The layer -- where the file lives, that a name which cannot be a filename gets no
  file rather than a guessed one, and that a malformed line costs its own line and
  not the whole workspace.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.config import loader as loader_mod
from kiro_crew.config import variables_store as vstore
from kiro_crew.config.loader import (
    SCOPE_GLOBAL,
    SCOPE_WORKSPACE,
    SCOPE_WORKSPACE_FILE,
    VARIABLE_SCOPES,
    KiroCrewAgentConfig,
    KiroCrewConfig,
    WorkspaceConfig,
    resolve_variables,
)
from kiro_crew.platform_compat import symlink_or_junction
from kiro_crew.variables import (
    _FORBIDDEN_CHARS,
    _TRIMMABLE,
    MAX_VALUE_LEN,
    parse_dotenv,
    render_dotenv,
    validate_pair,
)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Redirect the config path so the store and its workspaces dir land in tmp_path.

    Redirects ``config_path`` and NOT the derived ``store_path``: the store's location
    is derived from the config directory, and patching the derived path would still
    pass if it were hardcoded somewhere else -- which is the one thing the derivation
    exists to prevent.
    """
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(loader_mod, "config_path", lambda: cfg_file)
    vstore.invalidate_cache()
    yield tmp_path
    vstore.invalidate_cache()


def _write_env(root: Path, workspace: str, text: str) -> Path:
    path = root / "variables" / "workspaces" / f"{workspace}.env"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestGrammar:
    def test_a_plain_pair(self):
        assert parse_dotenv("API_URL=https://x.test") == ({"API_URL": "https://x.test"}, [])

    def test_export_prefix_is_accepted(self):
        """Real .env files carry it, pasted from a shell profile."""
        assert parse_dotenv("export API_URL=https://x.test")[0] == {"API_URL": "https://x.test"}

    def test_blank_lines_and_comments_are_skipped(self):
        pairs, problems = parse_dotenv("# heading\n\n   \nA=1\n")
        assert pairs == {"A": "1"}
        assert problems == []

    def test_a_hash_only_starts_a_comment_at_the_start_of_a_line(self):
        """Mid-line it is an ordinary character. Treating it as a comment would
        silently truncate a URL fragment or a colour literal."""
        assert parse_dotenv("A=abc#123")[0] == {"A": "abc#123"}

    @pytest.mark.parametrize("quote", ["'", '"'])
    def test_one_matching_pair_of_surrounding_quotes_is_stripped(self, quote: str):
        assert parse_dotenv(f"A={quote}on call{quote}")[0] == {"A": "on call"}

    def test_an_interior_quote_is_literal(self):
        """Only a MATCHING surrounding pair is stripped, which is what lets
        `render_dotenv` skip escaping entirely."""
        assert parse_dotenv('A=say "hi"')[0] == {"A": 'say "hi"'}

    def test_mismatched_quotes_are_not_stripped(self):
        assert parse_dotenv("A='hi\"")[0] == {"A": "'hi\""}

    def test_whitespace_around_the_name_and_bare_value_is_trimmed(self):
        assert parse_dotenv("  A  =  spaced  ")[0] == {"A": "spaced"}

    def test_quotes_preserve_the_whitespace_trimming_would_remove(self):
        assert parse_dotenv('A="  padded  "')[0] == {"A": "  padded  "}

    def test_an_empty_value_is_legal(self):
        """An empty string is a deliberate override to empty, not a missing value --
        the same rule the cascade uses for key presence."""
        assert parse_dotenv("A=")[0] == {"A": ""}

    def test_an_escape_sequence_is_not_interpreted(self):
        r"""``\n`` stays two characters. Interpreting it would produce a newline,
        which ``validate_pair`` forbids -- turning a legal line into a rejected one."""
        assert parse_dotenv(r"A=one\ntwo")[0] == {"A": r"one\ntwo"}

    def test_a_value_containing_an_equals_sign_keeps_it(self):
        """The split is on the FIRST `=`; a base64 or query-string value has more."""
        assert parse_dotenv("A=a=b=c")[0] == {"A": "a=b=c"}


class TestProblemsAreReportedNotRaised:
    def test_a_line_without_an_equals_is_reported_with_its_number(self):
        pairs, problems = parse_dotenv("A=1\nnonsense\nB=2\n")
        assert pairs == {"A": "1", "B": "2"}, "the good lines still parse"
        assert problems == [(2, "expected NAME=value")]

    def test_a_duplicate_takes_the_last_value_and_is_still_reported(self):
        """Last-wins matches every other dotenv reader, but silently dropping one of
        two lines the operator wrote is the failure they would not notice."""
        pairs, problems = parse_dotenv("A=first\nA=second\n")
        assert pairs == {"A": "second"}
        assert len(problems) == 1 and problems[0][0] == 2
        assert "duplicate" in problems[0][1]

    def test_line_numbers_are_one_based_and_count_skipped_lines(self):
        _, problems = parse_dotenv("# c\n\nA=1\nbad\n")
        assert problems == [(4, "expected NAME=value")]


class TestTheFileObeysTheSameRulesAsThePanel:
    """A dotenv file must not be a way in for a value the API would refuse."""

    def test_a_name_that_is_not_an_identifier_is_refused(self):
        _, problems = parse_dotenv("1abc=x")
        assert problems and "name must start with a letter" in problems[0][1]

    def test_a_reserved_token_name_is_refused(self):
        _, problems = parse_dotenv("STOP_FILE=/tmp/x")
        assert problems and "reserved" in problems[0][1]

    def test_an_oversized_value_is_refused(self):
        _, problems = parse_dotenv("A=" + "x" * (MAX_VALUE_LEN + 1))
        assert problems and str(MAX_VALUE_LEN) in problems[0][1]

    def test_a_value_carrying_the_opening_delimiter_is_refused(self):
        """The rule that makes expansion idempotent across two boundaries, not merely
        single-pass within one."""
        _, problems = parse_dotenv("A={{other}}")
        assert problems and "{{" in problems[0][1]


class TestUnicodeLineSeparatorsCannotInjectARecord:
    """A value carrying U+2028 was a bulk-round-trip injection, not just corruption.

    `str.splitlines()` splits on U+2028/U+2029/NEL and the C1 separators, so a stored
    value like ``prefix\u2028INJECTED=second`` rendered onto ONE physical line came
    back as TWO records: the original truncated, and `INJECTED` persisted as a variable
    nobody wrote.
    """

    @pytest.mark.parametrize(
        "ch", ["\u2028", "\u2029", "\u0085", "\u000b", "\u000c", "\u001c", "\u001d", "\u001e"]
    )
    def test_the_validator_refuses_every_separator_splitlines_honours(self, ch: str):
        """Refused for the same stated reason as `\n`: a value spanning lines can forge
        a context header in the assembled prompt."""
        assert validate_pair("K", f"a{ch}b")[0] is None

    @pytest.mark.parametrize("ch", ["\u2028", "\u2029", "\u0085"])
    def test_the_parser_does_not_split_on_them_either(self, ch: str):
        """Defence in depth: the grammar must not depend on the validator having
        thought of every separator."""
        pairs, _ = parse_dotenv(f"K=prefix{ch}INJECTED=second")
        assert "INJECTED" not in pairs, "a separator created a record nobody wrote"

    def test_crlf_and_lf_still_separate_records(self):
        """The positive control: narrowing the split must not stop it splitting."""
        assert list(parse_dotenv("A=1\r\nB=2\nC=3")[0]) == ["A", "B", "C"]

    def test_a_lone_cr_still_separates(self):
        assert list(parse_dotenv("A=1\rB=2")[0]) == ["A", "B"]


class TestRoundTrip:
    """``parse_dotenv(render_dotenv(x)) == x`` for every value the API will store.

    Driven by hypothesis rather than a hand-picked list, because a hand-picked list is
    what let the quote bug through: the original case here used an INTERIOR quote
    (``say "hi"``), which round-trips fine, and never tried a value that is itself a
    matching pair. ``"hello"`` rendered bare, and the re-parse stripped the operator's
    own quotes -- silent shortening on a save path with no undo.
    """

    @given(
        st.text(
            # The alphabet is aimed at the parser's seams -- quotes, the delimiter, the
            # comment marker, whitespace -- rather than at arbitrary Unicode, which
            # would spend the budget on characters the grammar treats identically.
            alphabet=st.sampled_from(list("ab \t'\"=#\\") + ["é", "日"]),
            min_size=0,
            max_size=12,
        )
    )
    @settings(max_examples=400, deadline=None)
    def test_any_storable_value_survives_the_round_trip(self, value: str):
        # Only values the API would actually store: `validate_pair` is the gate, so a
        # value it refuses is not a round-trip obligation.
        if validate_pair("K", value)[0] is None:
            return
        assert parse_dotenv(render_dotenv({"K": value}))[0] == {"K": value}

    @pytest.mark.parametrize(
        "value",
        [
            '"hello"',  # a matching double pair -- the reported bug
            "'hello'",  # and the single-quote spelling
            '""',  # two quotes and nothing between them
            "''",
            '"',  # one quote: too short to be a pair, must stay bare
            "'",
            "\"mismatched'",  # ends differ, so nothing is stripped either way
            'say "hi"',  # interior only -- the case the original test used
            "  pad  ",
            "",
        ],
    )
    def test_the_quote_shapes_by_name(self, value: str):
        """The regression cases spelled out, so a failure names the shape."""
        assert parse_dotenv(render_dotenv({"K": value}))[0] == {"K": value}

    def test_render_then_parse_returns_the_same_pairs(self):
        pairs = {"B": "two", "A": "one", "SPACED": "  pad  ", "EMPTY": "", "Q": 'say "hi"'}
        assert parse_dotenv(render_dotenv(pairs))[0] == pairs

    def test_render_sorts_by_name(self):
        assert render_dotenv({"B": "2", "A": "1"}) == "A=1\nB=2"

    def test_only_values_that_need_quoting_get_it(self):
        """A wall of unnecessary quotes is what makes a generated dotenv file look
        machine-owned and discourages hand-editing."""
        out = render_dotenv({"PLAIN": "value", "EMPTY": "", "PAD": " x "})
        assert "PLAIN=value" in out
        assert 'EMPTY=""' in out
        assert 'PAD=" x "' in out


class TestAForbiddenCharCannotBeTrimmedAwayInsteadOfRefused:
    """A refusal must not be reachable around by deleting the evidence.

    `_FORBIDDEN_CHARS` exists so a separator cannot inject a second assignment. But
    the cleaners that run BEFORE validation used a bare `str.strip()`, which removes
    everything `str.isspace()` covers -- and that set OVERLAPS the forbidden set. So a
    value ending in one was trimmed to look clean, passed validation, and persisted
    TRUNCATED with a 200: `A=prefix<LS>` stored as `prefix`, on a save path with no
    undo. Silent corruption, not a rejected write.
    """

    # Every forbidden char a bare `.strip()` would eat, by codepoint so the test file
    # stays ASCII -- a literal one here is invisible in review and in a diff.
    EATEN = [0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x1F, 0x85, 0x2028, 0x2029]

    @pytest.mark.parametrize("cp", EATEN)
    @pytest.mark.parametrize("where", ["trailing", "leading"])
    def test_it_is_refused_rather_than_silently_trimmed(self, cp: int, where: str):
        ch = chr(cp)
        value = "prefix" + ch if where == "trailing" else ch + "prefix"
        pairs, problems = parse_dotenv(f"A={value}")

        assert problems, f"U+{cp:04X} {where} was accepted; the write is silently corrupt"
        assert pairs == {}, f"U+{cp:04X} {where} produced a TRUNCATED pair: {pairs!r}"

    @pytest.mark.parametrize("cp", EATEN)
    def test_it_is_refused_in_the_key_too(self, cp: int):
        """The value is not the only place one can hide -- the key was trimmed by its
        own bare `.strip()` on the same line."""
        pairs, problems = parse_dotenv(f"{chr(cp)}A=v")
        assert problems and pairs == {}

    def test_the_trimmable_set_cannot_overlap_the_forbidden_set(self):
        """The invariant behind all of the above, stated once.

        This is what makes the coupling explicit: the two sets are complements by
        intent, and nothing else in the module says so. Widening `_TRIMMABLE` back to
        `string.whitespace` -- or reaching for a bare `.strip()` -- reopens every case
        above at once, so it is asserted directly rather than only through behaviour.
        """
        overlap = set(_TRIMMABLE) & _FORBIDDEN_CHARS
        assert not overlap, (
            "these characters are both trimmable and forbidden, so trimming deletes "
            f"the evidence the validator is looking for: {sorted(map(ord, overlap))}"
        )

    def test_a_legal_value_still_gets_its_edges_trimmed(self):
        """Not an everything-is-refused pass: ordinary padding around a value is still
        cleaned, which is why the bare `.strip()` looked correct in the first place."""
        pairs, problems = parse_dotenv("A=  \tspaced\t  ")
        assert problems == []
        assert pairs == {"A": "spaced"}


class TestWorkspaceFilePath:
    def test_the_file_sits_under_the_fenced_store_directory(self, wired):
        """Inside `store_path().parent` so it inherits that directory's `security.py`
        fence -- the containment that is the whole reason these files are not in the
        workspace working directory the agent edits."""
        path = vstore.workspace_env_path("ops")
        assert path is not None
        assert path.parent.parent == vstore.store_path().parent
        assert path.name == "ops.env"

    @pytest.mark.parametrize(
        "name",
        [
            "../escape",
            "..",
            "",
            "a/b",
            "a\\b",
            ".hidden",
            "x" * 65,
            "wörk",
            # Mixed case: macOS and Windows fold these onto one file, so two distinct
            # workspaces would share their variables.
            "Ops",
            "OPS",
            "myWorkspace",
        ],
    )
    def test_a_name_that_cannot_be_a_filename_gets_no_file(self, wired, name: str):
        """Refusing is safe -- the workspace still resolves from the JSON store.
        Guessing a sanitized name is not: two workspaces could sanitize onto one file
        and silently share their variables."""
        assert vstore.workspace_env_path(name) is None

    def test_a_refused_name_resolves_no_file_values(self, wired):
        assert vstore.workspace_env_values("../escape") == {}


class TestTheFenceIsCheckedByLinkNotByResolvedPath:
    """A symlinked container must not relocate the file layer outside the fence.

    ``security.py`` fences a path by NAME. A symlinked ``variables/`` or
    ``variables/workspaces/`` leaves the name fenced while the bytes live wherever the
    link points -- and if that is the agent's own workspace, the agent authors what
    gets substituted into the next prompt.
    """

    def _link_dir(self, root: Path, link_at: Path) -> Path:
        """Link a directory into place, the way the rest of the codebase does.

        ``symlink_or_junction`` rather than ``Path.symlink_to``: on Windows a symlink
        needs a privilege the ordinary CI user does not hold, so this test would fail
        with ``WinError 1314`` on the platform where the guard matters MOST -- a
        directory junction is not a symlink, which is exactly why the guard uses
        ``is_link_or_junction`` rather than ``is_symlink``.
        """
        target = root / "agent-writable"
        target.mkdir(exist_ok=True)
        link_at.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        symlink_or_junction(target, link_at)
        return target

    def test_a_symlinked_workspaces_directory_is_refused(self, wired):
        target = self._link_dir(wired, wired / "variables" / "workspaces")
        (target / "ops.env").write_text("INJECTED=agent-authored\n", encoding="utf-8")
        vstore.invalidate_cache()

        assert vstore.workspace_env_path("ops") is None
        assert vstore.workspace_env_values("ops") == {}

    def test_a_symlinked_store_root_is_refused(self, wired):
        """The link one level up relocates everything beneath it just as well."""
        target = self._link_dir(wired, wired / "variables")
        (target / "workspaces").mkdir(exist_ok=True)
        (target / "workspaces" / "ops.env").write_text("INJECTED=x\n", encoding="utf-8")
        vstore.invalidate_cache()

        assert vstore.workspace_env_path("ops") is None

    def test_a_symlinked_file_is_refused(self, wired):
        outside = wired / "outside.env"
        outside.write_text("INJECTED=x\n", encoding="utf-8")
        link = wired / "variables" / "workspaces" / "ops.env"
        link.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        symlink_or_junction(outside, link)
        vstore.invalidate_cache()

        assert vstore.workspace_env_path("ops") is None

    def test_comparing_RESOLVED_paths_would_not_have_caught_it(self, wired):
        """Pins why the guard is shaped this way rather than as a containment compare.

        The shipped version was ``candidate.resolve().parent == base.resolve()``.
        Resolving both sides follows the SAME link, so they agree and the check passes
        while pointing outside the fence — this asserts that equality still holds, so
        a future 'simplification' back to it cannot look correct.
        """
        target = self._link_dir(wired, wired / "variables" / "workspaces")
        (target / "ops.env").write_text("x=1\n", encoding="utf-8")
        base = vstore.workspace_env_dir()
        candidate = base / "ops.env"

        assert candidate.resolve().parent == base.resolve(), (
            "the resolved-vs-resolved compare no longer agrees; if the platform "
            "changed, re-point this guard rather than deleting it"
        )
        assert vstore.workspace_env_path("ops") is None, "but the shipped guard refuses"

    def test_an_ordinary_directory_is_still_accepted(self, wired):
        """Not an everything-is-refused pass."""
        _write_env(wired, "ops", "A=1\n")
        assert vstore.workspace_env_path("ops") is not None
        assert vstore.workspace_env_values("ops") == {"A": "1"}


class TestTheFenceActuallyCoversThisPath:
    """The coupling nothing else checks, and the reason this module can stop
    re-deriving containment.

    ``security.py`` spells the fenced directory as the literal ``"variables"`` in
    ``_SENSITIVE_HOME_DIRS``; this module spells it as ``_STORE_DIR``. They are the
    same directory and there is no shared symbol, so a rename on either side would
    silently unfence the dotenv files -- leaving them agent-writable, which turns the
    variable cascade into a channel the agent authors. The two guards this module
    already shipped were both about keeping the file INSIDE that fence; this asserts
    the fence is actually there to be inside of.
    """

    def test_the_dotenv_path_is_fenced_from_the_agent(self, monkeypatch, tmp_path):
        """Asserted through the real matchers, against a real ``$HOME`` layout, because
        the fence is ``$HOME``-relative and a tmp_path config dir is not under it."""
        from kiro_crew import security

        home = tmp_path / "home"
        crew = home / ".kiro" / "crew"
        crew.mkdir(parents=True)
        (crew / "config.json").write_text("{}", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(loader_mod, "config_path", lambda: crew / "config.json")
        vstore.invalidate_cache()

        path = vstore.workspace_env_path("ops")
        assert path is not None

        assert security.is_sensitive_path(str(path)), (
            "the dotenv file is NOT read-fenced; an agent can read every workspace "
            "variable. Check that security._SENSITIVE_HOME_DIRS still names the "
            f"directory variables_store spells as _STORE_DIR ({vstore._STORE_DIR!r})"
        )
        assert security.is_sensitive_write_path(str(path)), (
            "the dotenv file is NOT write-fenced; an agent can author what gets "
            "substituted into its own next prompt"
        )

    def test_the_directory_itself_is_fenced(self, monkeypatch, tmp_path):
        """The lock sidecar and the atomic-replace temp inode live beside the file, so
        the DIRECTORY has to be covered, not just the leaf."""
        from kiro_crew import security

        home = tmp_path / "home"
        crew = home / ".kiro" / "crew"
        crew.mkdir(parents=True)
        (crew / "config.json").write_text("{}", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(loader_mod, "config_path", lambda: crew / "config.json")

        assert security.is_sensitive_path(str(vstore.workspace_env_dir()))
        assert security.is_sensitive_path(str(vstore.workspace_env_dir() / "anything.env"))


class TestEstablishingContainmentIsTotal:
    """The containment guard answers, it never raises.

    Three separate rounds of findings landed in this one function, each a failure
    mode the previous guard had not enumerated -- the last of them a symlink CYCLE,
    where ``Path.resolve`` raises ``RuntimeError`` and NOT ``OSError``, so a guard
    catching only the latter let the exception escape and took down variable
    resolution and ``/api/variables`` with it.

    So the invariant is not "also catch RuntimeError". It is that every way we can
    fail to prove these bytes are inside the fence has the same correct answer --
    no file layer -- and enumerating exception types is what keeps producing the
    next round. A variables file is an optional convenience; no failure to read one
    may take down a turn.
    """

    def test_a_symlink_cycle_answers_no_file_instead_of_raising(self, wired):
        """The reported repro, through the real filesystem."""
        from kiro_crew import platform_compat

        store = wired / "variables"
        store.mkdir()
        # workspaces -> loop -> workspaces. `resolve()` on anything under it raises.
        platform_compat.symlink_or_junction(store / "loop", store / "workspaces")
        platform_compat.symlink_or_junction(store / "workspaces", store / "loop")
        vstore.invalidate_cache()

        assert vstore.workspace_env_path("ops") is None
        assert vstore.workspace_env_values("ops") == {}

    def test_the_cascade_survives_a_cycle(self, wired):
        """The blast radius the finding actually named: a cycle must not stop a turn
        from resolving its variables. The layer below the broken one still wins its
        keys, rather than the whole cascade going down with the file layer."""
        from kiro_crew import platform_compat

        store = wired / "variables"
        store.mkdir(mode=0o700, exist_ok=True)
        (store / "variables.json").write_text(
            json.dumps({"global": {"CITY": "berlin"}}), encoding="utf-8"
        )
        platform_compat.symlink_or_junction(store / "loop", store / "workspaces")
        platform_compat.symlink_or_junction(store / "workspaces", store / "loop")
        vstore.invalidate_cache()

        cfg = KiroCrewConfig()
        cfg.workspaces = {"ops": WorkspaceConfig(dir="w-ops")}
        cfg.default_workspace = "ops"
        assert resolve_variables(cfg).values["CITY"] == "berlin"

    def test_the_guard_does_not_enumerate_exception_types(self, wired, monkeypatch):
        """The ratchet, and deliberately type-AGNOSTIC.

        A guard narrowed back to a list of known-bad types -- ``OSError``, or
        ``(OSError, RuntimeError)`` after the cycle round -- passes every test above
        and then fails on the next type nobody thought of. Raising a type that is on
        no plausible list is what makes this test discriminate: it goes red for any
        guard that enumerates, and green only for one that is total.
        """

        class AnExoticFilesystemError(Exception):
            pass

        real_resolve = Path.resolve

        def exploding_resolve(self, *a, **kw):
            if "workspaces" in str(self):
                raise AnExoticFilesystemError("no plausible except clause lists this")
            return real_resolve(self, *a, **kw)

        monkeypatch.setattr(Path, "resolve", exploding_resolve)
        vstore.invalidate_cache()

        assert vstore.workspace_env_path("ops") is None
        assert vstore.workspace_env_values("ops") == {}


class TestCaseVariantWorkspacesCannotShareAFile:
    def test_a_mixed_case_name_gets_no_file(self, wired):
        assert vstore.workspace_env_path("Ops") is None

    def test_the_lowercase_sibling_still_works(self, wired):
        _write_env(wired, "ops", "OWNER=lowercase\n")
        assert vstore.workspace_env_values("ops") == {"OWNER": "lowercase"}

    def test_the_mixed_case_workspace_does_not_read_the_lowercase_file(self, wired):
        """The leak itself: two distinct workspaces, one physical file."""
        _write_env(wired, "ops", "OWNER=lowercase\n")
        assert vstore.workspace_env_values("Ops") == {}

    def test_the_refusal_explains_itself(self, wired, caplog):
        """An operator with an ordinary `Ops` workspace would otherwise see no file
        layer and no reason for it."""
        with caplog.at_level("WARNING"):
            vstore.workspace_env_path("Ops")
        assert "lowercase" in caplog.text

    def test_a_name_unusable_for_other_reasons_stays_quiet(self, wired, caplog):
        """The case message is specific; a traversal attempt is not an operator typo
        to be coached through."""
        with caplog.at_level("WARNING"):
            vstore.workspace_env_path("../escape")
        assert "lowercase" not in caplog.text


class TestWorkspaceFileValues:
    def test_values_load_from_the_file(self, wired):
        _write_env(wired, "ops", "API_URL=https://ops.test\n")
        assert vstore.workspace_env_values("ops") == {"API_URL": "https://ops.test"}

    def test_a_missing_file_is_not_an_error(self, wired):
        assert vstore.workspace_env_values("ops") == {}

    def test_one_bad_line_costs_its_own_line_only(self, wired, caplog):
        """TOLERANT here, unlike the endpoint that shares the parser: nobody is
        watching, so a malformed line must not blank a whole workspace."""
        _write_env(wired, "ops", "GOOD=1\nnonsense\nALSO_GOOD=2\n")
        with caplog.at_level("WARNING"):
            values = vstore.workspace_env_values("ops")
        assert values == {"GOOD": "1", "ALSO_GOOD": "2"}
        assert "line 2" in caplog.text

    def test_an_unreadable_file_yields_no_values_rather_than_raising(self, wired):
        path = _write_env(wired, "ops", "A=1\n")
        path.write_bytes(b"\xff\xfe\x00invalid utf-8 \xc3\x28")
        assert vstore.workspace_env_values("ops") == {}


class TestTheFileReadIsCachedAndBounded:
    """The layer is read on EVERY resolution, and resolutions run on the event loop.

    Uncached it was a file read plus a parse per turn, per cron dispatch and per nudge
    -- strictly worse than the mtime-cached store read beside it, and the residual
    ``_store_layer`` documents made unconditional.
    """

    def test_a_second_read_does_not_touch_the_file(self, wired, monkeypatch):
        path = _write_env(wired, "ops", "A=1\n")
        assert vstore.workspace_env_values("ops") == {"A": "1"}

        reads: list[str] = []
        real = Path.read_text

        def _counting(self, *a, **kw):
            if self == path:
                reads.append("hit")
            return real(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", _counting)
        assert vstore.workspace_env_values("ops") == {"A": "1"}
        assert reads == [], "the cached read went back to the file"

    def test_an_edit_busts_the_cache(self, wired):
        _write_env(wired, "ops", "A=1\n")
        assert vstore.workspace_env_values("ops") == {"A": "1"}
        vstore.invalidate_cache()
        _write_env(wired, "ops", "A=2\n")
        assert vstore.workspace_env_values("ops") == {"A": "2"}

    def test_the_cache_is_per_workspace(self, wired):
        _write_env(wired, "ops", "A=ops\n")
        _write_env(wired, "default", "A=default\n")
        assert vstore.workspace_env_values("ops") == {"A": "ops"}
        assert vstore.workspace_env_values("default") == {"A": "default"}

    def test_a_caller_cannot_mutate_the_cached_map(self, wired):
        """A cached dict handed out by alias would let one resolution's edit leak into
        every later one."""
        _write_env(wired, "ops", "A=1\n")
        first = vstore.workspace_env_values("ops")
        first["A"] = "mutated"
        assert vstore.workspace_env_values("ops") == {"A": "1"}

    def test_an_oversized_file_is_refused_whole(self, wired, caplog):
        """Refused rather than truncated: half a file silently resolves half a
        workspace's variables, with tokens surviving and no reason given."""
        big = "A=1\n" + ("# " + "x" * 200 + "\n") * 2000
        assert len(big.encode()) > vstore._MAX_ENV_BYTES
        _write_env(wired, "ops", big)
        with caplog.at_level("WARNING"):
            assert vstore.workspace_env_values("ops") == {}
        assert "limit" in caplog.text

    def test_a_file_just_under_the_limit_still_loads(self, wired):
        """Not an everything-is-refused pass."""
        body = "A=1\n" + ("# " + "x" * 100 + "\n") * 100
        assert len(body.encode()) < vstore._MAX_ENV_BYTES
        _write_env(wired, "ops", body)
        assert vstore.workspace_env_values("ops") == {"A": "1"}


class TestAHardLinkedFileIsRefused:
    """The one link the lstat checks upstream cannot see.

    A hard link planted inside the fenced directory shares its inode with a file the
    agent can write. `lstat` reports an ordinary regular file -- there is no link to
    detect at the path level -- so every symlink guard in this module passes it, and
    only `st_nlink > 1` tells them apart. The agent then rewrites ITS path and the
    fenced read follows the shared inode into an operator-authored prompt.

    Fixed by reading through `hooks.safe_read_file_bytes_nolink`, which `hooks.py` and
    `onboarding_import.py` already use. This reader simply did not, which made it the
    weakest reader of a fenced path in the tree -- so the fix is applying a house
    convention rather than inventing a guard.
    """

    def _wire(self, tmp_path, monkeypatch):
        (tmp_path / "config.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(loader_mod, "config_path", lambda: tmp_path / "config.json")
        vstore.invalidate_cache()

    def test_a_hard_linked_dotenv_yields_nothing(self, tmp_path, monkeypatch):
        self._wire(tmp_path, monkeypatch)
        outside = tmp_path / "agent-writable.env"
        outside.write_text("HOST=attacker.example\n", encoding="utf-8")
        ws = tmp_path / "variables" / "workspaces"
        ws.mkdir(mode=0o700, parents=True)
        os.link(outside, ws / "ops.env")
        vstore.invalidate_cache()

        # The premise: nothing at the PATH level distinguishes this from a real file.
        assert not (ws / "ops.env").is_symlink()
        assert os.lstat(ws / "ops.env").st_nlink > 1

        assert vstore.workspace_env_values("ops") == {}

    def test_the_agent_cannot_steer_it_by_rewriting_its_own_copy(self, tmp_path, monkeypatch):
        """The exploit, not just the shape: the second write is the one that would
        reach a prompt after the file had already been reviewed."""
        self._wire(tmp_path, monkeypatch)
        outside = tmp_path / "agent-writable.env"
        outside.write_text("HOST=looks-fine\n", encoding="utf-8")
        ws = tmp_path / "variables" / "workspaces"
        ws.mkdir(mode=0o700, parents=True)
        os.link(outside, ws / "ops.env")

        outside.write_text("HOST=attacker.example\n", encoding="utf-8")
        vstore.invalidate_cache()

        assert vstore.workspace_env_values("ops") == {}

    def test_an_ordinary_file_still_reads(self, tmp_path, monkeypatch):
        """Not an everything-is-refused pass -- `st_nlink == 1` is every normal file."""
        self._wire(tmp_path, monkeypatch)
        _write_env(tmp_path, "ops", "HOST=fine\n")
        assert vstore.workspace_env_values("ops") == {"HOST": "fine"}

    def test_the_reader_uses_the_shared_hardened_helper(self):
        """Structural: a future edit back to `Path.read_text` reopens both the hard-link
        hole and the check-to-use race, and neither shows up in the tests above until
        someone plants a link."""
        import inspect

        src = inspect.getsource(vstore._read_env_uncached)
        assert "safe_read_file_bytes_nolink" in src, (
            "the dotenv read no longer goes through the hardened helper; a hard link "
            "or a swapped inode is readable again"
        )
        assert ".read_text(" not in src


class TestTheFileLayerInTheCascade:
    """Ranked between global and the panel's workspace scope."""

    def _config(self) -> KiroCrewConfig:
        cfg = KiroCrewConfig()
        cfg.workspaces = {"ops": WorkspaceConfig(dir="w-ops")}
        cfg.default_workspace = "ops"
        cfg.agents = {"crew1": KiroCrewAgentConfig(workspace="ops")}
        cfg.default_agent = "crew1"
        return cfg

    def _seed_store(self, root: Path, doc: dict) -> None:
        path = root / "variables" / "variables.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")
        vstore.invalidate_cache()

    def test_the_scope_order_places_the_file_below_the_panel(self):
        assert VARIABLE_SCOPES.index(SCOPE_GLOBAL) < VARIABLE_SCOPES.index(SCOPE_WORKSPACE_FILE)
        assert VARIABLE_SCOPES.index(SCOPE_WORKSPACE_FILE) < VARIABLE_SCOPES.index(SCOPE_WORKSPACE)

    def test_the_file_overrides_global(self, wired):
        self._seed_store(wired, {"global": {"a": "from-global"}})
        _write_env(wired, "ops", "a=from-file\n")
        resolution = resolve_variables(self._config())
        assert resolution.values["a"] == "from-file"
        assert resolution.winning_scope["a"] == SCOPE_WORKSPACE_FILE
        assert SCOPE_GLOBAL in resolution.shadowed["a"]

    def test_the_panel_overrides_the_file(self, wired):
        """An edit made in the UI must take effect: a panel that silently loses to a
        file on disk is a panel that lies."""
        self._seed_store(wired, {"workspaces": {"ops": {"a": "from-panel"}}})
        _write_env(wired, "ops", "a=from-file\n")
        resolution = resolve_variables(self._config())
        assert resolution.values["a"] == "from-panel"
        assert resolution.winning_scope["a"] == SCOPE_WORKSPACE
        assert SCOPE_WORKSPACE_FILE in resolution.shadowed["a"]

    def test_a_file_only_key_still_resolves(self, wired):
        _write_env(wired, "ops", "only=from-file\n")
        assert resolve_variables(self._config()).values["only"] == "from-file"

    def test_a_workspace_with_no_config_entry_still_gets_its_file(self, wired):
        """Keyed on the NAME, not on the config object: a workspace can carry a file
        before it has a config entry, and dropping the layer then would make the file
        silently inert for exactly the operator who just created it."""
        _write_env(wired, "ops", "a=from-file\n")
        cfg = self._config()
        cfg.workspaces = {}
        assert resolve_variables(cfg).values.get("a") == "from-file"

    def test_the_file_of_another_workspace_is_not_consulted(self, wired):
        _write_env(wired, "other", "leaked=yes\n")
        assert "leaked" not in resolve_variables(self._config()).values
