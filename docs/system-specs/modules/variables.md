# Variables

`{{name}}` substitution for prompts: a five-scope cascade resolved per turn, stored
outside `config.json` in a directory the agent can neither read nor write, and
applied **only** to text an operator authored.

Owning modules: [`variables.py`](../../../src/kiro_crew/variables.py) (parsing and
validation, no I/O), [`config/variables_store.py`](../../../src/kiro_crew/config/variables_store.py)
(the fenced store), [`config/loader.py`](../../../src/kiro_crew/config/loader.py)
(`resolve_variables`), and [`dashboard/handlers/variables.py`](../../../src/kiro_crew/dashboard/handlers/variables.py)
(the HTTP surface).

## The name

The panel is called **Environment Variables**, and the label is deliberate rather
than loose. These are template substitutions — they never enter a subprocess
environment, and nothing exports them to a shell. Postman uses the same word for
the same mechanism, and that is the precedent being followed; the intended
direction is *toward* environment-variable ergonomics (a `.env` file per
workspace, bulk paste), not toward process-env injection.

Do not "fix" this by renaming the feature.

## The cascade

Five scopes, narrowest winning, in `VARIABLE_SCOPES` order:

| Scope | Written by | Lives in |
|---|---|---|
| `global` | the panel | `variables/variables.json` |
| `workspace_file` | the operator, in a text editor | `variables/workspaces/<name>.env` |
| `workspace` | the panel | `variables/variables.json` |
| `crew` | the panel | `variables/variables.json` |
| `session` | a per-session override | in memory |

`workspace_file` ranks **below** the panel's `workspace` scope on purpose: an edit
made in the UI has to take effect, and a panel that silently loses to a file on
disk is a panel that lies.

Merging is keyed on **key presence, not truthiness**. A key set to `""` in a
narrower scope shadows a non-empty value in a wider one, because clearing a
variable is a thing an operator does deliberately and a truthiness test would
silently ignore it.

`resolve_variables(config, ...)` returns a `VariableResolution` carrying `values`,
`winning_scope` per key, and `shadowed` — the panel renders the last two, so a
value that lost to a narrower scope is visible rather than mysterious.

Workspace and crew selection follow the same rules as `resolve_agent_bindings`
(unknown agent → default crew's bindings; crew naming a missing workspace →
`default_workspace`), so the variables a session sees always belong to the
workspace it actually runs in. `test_variables_scopes.py` pins the two functions
to the same verdict.

## The trust boundary

This is the part with no substitute, and the reason the feature is shaped the way
it is.

**Substitution applies only to operator-authored text.** An agent that can write
the text it is about to be prompted with can rewrite its own instructions, so
every surface that expands `{{name}}` carries an explicit `operator_authored`
flag, defaulting to **`False`**. A surface that cannot prove a human authored the
text does not expand it — it passes the braces through literally.

Authorship is decided **once**, at the HTTP edge, by a single predicate:

```python
def request_is_operator(request: Any) -> bool:
    return not request.get("app", "")
```

A request carrying an `app` id came from an app, not from a person at the
dashboard. `test_variables_operator_authored.py` carries an AST ratchet
(`TestNoHandlerAssertsOperatorAuthorship`) that fails if any handler re-derives
authorship itself, because five separate rounds of findings came from handlers
each deciding this question their own way. There is one predicate; call it.

`operator_authored` is **persisted** where the surface outlives the request — a
cron job serializes it, and a legacy record without the field reads as `False`.
A stored job that silently upgraded itself to operator-authored on reload would
undo the whole gate.

Agent-prompt expansion was considered and **withdrawn**: there was no way to
establish authorship for that surface, and expanding it anyway is precisely the
hazard above.

## The fence

The store lives at `~/.kiro/crew/variables/` and is listed in
`security._SENSITIVE_HOME_DIRS`, so the agent can neither read it (every
workspace's values) nor write it (what gets substituted into its own next
prompt). Both directions matter; both are asserted through the real matchers in
`TestTheFenceActuallyCoversThisPath`.

