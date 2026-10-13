# CLAUDE.md

## What this is

A Python CLI (`promptmend`, PromptMend; the old command `prompt-workflow` stays a
deprecated alias until 1.0.0, #169) invoked by Espanso text-expansion triggers to rewrite a
clipboard/stdin draft into a more precise prompt via a local or cloud LLM. Espanso match files
in `espanso/match/` call the CLI from `type: script` vars (an argv list, no shell, #18) and
paste back stdout.

## Commands

Contributor install (`uv tool install --editable --compile-bytecode` constrained to `uv.lock`, checked by
`scripts/check_tool_lock.py`, then `promptmend espanso deploy --yes`, which substitutes the
absolute CLI path into the match files, since GUI-launched Espanso does not inherit shell PATH):

```bash
./scripts/install_macos.sh      # macOS
.\scripts\install_windows.ps1   # Windows
promptmend espanso status  # missing / in sync / stale / modified / foreign per match file
```

Users install a release instead (`uv tool install promptmend -c <constraints.txt>`, Homebrew
tap, docs/install.md; on Windows `scripts/install.ps1`, the Release asset #186: uv via winget, then
the same Release's wheel + constraints.txt (not PyPI), `doctor`; never setup/deploy; CI's
`install-script` job runs it via `PROMPTMEND_WHEEL`/`PROMPTMEND_CONSTRAINTS` (promptmend never
comes from PyPI), once without uv on PATH, plus a `PROMPTMEND_DRY_RUN=1` rendered copy;
never run it here), which reads no checkout `.env`; docs/install.md "Update"/"Uninstall"
document `--force` and detach-before-uninstall.

Docs map: README.md is the landing page and the PyPI long description (absolute links and
images only; HTML only `<details>`/`<summary>` and `<picture>`/`<source>`/`<img>`; no mermaid
block or `> [!` alerts). Its flow diagram is `docs/flow.mmd`, which `scripts/render_diagram.py`
renders (npx mermaid-cli) into `docs/flow-light.svg`/`flow-dark.svg`, shown by a `<picture>`
with raw URLs; rerun it after editing the `.mmd` (`tests/test_docs.py` checks the SVGs carry
its hash).
The depth lives in `docs/` (index `docs/README.md`): `install`, `usage`, `commands` (the only
home of the exit codes), `configuration` (the only home of the settings tables, which
`tests/test_docs.py` matches against `env_names()`), `profiles`, `privacy`, `interface`,
`troubleshooting`, `benchmark` (the only home of benchmark numbers). Each page: `# Title`, an
intro paragraph, sections, `## See also` last. `tests/test_docs.py` (`DOC_PAGES`, a fixed list;
`docs/prompt-candidates/` is an archived record and stays out) checks every `promptmend …`
invocation in README, the docs pages and the CHANGELOG's Unreleased and newest release notes,
and every standalone `--flag` span (not in benchmark.md, the bench's own flags), against the
Click tree; every repo link and `#anchor` resolves; README is PyPI-safe; each page has the
skeleton. `tests/test_tui_snapshots.py` writes the README screenshots `docs/interface.svg`
(Home) and `docs/try.svg` (Try) with the snapshots.

Checks (CONTRIBUTING "Checks"; CI runs `uv sync --locked`, then these, with `pytest --cov`
held to `fail_under = 95`):

```bash
uv run pytest --cov                             # unit tests, offline
uv run ruff check . && uv run ruff format --check .
uv run mypy                                     # strict: src, scripts, tests, packaging
uv run pre-commit run --all-files               # also YAML checks, gitleaks and zizmor
UPDATE_SNAPSHOTS=1 uv run pytest tests/test_tui_snapshots.py  # after a tui/ screen change
```

Rules for agents:

- `scripts/bench_models.py` and `uv run pytest -m live` (`tests/test_live.py`) call the paid
  OpenRouter API: run them only when asked.
- Never run the installers, `promptmend espanso …` (`deploy`, `status`, `detach`),
  `setup --deploy` or `espanso` itself: they read or rewrite the user's live Espanso match
  files, and a deploy restarts Espanso.
- Tests stay offline: no external API calls; mock HTTP (`fake_http`).
- Every user-visible change gets a CHANGELOG `## Unreleased` entry and an update of the README
  or the relevant docs/ page.

## Architecture

- `cli.py` — main Typer command, `improve`. Reads a draft (`clipboard`/`stdin`/`argument`),
  calls a provider built by `make_provider()` (which applies the gate), and prints the result
  (everything after reading the input is `rewrite(raw, provider, cfg, profile, ran)`, which
  the Try tab calls too; `ran`, a `Rewrite`, keeps the profile id and the built provider when
  it raises, and `marker(exc)` words the failure) with no trailing newline (Espanso inserts stdout verbatim). All failures are caught and
  converted to a `[promptmend: ...]` marker printed to stdout with exit code 0, rather than a
  stack trace or blank expansion, since Espanso has no good way to surface a nonzero exit /
  stderr to the user. A golden-template rewrite (its profile contains `<output_template>`, which
  `tests/test_prompts.py` ties to `<CONTEXT>`) first goes through
  `prompt_builder.repair_template_tags()`, which fixes only the `<CONTEXT>…</GOAL>` slip. Everything printed goes through `_emit()`, which strips control characters, every
  default-ignorable code point (`redaction.DEFAULT_IGNORABLE`) and every other format character
  except the visible prepended concatenation marks, maps other line breaks to `\n`, and keeps a
  selector/joiner run only as one selector plus one joiner after a visible character (after
  ASCII, only a keycap's selector) (the draft gets the same `_clean()`; the length limit is
  checked before cleaning); stdin/stdout are reconfigured to
  UTF-8 because Windows pipes default to the ANSI code page. `--tier pro` swaps in the
  `OPENROUTER_PRO_*` settings (`OPENROUTER_PRO_MAX_TOKENS` only if set; empty inherits
  `OPENROUTER_MAX_TOKENS`) and `PROMPT_PRO_PROFILE` (if set) via `Settings.for_tier()`; `--model`/`--effort`/`--max-tokens`/
  `--timeout` are applied by `Settings.with_overrides()` (`--model` sets every provider's model),
  so `make_provider()` always sees the effective settings (`--max-tokens` for Ollama/LM Studio
  goes in `call_max_tokens`, a per-call field that is not a setting: `setting_fields()` skips
  it). `Settings.for_call()` chains the two, applies the tier only for `provider="openrouter"`,
  and drops back to `PROMPT_PROFILE` when a pro call runs a model other than
  `OPENROUTER_PRO_MODEL`. `config.openrouter_only()` then refuses `--tier pro` and a non-`default`
  `--effort` for any other provider (a ValueError marker, `error_marker` in the history). An
  explicit `--profile` beats both, so the OpenRouter triggers pass none.
  `PROMPT_OUTPUT=clipboard` (or `--output clipboard`, through `with_overrides()`; #134) makes
  `_deliver()` copy the finished rewrite and print only markers: nothing on success, the
  sent-despite note without its blank line, and a provider's cut-off/filtered note, split off
  the rewrite by `providers.base.split_stop_note()` (which `rewrite()` and the bench also use
  before the fence strip). `_copy_failed()` copies for it and for `--copy` (both without the
  stop note): a failed copy (any exception) prints the marker and then the rewrite (outcome
  `clipboard_failed`); success stays `ok`. `--copy` is ignored there;
  `persona` never reads the setting.
- `clipboard_guard.py` — `is_concealed()` asks the clipboard (ctypes: NSPasteboard types on
  macOS, user32 formats on Windows) whether a password manager marked the item, without reading
  it. `cli._read_input()` refuses such an item before `pyperclip.paste` and clears the clipboard
  (Espanso's restore would put it back unmarked); `None` (Linux, a probe error) reads as before. `tests/conftest.py` stubs it so tests never probe the real clipboard.
- `factory.py` — `make_provider(name, cfg)` builds a provider from `Settings`; `PROVIDER_NAMES`
  lists the valid names. `cli.py` and `scripts/bench_models.py` both build through it. For
  OpenRouter it adds the endpoint pin (`OPENROUTER_PROVIDER`), `provider.data_collection`
  when `OPENROUTER_DATA_COLLECTION` is set (both tiers; empty sends no field) and
  `reasoning: {effort, exclude: true}` (`OPENROUTER_REASONING_EFFORT`; empty omits it).
  Ollama gets `OLLAMA_NUM_CTX` as `options.num_ctx` (#168; empty, the default, sends none).
  An optional `observer=` is passed to every provider, inside the gate (no request, no record).
- `providers/usage.py` — `AttemptUsage` (frozen) and the `UsageObserver` protocol. With an
  observer, `post_json(..., meter=Meter(...))` reports one record per HTTP attempt (both
  attempts of a retry, timeouts and non-2xx included) before it returns or raises, so usage
  survives a `finalize_content()` failure. Each provider adds what its body reports
  (`openrouter_usage`, `openai_usage`, `anthropic_usage`, `ollama_usage`). Records hold
  metadata only, never prompt, response, body or key text. A missing cost is `None`/`unknown`,
  never 0; the factory's `local=True` (loopback, non-cloud Ollama/LM Studio) makes it
  `not_applicable`. Observer and parser exceptions are swallowed (no retry, output unchanged);
  `decimal` is imported only when a cost is parsed. Nothing stores the records yet (#88, #89).
- `config.py` — `Settings` is a frozen dataclass read from env vars, with defaults for each
  provider. Values are parsed strictly (`_bool`, `_positive_int`, ...) and a bad one raises a
  `ValueError` naming the variable; API key fields are `secret` (kept out of `repr()`). Any error
  that quotes a rejected value goes through `redaction.safe_repr()`, since markers are pasted
  into the focused app; it also hides a value matching PROMPT_EXTRA_PATTERNS once settings
  resolve (`ConfigLayers.resolve()` resolves that setting first and calls
  `redaction.set_user_patterns()` before parsing the rest; conftest resets it). Errors raised
  before that point, and Click usage errors before any load, see only the built-in patterns. `Settings.load()` builds from `ConfigLayers.resolve()`, a pure merge
  of layers (lowest first): built-in default < saved settings < the secret store < the real
  environment; per-call overrides come after, in `Settings.with_overrides()`. Saved settings
  are the `.env` that `env_file_override()` names alone if set (`$PROMPTMEND_ENV`, else its
  alias `$PROMPT_WORKFLOW_ENV`, empty = unset; messages name `env_file_var()`, the one in
  effect) (legacy mode: no TOML, no secret store, exactly as before); else the user config dir's `config.toml` once it exists (then no `.env` is read, so
  one can never shadow a saved value; repair mode reports a lingering one); else the first
  readable of the editable-install repo root's `.env` (derived from `__file__`) or the user
  config dir `.env` (read as UTF-8, a byte order mark dropped; one that is not UTF-8 is an error,
  never skipped for the next one; an unquoted ` #` starts a comment, and repair mode and
  `config migrate` flag only a `_HASH_IN_VALUE` value cut that way: the extra patterns, which
  also fail strict mode, so the gate never runs on part of them, and the persona, where `#`
  may be data). The secret store (`config_files.secret_store()`, today `secrets.toml`; a
  keyring seam is left as a TODO) holds only `secret_names()`. A broken `config.toml` or store
  fails closed: `improve` prints the marker. `persona` then reads PROMPT_PERSONA alone
  (`ConfigLayers.resolve(only=...)`: another setting's bad value or a broken secret store does
  not hide it) and prints its placeholder only when the persona itself cannot be read. It never
  writes `os.environ` (a second load sees an edited file; child processes inherit nothing) and
  records each key's source (`default`, `file:<path>`, `env`) and the layers it shadows. Only
  `env_names()` keys are taken, so a `.env` cannot set `HTTPS_PROXY`, `SSL_CERT_FILE` or any
  other variable, and a value holding another setting's `NAME=` (a merged line) is refused.
  `resolve(strict=False)` (repair mode) returns those errors as `Finding`s and falls back to the
  next lower layer (recorded in `Entry.rejected`) instead of raising; it also notes an unreadable
  `.env` and lines without `=`, which strict mode skips silently. It deliberately never reads the
  cwd, so a planted `.env` cannot redirect the base URL or enable the override. This matters
  because Espanso runs the CLI as a GUI-spawned subprocess without an inherited login-shell
  environment. The user folders (#169): `config_folders()`/`data_folders()` give the new
  (`APP_DIR`, `promptmend`) and legacy (`LEGACY_APP_DIR`, `prompt-workflow`) folder under
  `$XDG_CONFIG_HOME`/`%APPDATA%` and `$XDG_DATA_HOME`/`%LOCALAPPDATA%`; `folder_in_use()`
  (behind `_user_config_dir()` and `user_data_dir()`, on every call, trigger path included)
  takes the new one unless only the legacy one exists, then reads and writes the legacy one,
  and never creates a folder. `tests/conftest.py` points `PROMPTMEND_ENV` at a temp file per
  test (and drops `PROMPT_WORKFLOW_ENV`), and `HOME`, `XDG_CONFIG_HOME`, `APPDATA` and
  `_PROJECT_ROOT` at temp dirs.
- `config_files.py` — the light read side (on the trigger path: `tomllib` only once a file
  exists) and `write_atomic()` (temp file in the same dir + `os.replace`; mode 600 from creation
  on POSIX; on Windows a protected single-ACE DACL for the current user's SID, set via Win32
  while the file is empty and read back, else `SecretStoreError`; no plaintext fallback).
  A config dir that is a file or unreadable, or a `config.toml`/`secrets.toml` that is not a
  regular file, counts as absent; only a real but unreadable file fails closed. The
  migration marker is written before any change and names every place a `.env` may go.
- `config_store.py` — services for the management commands (#92), never on the trigger path:
  `save_settings()` (validates with the field parsers, refuses secrets, a newer
  `config_version`, and a file changed since its `read_settings()` snapshot, naming the changed
  keys), `save_secret()`, and the `.env` migration: `plan_migration()` previews (names only),
  `apply_migration(consent=plan.token)` backs up, writes, verifies by reloading that every
  effective value is unchanged, then moves each `.env` into `backups/` (rename, never delete)
  and writes `migration.json`; `plan_rollback()`/`apply_rollback(consent=...)` restore exact
  bytes. Copy mode (#110, `source=<checkout root>`): the earlier checkout's `.env` fills only
  keys whose effective source is the default (the rest are listed as `kept`), the active
  `.env` still migrates as above, the check is `after == before` overlaid with the plan, the
  copied `.env` is never moved, and the marker gets `"mode": "copy"` and a `copied` entry.
  `plan_retire()`/`apply_retire()` later rename that `.env` into the backup, refused while a
  deployed match file or manifest entry still runs a launcher inside the checkout (or Espanso
  cannot say where its files are); a rollback moves a retired one back. `tomli_w` is imported
  lazily (forbidden on the trigger path).
- `history.py` — the local usage history (#88): `HistoryStore` over a per-device SQLite file,
  `config.user_data_dir()/history.sqlite3` (`$XDG_DATA_HOME` or `~/.local/share`,
  `%LOCALAPPDATA%` on Windows; `tests/conftest.py` points both at a temp dir). Metadata only:
  the tables' columns are `OPERATION_COLUMNS` and `ATTEMPT_COLUMNS` (the `AttemptUsage` fields
  of #87, keyed by `(operation_id, seq)`), and only those keys are read from a record mapping.
  Each text column has its own shape (`_TRIGGER`, `_PROFILE`, `_PROVIDER`, `_MODEL`,
  `_GENERATION_ID`, `_VERSION`, or an enum such as `ERROR_KINDS`, which a test ties to
  `providers/base.py`); only the model columns take `/` and `:`, none takes a space, `@` or
  `\`, and `_looks_secret()` (the gate's patterns plus `PROMPT_EXTRA_PATTERNS`, and the
  bare-token check on each part) rejects the rest. A bad optional value becomes NULL (a bad
  cost leaves it `unknown`); a bad required one drops the record. `tests/test_history.py` holds the allowlist and fails on any other column; add a
  column only through a new `_MIGRATIONS` step (forward only, `PRAGMA user_version`). Money is
  exact decimal TEXT plus a unit, never REAL. `record()` never raises and returns within `_BUDGET`
  (0.25 s) from its start: one `BEGIN IMMEDIATE` transaction within `_WRITE_BUDGET`. A write
  that fails (budget, lock, corrupt; #213) goes to the `history.spool` sidecar
  (`_spool_or_mark()`: the validated rows as `{column: value}` of the two allowlists only,
  at most `_SPOOL_LIMIT` 100 records / `_SPOOL_BYTES`), and an invalid record, a full or
  unwritable spool bumps `history.lost` (both: temp file + `os.replace` under one lock file
  holding a nonce; a stale one is broken by renaming it first). The next `record()` (in its
  own transaction), `stats()`, `export()` and doctor (`replay()`) validate each spooled entry
  again (`_from_spool()`: the same row functions, so shapes and `_looks_secret()` apply) and
  store it; `_settle_spool()` then drops the stored and invalid entries under the lock, the
  invalid ones (a non-spool file once) counted in `history.lost`. `health().spooled` counts
  what waits; `reset()` deletes the spool. `_CREATE_EXTRA` also applies while the database
  is under 8 KiB (no schema yet). The services (`stats`, `export`, `prune`,
  `reset`) raise `HistoryError`; `reset()` deletes the file only on `UnusableHistory` (corrupt
  or newer schema), never when it is merely locked. `health()` never raises and sets
  `tracking_incomplete` when writes were lost or the files cannot be written. The write that creates the
  database gets `_CREATE_EXTRA` (0.75 s) on top of both budgets. `record(prune=True)` (the
  recorder's call) then deletes up to `_PRUNE_BATCH` operations past the retention in a second
  short transaction, only after the record committed and only within the budget (an interrupt
  rolls back a whole SQLite transaction, so the two are never one), so a failed prune never
  costs the record. `promptmend history prune` prunes on demand. `PROMPT_HISTORY` (default `true`) and
  `PROMPT_HISTORY_RETENTION_DAYS` configure it; estimates come only from a user `prices.toml`
  in the config dir and are kept apart from reported costs. `sqlite3`, `tomllib`, `csv` and
  `decimal` are imported inside functions, and `recorder.py` imports the module only when a run is recorded.
- `recorder.py` — records each `improve`/`persona` run (#89). `Recorder.track(cfg)` turns it
  on when `PROMPT_HISTORY` is true (settings that fail to load record nothing); its `observer`
  goes to `make_provider(observer=...)` and only collects `AttemptUsage` in memory; `finish()`
  flushes stdout, then writes the operation and every attempt in one `history.record(...,
  prune=True)` after `_emit()`, so history never changes stdout, the exit code or a retry, and
  a rejected reply's charge is kept. `cli._failure()` maps the marker's exception to the
  outcome: `ConcealedClipboard` -> `concealed_refused`, `ClipboardUnavailable` ->
  `clipboard_failed`, `gate.GateBlocked` (the gate's block, and factory.py's `PROMPT_LOCAL_ONLY` and non-https
  URL refusals) -> `gate_blocked`, a non-ProviderError/ValueError ->
  `unexpected_error`, a ProviderError after a 2xx last attempt (`Recorder.answered`) ->
  `validation_failed`, else `error_marker`. Off, it never imports `history`/`sqlite3`
  (`tests/test_trigger_contract.py` runs both ways).
- `prompt_builder.py` — `PROFILES` maps a profile name (`default`, `general`) to a system
  prompt used to instruct the rewrite; `ALIASES` keeps the retired `default-pro` resolving to `default`. `render()` fills the `{{PERSONA_RULE}}` token (and
  `{{PERSONA_OPENING}}`, which opens both examples' CONTEXT, #49) in one pass from
  `PROMPT_PERSONA` (also printed by the `persona` subcommand for the `-p-` snippet). `default` (the
  `PROMPT_PROFILE` fallback) rewrites the draft into the golden template kept in
  `espanso/match/prompts-template.yml` (`CONTEXT / GOAL / INSTRUCTIONS / CONSTRAINTS / INPUTS / OUTPUTS`),
  selecting the plan-first vs execute-now and the independent-review vs self-review instruction
  variant from signals in the draft. It treats the whole user message as the draft (data, not
  instructions), always rewrites in English (adding a `- Language:` constraint for other
  languages) and matches `OUTPUTS` to the deliverable. The prompt uses lowercase XML tags for its
  own structure so they are not confused with the uppercase output sections. Its exact variant
  wordings are
  matched by `scripts/bench_models.py` and `tests/test_bench.py` (which also checks the `-p-`
  template in `prompts-template.yml`); change them together, and
  re-run the benchmark (`--suite all --system-prompt-file`) on both default models before
  shipping a prompt change. Both tiers send `default` (`PROMPT_PRO_PROFILE` is empty by
  default), so one prompt serves flash-lite and gpt-6-luna; the bench scores it on both. Open
  weaknesses are listed in CONTRIBUTING.md ("Known gaps in the default prompt"). `general`
  (the local triggers) is a short draft-as-data rewrite in the language of the user's request; the bench scores
  it with `--profile general` via `check_general()`, and `test_general_profile_contract` pins its
  phrases. `strip_outer_fence()` removes a fence around a whole reply; the CLI and the bench
  apply it to every profile before the tag repair.
- User profiles: `system_prompt()` also reads `<user config dir>/profiles/<name>.md`
  (`user_profiles_dir()`, the dir `config._user_config_dir()` gives `.env`), but only for a name
  that is not built in, or a built-in listed in `PROMPT_PROFILE_OVERRIDES` (strictly parsed;
  built-in names only). A user `default.md` without that opt-in is ignored, never a silent swap,
  and a built-in that is not opted in touches no file. Names must match `PROFILE_NAME` (lower case,
  no dots, separators or Windows device names, so `--profile` cannot leave the dir and `Default`
  never opens `default.md` on a case-insensitive FS) and the listed file name exactly. `user_profiles()` reports each file as
  added/overrides/shadowed/invalid name, plus opted-in built-ins without a file (missing), for
  the doctor/profiles commands (#92). `profiles.migrate_profiles()` copies a checkout's added
  or modified `src/promptmend/prompts/*.md` (against `git_pristine_profiles()` or the
  packaged `PROFILES`) into that dir: copy only, exclusive create, never deletes or overwrites.
- `assets.py` — the Espanso match files as package data. Hatch `force-include` copies the repo's
  `espanso/match/` (the source of truth) to `promptmend/espanso/match/` in the wheel. An editable install falls back to the checkout's folder.
  CI's `wheel` job builds sdist + wheel, installs the wheel in a clean venv outside the checkout
  and runs `scripts/check_wheel.py` (match files, profiles, `promptmend --version` and the
  `prompt-workflow --version` alias, `persona`) on all three OSes.
- `deploy.py` — managed Espanso deployment behind `promptmend espanso deploy|status|detach`
  (cli.py imports it lazily, and `tests/test_trigger_contract.py` keeps it off the trigger path).
  `plan()` renders each packaged match file (`render()`: the stamp line
  `# promptmend <version> (managed; edit at your own risk)` + a literal replace of
  `__PROMPT_WORKFLOW__`, byte-identical to the old scripts otherwise, see `tests/test_deploy.py`;
  `_STAMP_LINE` also takes 0.18.0's `# prompt-workflow <version>` stamp, #169)
  and compares it with disk and the manifest (`user_data_dir()/espanso-manifest.json`: target,
  asset version, digest, launcher, backups) into `missing`/`in sync`/`stale`/`modified`/`foreign`, and lists the folder's other
  `.yml`/`.yaml` files (links too, never followed; not the legacy `base.yml`, backups or
  subfolders) as `Plan.yours`, which status and the Triggers tab show and nothing writes (#38).
  A file that is exactly what any release wrote counts as ours (`stale`): stamp dropped, the
  launcher (`launchers_in()`: a script var's first arg, `args: ["<path>", …`, or the
  `\"<path>\"` of the shell cmd lines every release before #18 wrote; Windows forward
  slashes) put back as the placeholder,
  and its SHA-256 found in `match_history.KNOWN_SOURCES` (generated by `scripts/update_match_history.py` as a
  union that never drops a listed digest: the `v*` tags, the file at every commit that changed it
  on this branch and on `origin/HEAD` (an editable install runs untagged commits), and the
  current sources; rerun after editing `espanso/match/`
  and at each release, `tests/test_deploy.py` checks it). A symlinked target is `foreign`.
  `Manifest.load()` type-checks every field (`DeployError` otherwise), writes go through
  `mkstemp` in the same folder + `os.replace`, and backups use `"xb"` after an `is_symlink()`
  check.
  `apply()` writes only into `<espanso>/match/` (never `config/`, #37), keeps a modified/foreign
  file unless the caller chose `ours` (timestamped backup; pruning to 2 deletes only paths in the
  target's folder named exactly `<name>.bak-<14 digits>[-n]`) or `side`
  (`<name>.promptmend-new`), and retires the pre-0.9 `match/base.yml` (naming either command)
  with a backup. `is_legacy_launcher()` spots a deployed `prompt-workflow[.exe]` launcher, which
  doctor's `launcher` check WARNs about (redeploy). The
  CLI ends a deploy that kept a file with a stderr `WARNING:` naming it (exit 0).
  `detach()` (default `--keep-static`, D-UNI-1) removes only owned files whose digest still
  matches, and only entries naming one of `assets.match_names()` in the current (resolved)
  Espanso match folder; `--espanso-dir` is resolved to an absolute path first. `resolve_launcher()` picks the channel's stable entry point (uv tool bin, Homebrew
  `<prefix>/bin` or `opt`, Scoop shim, else a running console script outside any versioned,
  `Cellar` or `.venv` dir; a frozen build (`sys.frozen`, #185) skips those lookups: on Windows
  WinGet's `%LOCALAPPDATA%\Microsoft\WinGet\Links\promptmend.exe` when it is the running exe
  (`os.path.samefile`), else the exe inside `WinGet\Packages\vlastimilbures.PromptMend_*\`,
  also machine scope `%ProgramFiles%\WinGet\{Links,Packages}`, channel `winget`, else the exe
  itself, `script`, refused with a DeployError naming a fixed place when its path has an
  X.Y.Z part or lies under the temp folder); `launcher_text()` guards the path, which sits in a
  double-quoted YAML string that Espanso runs with no shell (Windows converts `\` to `/`
  first): it refuses a quote, a backslash, a character YAML cannot hold, `{{` (Espanso fills
  its variables into every script param) and Espanso's `%HOME%`, `%CONFIG%`, `%PACKAGES%`
  (replaced in every script arg); shell characters (`$`, backtick,
  `% ^ & | < >`) are allowed since #18. Every external command
  (`espanso path config`, `espanso restart`/`start`, `uv tool dir`, `brew --prefix`) goes
  through `run_command`, which `tests/conftest.py` replaces with a refusal.
- `espanso_windows.py` — on Windows (`deploy.WINDOWS_ESPANSO`, which conftest pins to
  False), `deploy.run_command()` hands every `espanso …` argv to `run()`, which never reads
  Espanso's output (`espansod.exe` writes to the parent console, #210): it finds
  `espansod.exe` (running process via ctypes, PATH, `%LOCALAPPDATA%\Programs\Espanso`, the
  uninstall registry; #211), answers `path config` from disk (portable `.espanso` next to it,
  else `%APPDATA%\espanso`), `status` from the named pipe `espansodaemonv2`, and runs
  `start`/`restart` behind `%SystemRoot%\System32\cmd.exe /d /c` with `CREATE_NO_WINDOW |
  CREATE_NEW_PROCESS_GROUP` and DEVNULL stdio, then polls the pipe (#209). Probes are
  injected via `System` (`tests/test_espanso_windows.py`).
- `commands/` — the management commands (#92): `setup`, `config show|get|set|unset|validate|
  migrate|rollback`, `secrets set|status|remove`, `profiles list|migrate`, `stats` (`--verbose` adds the disclosure and notes, #222),
  `history export|prune|reset`, `doctor` (plus `espanso` in `cli.py`, which gained
  `deploy --dry-run`). `commands/shell.py` (#183) is `promptmend shell`: a prompt_toolkit
  REPL (imported only once it opens; `prompt_toolkit` and `promptmend.console` are in
  `doctor.HEAVY_MODULES` and the trigger contract's FORBIDDEN) over `promptmend/console.py`'s
  completion (`words_for()`, `suggest()`) and `describe()` as the bottom bar; without a TTY
  exit 3. `plan(line)` runs everything the parser reads as a command except `improve`,
  `persona` and `shell` (`REFUSED`; their `--help` only when the strict parse succeeds), a
  key-like line (`holds_a_key()`) and `refused_secret()` (also on a whitespace split when
  shlex fails; it covers `config set|get|unset <key name> VALUE` too); a leaf's `--help`
  counts only as the parser read it. `Editor` wraps the default buffer's accept handler (so every
  accept key: Enter, Meta-Enter, Ctrl-O, a search's Enter) to replace a `secret_line()` with
  `<value withheld>` before the prompt exits (`read()` returns the original to plan). `run_here()`
  re-checks `runs_here(argv)`, then starts `[*entry.self_command(), *argv]` with the terminal's stdio (no capture, no timeout, waits through Ctrl-C); `doctor`
  is not forced to `--no-clipboard`. The in-memory history keeps only lines `kept()` allows: those that plan to RUN.
  `load_settings()` (repair mode, never fatal) runs at start and after each run, so
  `holds_a_key()` sees `PROMPT_EXTRA_PATTERNS`: `common.looks_like_a_key()` scans with
  `redaction._user_patterns` (`user_patterns=False` for `common.EXTRA_PATTERNS` itself).
  `repl(read, runner=, echo=)` is the loop tests drive; conftest refuses `run_here`. Started as the `prompt-workflow` alias (`cli._run_as_alias()`:
  `Path(sys.argv[0]).stem`), a management command or `ui` first prints
  `cli.DEPRECATED_ALIAS` on stderr, once; the triggers, `--version` and `--help` never do, and
  `_LazyGroup.main()` makes every usage line say `promptmend`. `cli.py`'s `_LazyGroup` imports a command's module only when it runs or
  `--help` lists it (`_LAZY_COMMANDS`), so improve/persona never load them; it also passes
  every Click usage error through `redaction.redact_words()`, since Click quotes a stray argument;
  `tests/test_trigger_contract.py` forbids `commands`, `doctor`, `smoke` and `config_store` on
  the trigger path. `--version` is an eager callback on `_main`. Unlike the triggers they use
  ordinary exit codes (`commands/common.py`: 0 ok, 1 failed, 2 usage, 3 needs a terminal, 4
  doctor/validate found problems; docs/commands.md "Exit codes"), print errors to stderr via `@guard` (never a
  traceback), load settings in repair mode (`load_layers()`), never prompt without a TTY
  (`stdin_is_tty()`, which tests patch), colour only on a TTY without `NO_COLOR`, and take a key
  only from stdin or `getpass` (`read_secret()`); `config set` refuses secret names and any
  value `scan()` flags as a credential. `config set`/`secrets set` refuse in legacy
  `PROMPTMEND_ENV` mode, and `config set` refuses while a `.env` is in use without
  `config.toml` (it would be orphaned): migrate first. `config migrate|rollback` apply only with
  an interactive yes or `--yes --preview-token <token printed by the preview>`.
  `tests/test_commands.py` walks the whole command tree: no option named like a secret, a key
  given to any value is never saved, and every command on a broken `.env`/TOML/secrets exits
  0-4 without a traceback (add a new command to `_commands()` there).
- `tui/` — the full-screen Textual interface (#93): `promptmend ui`, and a bare
  `promptmend` when stdin and stdout are both TTYs (D-UI-1: `_LazyGroup.parse_args()` turns
  `[]` into `["ui"]`; anywhere else `no_args_is_help` prints the help and exits 2 exactly as
  before, pinned by `tests/golden/no-args-help.txt`; `ui` without a TTY exits 3). `textual` is a
  required dependency (D-UI-2) imported only by `tui/`, which only `commands/ui.py` imports, once
  the interface opens; `tests/test_trigger_contract.py` checks neither the triggers nor `--help`,
  `doctor`, `config show` and the other headless commands load it. Seven tabs (Home, Settings
  (`2 Settings`, tab id `settings`, was `2 Providers` before #199; all seven fit at 80
  columns), Profiles, Triggers, History, Diagnostics, Try; one module each, `tui/<tab>_pane.py`,
  over `tui/base.py`'s `Pane` and shared flows, #243) show one `tui/state.State`, which `gather()`
  reads again in a worker thread after every change (a generation number drops an older load
  that finishes late); each action calls the same services as the
  headless command (`commands/settings.save_setting()` is shared with `config set`, `store_key()` with
  `secrets set`; the .env migration is `base.confirm_migration()`) and adds no
  logic. Keys appear only as set/not set; destructive actions (key removal, deploy, detach,
  migrate, history prune/reset) go through a dialog whose Cancel has the focus, and deploy keeps
  an edited file unless a conflict choice says otherwise. Only one deploy/detach dialog opens at a
  time, and each re-reads the plan or manifest before writing and refuses if it changed since the
  preview. Workers run through `Pane.background()`, which reports any failure instead of
  crashing; base URLs and models are shown through `common.shown_value()`. No provider call except the explicit
  "Test call" (`smoke.run()`) and a confirmed Try tab run; doctor runs without the clipboard. Bindings are letters and digits
  only (on `MainScreen`, so none fires under a dialog); `t` switches to the high-contrast theme;
  Textual honours `NO_COLOR`; exit is `sys.exit(app.return_code or 0)`. `tui/previous.py`'s
  `PreviousInstallScreen` (#110) opens by itself once per session when `State.previous`
  (`previous_install.detect()`) has a candidate or an unskipped pending copy, and from Home's
  "Previous install…" (shown while `State.previous` is not gated, so "Enter a path…" stays
  reachable after a skip or with nothing found, #237, or with a candidate, a pending copy or
  `previous_error`, #222;
  tests open it with `app.open_previous()`): four steps (copy via `base.confirm_migration(source=)`, profiles via
  `base.copy_profiles()`, deploy via `TriggersPane.start_deploy()`, retire via
  `plan_retire()` in a worker, enabled only once no launcher points into the checkout and
  refused without `espanso path config`'s answer), none opening while `TriggersPane.busy`;
  digits 1-4 as keys,
  "Enter a path…" (detected again with `entered=`) and "Skip" (the skip marker, pending root
  too). `tests/test_tui.py`
  drives each tab with Pilot (`asyncio.run`, no pytest-asyncio); `tests/test_tui_snapshots.py`
  compares SVG exports with `tests/snapshots/` (Linux and macOS; `UPDATE_SNAPSHOTS=1`
  regenerates them, and the Home and Try ones are `docs/interface.svg` and `docs/try.svg`).
  The header shows `PromptMend <version>` (#112); the snapshots pin the version (`VERSION`,
  "0.20.0", the release that ships those screenshots), so a release changes none.
  Home (Mockup B, #112) is `tui/home.py`'s pure `home_rows(state)` (no reads of its own, no
  Textual; `tests/test_tui_home.py`): first `Version` (#197, from `State.update`, which
  `gather()` checks once and hands to `doctor.run(update=)`; a newer release is status
  `NOTE`, word `new`, text `<latest> available`, detail `HOW_TO_UPDATE` (About has the
  command); `worst()`/`headline()` count it as ok and `pill()` never sees it; the
  snapshots pin it to `latest`), then rows for `-i-`, `-ip-`, the match files/Espanso, the
  history, `Output` (`PROMPT_OUTPUT`, #134: paste or clipboard), and
  the doctor counts; each a status word plus the tab that fixes it, and `headline()` names the
  worst (first of equals; Markdown backticks stripped, #174). `pill()` is the header's
  sub-title status (`ok` / `1 problem, 2 warnings`), set first (before "set up and manage", so
  a narrow terminal keeps it, #174) in `ManageApp._show()`; its `Output` row's detail names
  the Settings tab (`CHANGE_OUTPUT`, #198). The Settings help line and the Diagnostics
  tables show a settings file under the home folder as `~/…` via `common.short_label()`
  (display only; headless `config show` keeps `source_label()`). The Settings tab (#199) is
  `settings_pane.SettingsPane` over `tui/settings_model.py` (pure, no Textual;
  `tests/test_tui_settings_model.py`): `SETTINGS` gives every `env_names()` key exactly once
  a group (`GROUPS`: Output, Keys, Privacy, Models, History, Interface; `PROMPT_PROVIDER` is in
  Models, its help naming a bare `improve` and the Try tab, #217), a kind (`bool`,
  `choice`, `int`, `text`, `key` for `secret_names()`), choices from the code (`OUTPUTS`,
  `PROVIDER_NAMES`, `""` + `EFFORTS`/`DATA_COLLECTION`, `PROFILE_CHOICES` filled by
  `choices()` with the built-in and the user's added profiles) and a one-line help; `rows()`
  gives each row's value (keys set/not set, `PROMPT_PERSONA` only `<set, hidden>`, else
  `common.shown_value()`), its dot (`on`: a bool true, a key set, or a value not at its
  default or set in a file/env) and its source file name (`source_name()`; `where` is the
  `short_label()` for the help line). `SettingsList` (an `OptionList`, group headers as
  disabled options, the `▶` cursor re-rendered on highlight) binds j/k, Space (bool: save at
  once), Enter (`PickModal(selected=)`, current marked; `EditModal`, prefilled, which keeps a
  value `settings_model.parse()` rejects or that looks like a key in the dialog with the
  reason; a key row's `set_key()`), `u` (`settings_cmd.unset_setting()`, shared with `config
  unset`; a key row's `remove_key()`) and `/` (`FilterInput`: Escape clears and returns, Enter
  or Down returns). Its keys act only while it has the focus: `MainScreen` focuses it when the
  tab opens and drops the focus when another tab opens; `r` is Reload on every tab (#215). Saves go
  through `save_setting()`; each logs `teach.for_setting()` (from `teach.SETTING_ACTIONS`,
  which the drift test parses; the persona's value as `<value withheld>`). The provider
  overview (`show_routes()`: `PROMPT_LOCAL_ONLY`/`PROMPT_PROVIDER` sentence and routes table)
  is on Diagnostics. `tui/intro.py`'s `IntroScreen` (#112; text from
  `tui/brand.py`: `brand.NAME` is the one display name the header, intro and About read, so a rename (#169) changes one line; `brand.MARK` is the ASCII logo (a `[+]` speech bubble beside figlet-small "PromptMend"), `brand.LOGO` the mark plus `TAGLINE`; the mark is dropped at `brand.NARROW` columns or less, frame is a Textual
  border) is pushed over `MainScreen` on every launch for `ManageApp(intro=True)`; it has no
  timer (#173): any key (Enter, Escape, …) or click closes it and is consumed. Its muted
  `INTRO_HINT` names `teach.INTRO_OFF` (`config set PROMPT_UI_INTRO false`, parsed by the
  drift test). `intro=False` shows none: `ui --no-intro`, `PROMPT_UI_INTRO=false`
  (read by `commands/ui.wants_intro()` in repair mode; the triggers never read it) and the
  tests (`drive()` and `_shoot()` default to it). The previous install offer waits for the intro
  (`_maybe_offer()`, called again when it closes). `a` opens `AboutScreen`
  (`brand.about_facts()`: version, doctor's install channel, Python/Textual via
  `brand.runtime()`, which the snapshots pin, the config and data dirs, licence, repo).
  Every action names its headless command (#111 stage 1, `tui/teach.py`, no Textual):
  `teach.BUTTONS` maps each button id to its commands (placeholders like `<NAME>`), shown as
  the tooltip by `base.button_row()`; a button with none goes in `teach.NO_COMMAND`
  (`test_every_button_has_its_command_as_tooltip` fails otherwise). `Pane.report(...,
  command=teach.equivalent(...))` (and `attempt(command=)`, `PreviousInstallScreen.report`)
  shows a muted `$ promptmend …` above the result and appends to `ManageApp.session`,
  which Home shows as a read-only log (`session_text()`, latest `SESSION_LINES`), or
  `teach.RECIPES` while it is empty. Home's "Recipes…" (`modals.PickModal`, its list focused)
  only prefills the command line (`CommandLine.prefill(teach.line(*argv))`), never runs;
  "Copy last" (disabled while the session is empty) sends the latest `Entry.command` as the
  log shows it (withheld values and placeholders included) through `App.copy_to_clipboard()`
  (OSC 52, write only, the terminal may drop it; never pyperclip). A value that looks like a key is shown as
  `<value withheld>` (`console.shown_arg()`). `tui/console.py` (#111 stage 2) is Home's command line
  (its Textual widgets only: `CommandSuggester`, `CommandLine`, `Host`; everything without
  Textual, `resolve()` to `describe()`, `POLICY`, `run()`, lives in `promptmend/console.py`,
  #183, which `promptmend shell` shares and which must not import textual; conftest refuses
  `run` in both modules) (`CommandLine`, `#home-command`; `c` on `MainScreen` switches to Home and focuses it, and
  nothing is focused at launch): `resolve()` walks the Click tree (no callback runs; the
  drift test in `tests/test_tui_teach.py` uses it too), `words_for()` gives the candidates
  (key names only after `secrets set|remove`, never elsewhere), `CommandSuggester` completes
  the last word and `describe()` the help line (a usage error through `redact_words()`, a
  key-like line only `<value withheld>`). Enter keeps the line in an in-memory Up/Down
  history (not a key-like one) and acts by `decide()` on `POLICY` (one `Rule` per visible
  leaf; a test walks the tree): `--help` and a bare `--version` always run, a leaf's
  `--help`/`--dry-run` only as the parser read them (`Resolved.values`), never as another
  option's value (`improve --profile --help`); RUN (commands that read, preview, change a
  setting or write an export, asking nothing; `doctor` gets `--no-clipboard` unless a
  clipboard flag is given) and a `--dry-run` of a `dry_run` rule without a `TERMINAL_ONLY`
  option go to `run()`, which first re-checks `runnable(argv)` (a fresh parse must decide RUN
  for that exact argv; `HomePane.run_line()` checks it too), then starts `[*entry.self_command(), *argv]`
  (no shell, stdin DEVNULL, stderr into stdout, `NO_COLOR=1`, `TIMEOUT` 120 s, output capped
  at `OUTPUT_CAP`) in `HomePane.background()`, one at a time (`HomePane.running`); its
  `transcript()` (each line through `redact_words()`, then `exit N` or the timeout) goes in
  the `#home-output` RichLog (hidden until the first run), the session log gets an `Entry`,
  and the state reloads. DIALOG opens the owning tab's dialog (`HomePane.open_dialog()`:
  `start_deploy`, `ask_detach`, `migrate_env`, `set_key`/`remove_key` with the name picked,
  `prune` with `--older-than` filled in, `reset`), ignoring `DIALOG_DECIDES` (`--yes`, …);
  `TERMINAL_ONLY` options and TERMINAL rules (`setup`, rollback, retire, profiles migrate)
  say to quit and run it in a terminal; REFUSE (`improve`, `persona`, `ui`) says why.
  `secrets set` with anything but a key's name (a value, `--stdin`) is cleared and not kept
  (`refused_secret()`). `CommandLine(runner=)` (or `line.runner`) takes a fake in tests;
  `tests/conftest.py` replaces `console.run` with a refusal in every test (the `--version`
  smoke test calls the real one it kept at import, `REAL_RUN`), and
  `tests/test_tui_console.py` checks every candidate parses and that no swallowed `--help`
  or `--dry-run` runs. `tests/test_tui_teach.py` parses every button command
  and recipe against the Click tree (the drift test).
  `tui/try_pane.py`'s `TryPane` (tab `7 Try`, #111) rewrites a draft typed into its
  `DraftArea` (`#try-draft`; Escape leaves it) and never touches the clipboard
  (`tests/test_tui_try.py` makes pyperclip and `is_concealed` fail if called). Run takes the
  pickers (`#try-target` stub/real, provider set to the settings once and then kept across
  reloads, profile defaulting to `CONFIGURED`, which passes no profile so `cfg.profile` applies
  as for a trigger, `PROMPT_PRO_PROFILE` on the pro tier included, then the built-ins and the
  user's added ones; tier), loads settings STRICTLY (`ConfigLayers.resolve()`, never repair
  mode: a bad `PROMPT_LOCAL_ONLY`, `PROMPT_EXTRA_PATTERNS` or `config.toml` is the marker and
  nothing is sent, stub included), applies
  `for_call()` and `openrouter_only()`, and calls `cli.rewrite()` in process in
  `Pane.background()` (the draft is never in an argv), one run at a time (`running`, Run
  disabled). Stub (default): `smoke.stub_server()` + `stub_settings()`, gated as the real
  call would be (`cli.rewrite(force_remote=_leaves_machine(provider, real cfg))`, passed on
  to `make_provider(force_remote=)`, which only this passes and which only escalates:
  remote = `_leaves_machine() or force_remote`), after `factory.check_base_url()` on the real
  cfg (the https rule make_provider() applies to OpenRouter/Anthropic), timed around the call alone, no
  `Recorder`, no session-log entry (its command would call the real provider). Real: a
  `ConfirmModal`
  (provider, model and base URL via `common.shown_value()`, whether it is recorded), then a
  `recorder.Recorder("improve")` (origin `direct`, trigger `recorder.TRY_TAB` `-try-`, which
  `stats` lists apart from a bare `improve`'s `no trigger (direct call)`, #222) whose `attempts` list is the
  observer's, outcome via `cli._failure()`, `finish()` in a `finally` after the result is
  shown (a failed hand-off still records the call); it logs
  `teach.equivalent("improve", …, "--text", "<draft withheld>")` (`--profile` only when one
  was picked). The result (or
  `cli.marker()` with the sent-despite note, through `cli._clean()`) goes in a read-only
  `TextArea` (`#try-result`, never markup); `usage_line()` (`#try-usage`) sums tokens and
  shows the cost as reported, `unknown` or `not applicable` (the stub, local models), never 0.
- `entry.self_command()` — the argv prefix of every child that runs the CLI again (the
  interface's command line, `promptmend shell`, `smoke.run()`): `[sys.executable, "-P", "-m",
  "promptmend.cli"]`, or `[sys.executable]` in a frozen build. Stays import-light (trigger
  contract). `entry.main()` answers a bare `--version` (exactly `argv == ["--version"]`)
  itself, through `promptmend.installed_version()`, before `quiet_trigger()` and the CLI
  import (#223); `promptmend/__init__.py` gives `__version__` through a module `__getattr__`,
  so importing the package never loads `importlib.metadata`. Start-up budget (#223):
  `providers/base.py` imports httpx only inside `_encode_headers()`/`_exchange()` (type-only
  at module level) and `cli.py` imports pyperclip only in `_clipboard(op)`, which `_copy_failed()` calls
  (tests patch the `pyperclip` module, never `cli.pyperclip`).
  `tests/test_trigger_contract.py`'s `test_version_startup_budget` and
  `test_improve_startup_budget` (fresh interpreter through `entry.main()`, the latter
  snapshotting `sys.modules` in a fake `ollama.post_json`) forbid httpx, pyperclip, sqlite3,
  textual, prompt_toolkit and `FORBIDDEN` (plus typer/click/rich/`promptmend.cli` for
  `--version`, `importlib.metadata` for `improve`) and cap the count (`VERSION_CEILING`,
  `IMPROVE_CEILING`). The installers (`install.ps1`, `install_windows.ps1`,
  `install_macos.sh`) pass `--compile-bytecode` to `uv tool install` (#216;
  `tests/test_install_scripts.py`, and the `install-script` job's dry-run string).
- `packaging/windows/` — the frozen Windows build (#185): `promptmend.spec` (PyInstaller 6
  onedir, console `promptmend.exe` + `_internal\`, entry `promptmend_main.py` calling
  `entry.main()`, so the trigger's fd-2 silencing is unchanged; `collect_submodules` for
  promptmend/textual/prompt_toolkit, the profiles, the repo's `espanso/match` at
  `promptmend/espanso/match` (where `assets.match_dir()` looks), promptmend's metadata for
  `__version__`, and `-X utf8`). PyInstaller is pinned in the `build` dependency group only
  (`uv sync --group build`), never in constraints.txt or the brew formula.
  `scripts/build_windows.py build` zips it as `dist/promptmend-<version>-windows-x64.zip`
  (`promptmend/promptmend.exe`, `promptmend/_internal/…`, sorted, fixed dates; refused
  without the match files and `prompts/default.md`, `REQUIRED`); `time <exe>`
  times the `-i-` trigger against `smoke`'s stub (job summary). test.yml's `frozen-windows`
  job builds, unzips and checks it (`--version`, `doctor --json` sees `frozen`, `persona`, a
  fake WinGet `Links` symlink reported as channel `winget`, `tests/test_triggers.py -k process`
  with `PROMPTMEND_EXE` set, which makes the replay start that exe, and the latency), then
  renders the WinGet manifests for the zip, runs `winget validate`, and installs it with
  `winget install --manifest` (a copy of the manifests whose `--url` is a loopback
  `http.server`; `LocalManifestFiles` and `LocalArchiveMalwareScanOverride` enabled), checks
  the real `Links` alias (`--version`, doctor's channel `winget`) and uninstalls it.
- `packaging/winget/` — the WinGet manifest templates (#185) of `vlastimilbures.PromptMend`
  (schema 1.10.0, the newest the runner's winget 1.11 validates without a warning; version,
  installer `zip` + `NestedInstallerType: portable`, `promptmend\promptmend.exe` aliased
  `promptmend`, defaultLocale en-US), with `__NAME__` placeholders.
  `scripts/winget_manifest.py --version X.Y.Z --zip <zip>` (or `--sha256`;
  `--url`, `--release-date`, `-o`, default `dist/winget`) fills them (SHA-256 upper case, the
  Release asset URL, notes and licence URLs at the tag; the date from the version's CHANGELOG
  heading via `release_notes.releases()`, or today (UTC) with `--url`), refuses a bad
  version/hash/date, a URL that is not https (plain http only to 127.0.0.1, localhost, ::1)
  or does not end in the release zip's name, a zip named for another version or a leftover
  placeholder, warns when the checkout's version differs, and writes winget-pkgs' layout
  `manifests/v/vlastimilbures/PromptMend/X.Y.Z/` (`tests/test_winget_manifest.py`). Nothing
  here submits to microsoft/winget-pkgs: that is a manual step with the owner's go.
- `relocate.py` — `migrate_folders(environ)` (#169), never on the trigger path
  (`tests/test_trigger_contract.py`, `doctor.HEAVY_MODULES`): `cli._LazyGroup.invoke()` runs it
  before every subcommand except `improve`/`persona`, a `--help`/`-h` anywhere, an unknown
  command and resilient parsing (`--version` is eager and exits first; a bare command turned
  into `ui` does run it), printing each returned line on stderr. For the config and the data
  folder: legacy absent or a symlink, nothing; a legacy folder holding the `.env` that
  `env_file_override()` names (`config.env_file_inside()`), one line asking to repoint the
  variable, and it stays; new absent, `os.rename(old, new)` (keeps mode 600 and the Windows
  DACL); both, each unit the new one lacks is renamed (a file plus its `-wal`/`-shm`/`-journal`,
  `.lock`, `.<pid>.tmp` and `.lock.<hex>.stale` companions, `_unit()`: all or none; `lexists()`
  first: never overwrite), conflicts stay (silent; doctor's `folders` check reports them) and
  the legacy folder is removed only once empty. A merge is not atomic: until it ends the
  triggers already use the new folder, so a setting or key still in the old one is briefly
  missing. A `FileNotFoundError` (a concurrent run moved it) is silent. Then every string in the new folder's
  `migration.json` that is the legacy config path or under it is rebased (`write_atomic()`;
  an unparsable marker is left alone). An `OSError` (a Windows lock) becomes one line, never
  an exception; the next command retries. `tests/test_relocate.py`; the TUI snapshots pin
  `config.APP_DIR` to the legacy name, so the folders they show change only with the rename.
- `doctor.py` — `run()` returns a `Report` of the fixed `CHECK_IDS` (JSON `schema_version` 1:
  only add ids/keys). Read-only: `espanso path config`/`espanso status` and the launcher lookup
  via `run_command`; keys as set/not set; the clipboard only as a length (never read when
  concealed); the one write: with history on, the `history` check first `replay()`s the
  spool (#213), and its WARN names the reason and the fix (`_history_problem()`, data
  `spooled`). Match files `stale`/`missing` or a deployed launcher that is gone fail (exit 4).
  The `persona` check (shared with `config validate` via `persona_problem()`) runs
  `redaction.scan()` (no `bare_token`) with `PROMPT_EXTRA_PATTERNS` over `PROMPT_PERSONA`, which
  the gate never scans, and warns by finding name only (#29), only when a configured profile's
  template (`prompt_builder.template()`) holds `{{PERSONA_RULE}}`, and not with
  `PROMPT_LOCAL_ONLY=true` unless `PROMPT_GATE_LOCAL=true` (`_persona_stays_local()`); shown on
  the interface's Home and Diagnostics tabs; never on the trigger path.
  `import_check()` (the interface's Diagnostics) imports `promptmend.cli` in a fresh
  interpreter (`-P`, so a module planted in the working directory never runs; the smoke test's
  child uses `-P` too) and reports its time, module count and any `HEAVY_MODULES` it loaded;
  in a frozen build it starts nothing and returns `FROZEN_IMPORT` (not applicable). The `cli`
  check's data has `frozen`.
  `deployed_triggers(espanso_dir)` parses the managed match files Espanso has
  (`assets.triggers_in()`, the shipped parser with any launcher as the first arg; regular
  files only), else the packaged `assets.triggers()` (deployed False). The `keys` check
  (#217) names the active `improve` triggers per missing key (`factory.routes()`, refused
  providers skipped): FAIL when deployed, WARN before a deploy; PROMPT_PROVIDER alone (a bare
  `improve`, the Try tab) only WARNs; data adds `triggers`. `local_servers` (#220, INFO only,
  last in CHECK_IDS) asks `local_probe.answers()` for each local provider those triggers use:
  `local_probe.py` (FORBIDDEN on the trigger path, in `HEAVY_MODULES`) GETs a loopback
  `OLLAMA_BASE_URL/api/tags` or `LMSTUDIO_BASE_URL/models` (`TIMEOUT` 0.5 s, own
  `HTTPTransport`, any HTTP answer is True), never another host (None); conftest stubs
  `_get` to refuse, `tests/test_local_probe.py` restores `REAL_GET` under `fake_http`. The
  Triggers tab reads that check's data (`triggers_pane._servers_down()`, no second probe) and shows
  `needs Ollama`/`needs LM Studio` as State for a deployed active local trigger (`missing`
  stays). The deploy never changes for it: `-il-`/`-ilm-` stay active, `-ic-` commented out.
  The `cli` check shows `<argv0>.exe` on Windows when argv[0] has no suffix and the exe
  exists; `history` says `no calls yet` while the file does not exist (#222).
  The `folders` check (#169) WARNs while a legacy folder is still in use, naming why (a
  symlink, the named `.env` inside it, a file holding the new name, else a failed move; doctor
  runs after the move) or holds conflicts left behind; data `config_dir`, `data_dir`,
  `legacy`, `conflicts`.
- `update_check.py` — whether a newer release exists (#197), for `doctor` and the
  interface only (FORBIDDEN in `tests/test_trigger_contract.py`). `check(cfg)` never raises:
  `PROMPT_UPDATE_CHECK=false` is `off` (no request, no file read); else the answer in memory
  (`_last`, so a failed cache write never refetches) or `user_data_dir()/update-check.json`
  (`checked_at`, `latest`) is reused while younger than 24 h, or 1 h (`RETRY_AFTER`) for a
  failure (`"latest": null`); a future, naive or damaged one counts as expired. Otherwise one
  GET of `https://pypi.org/pypi/promptmend/json` (`httpx.Timeout(3)` per phase, the whole call
  bounded to `TIMEOUT` by a daemon thread in `_within()`; env proxies honoured): newest plain
  `X.Y.Z` release not all yanked (`info.version` when `releases` is not a map), compared as
  int tuples (an installed pre/dev release is older than its final, a dev build newer than
  PyPI is `latest`), cached with `write_atomic()`, success or failure. `check_configured()`
  (doctor and `gather()`) resolves only `PROMPT_UPDATE_CHECK` strictly and fails closed
  (`unknown`, no request) on its rejected value or an unreadable settings file.
  `upgrade_command(channel)` gives docs/install.md's command per channel (`winget`: `winget
  upgrade vlastimilbures.PromptMend`; `script`, a plain console script, has none); `doctor.upgrade_hint()` prefers `editable` (git pull, then the
  platform's install script). doctor's
  `version` check (built after `install`, report order unchanged) stays INFO and adds
  `latest`, `update_available`, `checked_at`. `tests/conftest.py` stubs `_fetch` in every
  test; `tests/test_update_check.py` puts the real one back (`REAL_FETCH`) under `fake_http`.
- `previous_install.py` — finds an earlier checkout install (#110) from the launcher in the
  deployed match files (`deploy.deployed_launchers()`) and manifest, the uv tool receipt and a
  path the user entered; never a disk scan, and a checkout's `.env` is only checked for
  existence. A root counts only as `<root>/.venv/bin/promptmend` (or
  `.venv\Scripts\promptmend.exe`, or either with the pre-rename `prompt-workflow`, #169)
  whose `pyproject.toml` names this project (`PROJECT_NAMES`: `promptmend` or the old
  `espanso-prompt-rewriter`, also the uv receipt's folder) and that is not the running
  `config._PROJECT_ROOT`; its profiles are `profiles.prompts_path()` (`src/promptmend/prompts`,
  else the old `src/prompt_workflow/prompts`). Gated (D-MIG-4): nothing in legacy mode or once
  `config.toml` or the secret store exists; a manifest does not gate. Skipped roots live in
  `user_data_dir()/previous-install.json` (damaged = empty). `shadow()` reports a different
  `promptmend` (or `prompt-workflow` alias) first on PATH. `Detection.pending` (ungated, since the copy itself closes
  the gate) is a copy from `migration.json` whose `.env` is not retired yet: doctor WARNs
  (`retire_pending`) once no launcher points into it. `setup` offers the copy at the start of
  its Settings step (`--migrate-from PATH`), then the profiles, and the retire after the
  deploy step; a broken `.env` in a checkout it found by itself (not entered) is a to-do and
  setup then writes nothing, since config.toml or a key would close the gate. `best()` picks the
  candidate setup and the interface offer. Doctor's `previous_install` check; off the trigger
  path.
- `smoke.py` — `setup`'s smoke test: a `ThreadingHTTPServer` on 127.0.0.1:0 answering the
  OpenAI-compatible, Anthropic and Ollama shapes (each reply reports `INPUT_TOKENS`/
  `OUTPUT_TOKENS`, no cost), and a child `python -m promptmend.cli
  improve --provider <p>` whose env points every `*_BASE_URL` at it with a placeholder key (the
  real key is never sent). `stub_server()` (a context manager yielding a `Stub`: port, request
  paths) runs the stub, and `stub_settings(cfg, port)` is `stub_env()` for a `Settings`
  (`dataclasses.replace`: base URLs, placeholder keys, `history=False`), for the Try tab's
  in-process stub runs. `setup` offers a `.env` migration (applies only on an interactive
  yes), previews the deploy (`--deploy` applies, keeping edited files), and discloses the usage
  history (D-HIST-0).

### Data-protection gate

`factory.py`'s `make_provider()` (the only place providers are built) wraps, via `_gate()`,
everything that can send the draft off the machine (`_leaves_machine()`) in `gate.GatedProvider`: `openrouter` and
`anthropic` always, `ollama`/`lmstudio` when the base URL is not loopback (`providers.base.is_loopback()`) or
the Ollama model is a `cloud`-tagged one (`_is_ollama_cloud()`: the tag only, any case, an
`@digest` stripped). `PROMPT_GATE_LOCAL=true` gates a loopback Ollama/LM Studio too
(`_gate(..., remote=False)`), without changing `_leaves_machine()`, so `PROMPT_LOCAL_ONLY`,
`routes()` and the `not_applicable` cost still treat it as local. The gate runs `redaction.scan_draft()` (built-in patterns,
the user's `PROMPT_EXTRA_PATTERNS`, and the whole-draft `bare_token` rule, which `safe_repr()`'s
`scan()` leaves out) and blocks the call unless `ALLOW_CLOUD_OVERRIDE=true`.
`make_provider(..., allow_flagged=True)` (`--allow-flagged`, the `-iok-` trigger) lets one call
through when every finding is in `redaction.SOFT_FINDINGS` (labels, Vietnamese IDs, email, IBAN);
any other finding, including `custom_N`, stays blocked. `Settings` rejects an invalid
`PROMPT_EXTRA_PATTERNS` regex when it loads (`config._regexes`, naming the entry, never the
pattern); every repeat in a built-in pattern is bounded (a test walks each one). The CLI then prefixes the output with
`[promptmend: sent despite: …]` (finding names only) from `GatedProvider.sent_despite`.
It also requires `https` cloud base URLs (plain `http` only to loopback).
`providers.base.post_json()` (the only HTTP call) gives a loopback URL its own `HTTPTransport`,
which makes httpx skip env and system proxies for it; every other URL keeps the proxy.
`scripts/bench_models.py` builds through it too. A new provider that can leave the machine must
return through `_gate()` and be listed in `_leaves_machine()`. `PROMPT_LOCAL_ONLY=true` makes
`make_provider()` refuse such a provider before building it (and `_gate()` refuses too), so it
overrides any `--provider` a trigger passes; `PROMPT_PROVIDER` only sets the bare CLI's default.

### Espanso integration contract

- Every CLI call (the commented-out `-ic-` and `-p-`'s persona var too) is a `type: script`
  var, `params: args: ["__PROMPT_WORKFLOW__", "improve", "--trigger-id", "i", …]`, one
  double-quoted string per argv item (Espanso drops an item that is not a string). Espanso's
  script extension (`espanso-render/src/extension/script.rs`) runs `Command::new(args[0])`
  with the rest as arguments, no shell; `type: shell` would run `powershell -Command` on
  Windows, which cannot run a quoted path followed by arguments (#18). `{{form1.*}}` is filled
  into each item before the call. A nonzero exit **or any stderr output** fails the
  expansion, so `improve`/`persona` must keep stderr empty: the console scripts point at
  `entry.main()`, which for those two silences warnings and `dup2`s the null device onto fd 2
  before importing the CLI (`quiet_trigger()`, also on `python -m promptmend.cli`; in-process
  tests patch `discard_stderr`, never pytest's own fd 2). Never `ignore_error: true`: the exit
  code is left alone, so a crash still fails the expansion visibly. `promptmend espanso deploy`
  (which the install scripts call) substitutes `__PROMPT_WORKFLOW__` with the stable
  absolute path to the installed CLI; there is no `cd`. `tests/test_triggers.py` runs every
  match's args as a real process against `smoke`'s stub (exit 0, empty stderr; on
  windows-latest too).
  Nothing is ever deployed to Espanso's `config/` (`espanso/config/` and `--with-config` were
  removed, #37).
- Every match that runs the CLI (including commented-out ones and `-p-`) sets
  `force_mode: clipboard`, so output is always pasted: Espanso's default backend would type output
  shorter than 100 characters key by key. `tests/test_yaml.py` enforces it.
- Every match (commented-out ones too) has its own `label:` (`PromptMend: …`), shown in
  Espanso's search bar; `tests/test_yaml.py` enforces it.
- `espanso/match/prompts-core.yml` holds static, non-LLM form-based snippets (no CLI call).
- Every match (commented-out ones too) sets `left_word: true`, so a trigger fires only after a
  word separator (space, punctuation, bracket, newline), never inside a word such as
  `a[n-i-1]`; `tests/test_yaml.py` enforces it.
- `-i-` (OpenRouter, `PROMPT_PROFILE`) is the live cloud trigger, `-iok-` is the same call
  with `--allow-flagged`, and `-ip-` is
  the same rewrite with `--tier pro` (reasoning model, `PROMPT_PRO_PROFILE` if set). Only `-il-`,
  `-ilm-` and the commented `-ic-` pass `--profile general`; `tests/test_triggers.py` replays every trigger's real
  args and checks its provider, profile and tier. `-if-` puts an Espanso form
  (choice dropdowns) in front of the pro tier and passes the picks as `--model model@endpoint
  --effort --max-tokens --timeout` via `{{form1.*}}`, which `Settings.with_overrides()` applies.
  Its fields must stay fixed choices, each a whole args item (no free text reaches the CLI),
  each list starting with
  and defaulting to `default` (keep the pro-tier setting), timeouts at most 120 s;
  `tests/test_yaml.py` checks each value against the CLI. `-ic-`
  (Anthropic) stays commented out in `prompts-llm.yml`. The data-protection gate is a heuristic,
  not a guarantee — cloud triggers should only be used where company policy permits.
- Every CLI match (commented-out `-ic-` and `-p-`'s `persona` call too) passes its own literal
  `--trigger-id <id>` right after the subcommand: the trigger without dashes, from
  `recorder.TRIGGER_IDS` (`i iok ip if il ilm ic p`), stored in the usage history as `-i-`.
  Never a `{{form}}` value. No option means `origin=direct`; an unknown id is recorded
  unattributed (`espanso_managed`, trigger NULL) and never fails the run; the option is hidden from `--help` and is never inferred from other flags.
  `tests/test_yaml.py` checks each match's id; `tests/test_triggers.py` replays each and
  checks what is recorded. Changing a match file needs `scripts/update_match_history.py`.

### Releases

`.github/workflows/release.yml` is the only way a version is tagged and released (steps in
CONTRIBUTING's "Releasing"; the one exception is the documented v0.10.0 backfill). It runs on a
manual dispatch with *dry-run* unticked, or on a push to `main` only while the repository
variable `RELEASE_ON_PUSH` is `true`; it releases only the current head of `main` and does
nothing for a version whose Release is already published. A half-failed run can be re-run (it
reuses an annotated tag on the same commit and a draft Release). Only its `release` job can
write to the repository; the `pypi` job after it (only while the variable `PYPI_PUBLISH` is
`true`; environment `pypi`, `id-token: write` only) uploads that run's attested sdist and wheel
to PyPI by trusted publishing with `skip-existing`. Homebrew is a hand step per release:
`scripts/brew_formula.py --sdist <released sdist>`, run on the tag, writes the formula for the
`vlastimilbures/homebrew-tap` repo (`Language::Python::Virtualenv`, one sdist resource per
runtime pin in uv.lock, markers evaluated for macOS/Linux; `tests/test_brew_formula.py`).
WinGet likewise: `scripts/winget_manifest.py --zip <released, attestation-verified zip>`
renders the manifests; submitting them to microsoft/winget-pkgs is a manual, owner-approved
step (never from an agent).
`scripts/release_notes.py` gives the workflow the
version and its notes and refuses a CHANGELOG whose newest `## X.Y.Z - YYYY-MM-DD` heading is not
the `pyproject.toml` version (`## Unreleased` may come first); `tests/test_release.py` runs the
same check. Each Release carries `constraints.txt` (uv.lock's runtime pins), the Windows zip
(the `build-windows` job, windows-latest, `contents: read`, artifact `windows`, attested and
attached by the `release` job; #185) and `install.ps1`
(`scripts/render_installer.py` fills its one `__PROMPTMEND_VERSION__` in the build job;
`tests/test_install_scripts.py`), and the artifact test installs the wheel with it and runs `scripts/check_wheel.py --constraints`.