That coupling has **no shared symbol** — `security.py` spells the directory as the
literal `"variables"`, `variables_store.py` spells it `_STORE_DIR` — so a rename
on either side would silently unfence the files. The test above is what catches
that, and it is the reason this module does not re-derive containment anywhere
else.

The directory is fenced too, not just the leaf: the lock sidecar and the
atomic-replace temp inode live beside the file.

**The location is verified, not assumed.** A path-name fence protects a *name*, not
the inode it currently resolves to, and it cannot un-plant a link that predates it —
on an install that ran before `variables` was fenced, the directory may already be a
symlink into agent-writable space, and every read would load attacker-chosen values
into an operator's prompt. `store_location_is_trusted` checks with `lstat` semantics
at each level, on both the read and the write path — and *every* predicate in it must
be lstat-based, not only the obvious one. `Path.resolve` follows a link; so do
`Path.exists`, `is_dir` and `is_file`. A **dangling** link — planted at a target that
does not exist yet — reports "nothing here" to all of them, so a guard gated on
`exists()` concluded the location was clean and the writer then created the attacker's
target through it. `TestTheTrustCheckNeverAsksALinkFollowingQuestion` asserts the rule
structurally, because it is the shape of the mistake rather than any one spelling that
keeps recurring. A read yields `{}`; a write raises
`UntrustedStoreLocation`, which is separate from `MalformedStore` because nothing is
wrong with the document and the remedy — removing the link — belongs to the operator.

### Containment is total

`_is_inside_the_fence` answers, it never raises. Every way it can fail to prove a
path is inside the fence — a missing component, a permission error, a symlink
**cycle** (`Path.resolve` raises `RuntimeError`, not `OSError`), an exotic
filesystem — has the same correct answer: no file layer.

This is stated as an invariant rather than an exception list because enumerating
types is what produced three consecutive rounds of findings in that one function,
each fixing the mode just found and leaving the next reachable. The ratchet in
`TestEstablishingContainmentIsTotal` is deliberately **type-agnostic**: it raises
a type on no plausible except-list, so it goes red for any guard that enumerates
— including `(OSError, RuntimeError)`, which passes the cycle test.

The same reasoning governs the module's siblings: `read_store` and
`workspace_env_values` both document *never raises*. A variables file is an
optional convenience, and no failure to read one may take down a turn, a config
load, or `/api/variables`.

## The `.env` file layer

One file per workspace, at `variables/workspaces/<name>.env`.

- **Names** must match `^[a-z0-9][a-z0-9._-]{0,63}$` — **lowercase only**, so two
  workspaces differing by case cannot collide onto one file on a case-folding
  filesystem.
- **Size** is capped at `_MAX_ENV_BYTES` (256 KiB); reads are cached by mtime
  behind a generation guard.
- **The read goes through `hooks.safe_read_file_bytes_nolink`**, not `Path.read_text`.
  Two reasons, and the first is invisible to every other guard in this module: a
  **hard link** planted inside the fenced directory shares its inode with a file the
  agent can write, and `lstat` reports an ordinary regular file — there is no link at
  the path level to detect, only `st_nlink > 1`. The second is check-to-use: `stat()`
  for the size and then `read_text()` by name resolve the name twice, and the helper
  instead opens `O_NOFOLLOW` and `fstat`s the descriptor, so the inode validated is
  the inode read. `hooks.py` and `onboarding_import.py` already guard their reads this
  way; this reader is now consistent with them rather than the weakest reader of a
  fenced path in the tree.

  The helper *raises* `FileTooLargeError` on oversize while this module never raises,
  so that is caught and answered like every other refusal: no variables, and the turn
  continues.
- **Line breaks** are split by explicit regex (`\r\n|\r|\n`), never
  `str.splitlines()`, which also breaks on `\u2028`, `\u2029`, `\x85` and friends
  — a value containing one would otherwise inject a second assignment.
- **Forbidden characters** are the C0 controls except tab, `\x7f`, and the Unicode
  separators above. The check runs on the **raw line**, before anything trims it,
  and rejects the line rather than cleaning it.

  That placement is the point. Three cleaners run during parsing — the
  blank/comment probe, the key trim, and `_unquote` — and a bare `str.strip()`
  removes everything `str.isspace()` covers, which **overlaps the forbidden set**
  by eleven characters. So the convenient call is precisely the one that deletes
  the evidence the validator is looking for: `A=prefix<U+2028>` was trimmed to
  look clean, passed validation, and persisted *truncated* with a 200, on a save
  path with no undo. Checking each cleaner is not equivalent — the value is not
  the only place one can hide, and a future cleaner trimming one character more
  than it meant to would reopen it.

  `_TRIMMABLE` (`" \t"`) is therefore the deliberate complement of
  `_FORBIDDEN_CHARS`, and a test asserts the two sets cannot overlap.
- **Quoting round-trips.** `render_dotenv(parse_dotenv(text))` is stable: a value
  is quoted when it is empty, when it has significant leading/trailing whitespace,
  or when it already begins and ends with a matching quote. `test_variables_dotenv.py`
  pins this as a hypothesis property, because the corrupting case is a value that
  looks quoted and is not.

The file layer is keyed on the **workspace name**, not on a config entry, so a
workspace can have a file before it has an entry in `config.json`.

## The HTTP surface

`GET /api/variables` returns the resolved cascade plus per-key provenance.
`PUT /api/variables` patches exactly one scope.

**Both verbs refuse a caller carrying an `app` claim**, before the read. An app
manifest can allowlist an API path, so reaching the endpoint is not evidence the
caller is the operator, and each direction is its own breach: `GET` discloses every
variable at every scope, `PUT` rewrites what expands into the operator's next prompt.
This is a different question from `request_is_operator`, which decides whether text
may *expand*; it does not decide who may touch the store.

A refused location is an **answered** refusal, not a crash: an untrusted store makes
`PUT` return **409** with `code: "store_untrusted_location"`, naming the operator
action. Letting `UntrustedStoreLocation` escape as a bare 500 would tell the operator
"internal error" for a condition that no retry fixes — the remedy is removing a
symlink by hand, and nothing else would say so.

Writes are a locked **compare-and-swap**: the caller sends the baseline it read,
and a mismatch raises `StaleBaseline` rather than silently overwriting a
concurrent edit. The panel captures that baseline **at apply time**, not at open,
so a panel left open across someone else's edit does not clobber it.

`_WRITABLE_SCOPES` is `(global, workspace)` — **narrower than the readable set**,
and the gap is not symmetrical for the same reason in each case:

- `workspace_file` is the operator's to edit in a text editor; the API deliberately
  does not write it.
- `session` is in-memory and set per session, not patched.
- `crew` is **read but not writable** — the panel resolves and displays crew-scope
  values while offering no way to edit them. That one is a known asymmetry rather
  than a decision, and is deferred; a scope that renders as a source of truth but
  cannot be changed from the surface that shows it is a gap worth closing.

## Tests

| File | Covers |
|---|---|
| `test_variables.py`, `test_variables_scopes.py` | the cascade, key-presence merging, provenance |
| `test_variables_store.py` | the store, its cache, the write path, file mode |
| `test_variables_dotenv.py` | parsing, the round-trip property, the fence, containment totality |
| `test_variables_operator_authored.py` | the trust boundary and the AST ratchet |
| `test_variables_expansion.py`, `test_variables_routes.py` | expansion and the HTTP surface |
| `test_variables_nudge_crew.py`, `test_variables_channels.py`, `test_variables_off_loop.py` | the non-dashboard surfaces and what stays unexpanded |
