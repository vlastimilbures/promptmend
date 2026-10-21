# CLAUDE.md

This file holds the contracts and rules an agent must keep. The details live in the code:
read a module's docstring (and the tests named here) before editing it, and docs/ for
user-facing behaviour.

## What this is

A Python CLI (`promptmend`, PromptMend; the old `prompt-workflow` alias was removed in #275)
invoked by Espanso text-expansion triggers to rewrite a clipboard/stdin
draft into a more precise prompt via a local or cloud LLM. Espanso match files in
`espanso/match/` call the CLI from `type: script` vars (an argv list, no shell) and paste back
stdout. Besides the triggers (`improve`, `persona`) there are management commands
(`src/promptmend/commands/`) and a Textual interface (`promptmend ui`, `src/promptmend/tui/`).

## Commands

Contributor install: `scripts/install_macos.sh` / `scripts/install_windows.ps1` (an editable
`uv tool install` plus `espanso deploy`; agents never run them, see below).

Checks (CONTRIBUTING "Checks"; CI runs `uv sync --locked`, then these, with `pytest --cov`
held to `fail_under = 95`):

```bash
uv run pytest --cov                             # unit tests, offline
uv run ruff check . && uv run ruff format --check .
uv run mypy                                     # strict: src, scripts, tests, packaging
uv run pre-commit run --all-files               # also YAML checks, gitleaks and zizmor
```

## Rules for agents

- `scripts/bench_models.py` and `uv run pytest -m live` (`tests/test_live.py`) call the paid
  OpenRouter API: run them only when asked.
- Never run the installers (`scripts/install_macos.sh`, `scripts/install_windows.ps1`,
  `scripts/install.ps1`), `promptmend espanso …` (`deploy`, `status`, `detach`),
  `setup --deploy` or `espanso` itself: they read or rewrite the user's live Espanso match
  files, and a deploy restarts Espanso.
- Tests stay offline: no external API calls; mock HTTP (`fake_http` in `tests/conftest.py`).
  conftest also refuses every external command (`deploy.run_command`), the PyPI update check
  and the local-server probe, stubs the clipboard guard, and points `PROMPTMEND_ENV`,
  `HOME`, `XDG_*`, `APPDATA` and the project root at temp dirs. Keep new tests inside those fences.
- Every user-visible change gets a CHANGELOG `## Unreleased` entry and an update of the README
  or the relevant docs/ page.
- After editing `espanso/match/`, rerun `scripts/update_match_history.py` (it regenerates
  `match_history.KNOWN_SOURCES`; `tests/test_deploy.py` checks it).
- The variant wordings in `prompts/default.md`, the `-p-` template in
  `espanso/match/prompts-template.yml`, `scripts/bench_models.py` and `tests/test_bench.py`
  change together. Before shipping a `default` change, ask the owner to re-run the benchmark
  (`--suite all --system-prompt-file`) on both default models. `general.md` is scored with
  `--profile general` and pinned by `tests/test_prompts.py::test_general_profile_contract`.
  Known weaknesses are in CONTRIBUTING.md.
- After a `tui/` screen change, run `UPDATE_SNAPSHOTS=1 uv run pytest
  tests/test_tui_snapshots.py`; the Home and Try snapshots are also `docs/interface.svg` and
  `docs/try.svg`.
- A test that runs `entry.main()` for `improve`/`persona` in process patches
  `entry.discard_stderr`; never let it point pytest's own fd 2 at the null device.
- Never tag, release, publish to PyPI, push the Homebrew formula or submit WinGet manifests
  (see Releases).

## Docs map

README.md is the landing page and the PyPI long description: absolute links and images only;
HTML only `<details>`/`<summary>` and `<picture>`/`<source>`/`<img>`; no mermaid block or
`> [!` alerts. Its flow diagram is `docs/flow.mmd`, rendered by `scripts/render_diagram.py`
into `docs/flow-light.svg`/`flow-dark.svg`; rerun it after editing the `.mmd` (the SVGs carry
its hash).

The depth lives in `docs/` (index `docs/README.md`): `install`, `usage`, `commands` (the only
home of the exit codes), `configuration` (the only home of the settings tables, matched
against `config.env_names()`), `profiles`, `privacy`, `interface`, `troubleshooting`,
`benchmark` (the only home of benchmark numbers). Each page: `# Title`, an intro paragraph,
sections, `## See also` last.

`tests/test_docs.py` checks every `promptmend …` invocation and `--flag` against the Click
tree, every link and anchor, README's PyPI safety and each page's skeleton (`DOC_PAGES`).

## Espanso integration contract

- Every CLI call (commented-out matches too) is a `type: script` var, `params: args:
  ["__PROMPT_WORKFLOW__", "improve", "--trigger-id", "i", …]`, one double-quoted string per
  argv item. Espanso runs `args[0]` directly, no shell; never `type: shell` (PowerShell on
  Windows cannot run a quoted path with arguments). `deploy` replaces `__PROMPT_WORKFLOW__`
  with the stable absolute path of the installed CLI; there is no `cd`.
- A nonzero exit **or any stderr output** fails the expansion. So `improve`/`persona` keep
  stderr empty (`entry.main()`'s `quiet_trigger()` silences warnings and points fd 2 at the
  null device before importing the CLI), catch every failure, print a `[promptmend: ...]`
  marker on stdout and exit 0. Never `ignore_error: true`: a crash must still fail visibly.
- Everything printed goes through `cli._emit()`. Markers are pasted into the focused app, so an error that quotes a value goes through
  `redaction.safe_repr()` / `redact_words()`.
- `tests/test_yaml.py` enforces, for every match (commented-out ones too): `force_mode:
  clipboard` on every CLI match, its own `label:` (`PromptMend: …`), `left_word: true`, and a
  literal `--trigger-id <id>` right after the subcommand from `recorder.TRIGGER_IDS`
  (`i iok ip if il ilm ic io p`), never a `{{form}}` value.
- The `-if-` form's fields stay fixed choices, each a whole args item (no free text reaches
  the CLI), each list starting with and defaulting to `default`, timeouts at most 120 s.
- `espanso/match/prompts-core.yml` holds static snippets (no CLI call); `-ic-` (Anthropic)
  and `-io-` (OpenAI) stay commented out. `tests/test_triggers.py` replays every match's args
  as a real process against `smoke`'s stub.
- Nothing is ever deployed to Espanso's `config/`; `deploy` writes only into
  `<espanso>/match/`, never overwrites a file the user edited without an explicit choice, and
  routes every external command through `deploy.run_command`.

## Data-protection gate

- `factory.make_provider()` is the only place providers are built (the CLI, the interface and
  `scripts/bench_models.py` all use it). It wraps everything that can send the draft off the
  machine (`leaves_machine()`: OpenRouter, Anthropic and OpenAI always; Ollama/LM Studio on a
  non-loopback URL or an Ollama `cloud` model) in `gate.GatedProvider`, which runs
  `redaction.scan_draft()` and blocks a flagged draft unless `ALLOW_CLOUD_OVERRIDE=true`.
- **A new provider that can leave the machine must return through `_gate()` and be reported by
  `leaves_machine()`:** a cloud API is a `cloud=True` spec in `providers/registry.PROVIDERS`
  (always gated, key required, https only; `tests/test_registry.py`).
- `--allow-flagged` (the `-iok-` trigger) lets one call through only when every finding is in
  `redaction.SOFT_FINDINGS`; the output then starts with a `sent despite` marker naming the
  findings (names only, never the text).
- Cloud base URLs must be `https` (plain `http` only to loopback). `PROMPT_LOCAL_ONLY=true`
  refuses any remote provider, whatever `--provider` a trigger passes. `PROMPT_GATE_LOCAL=true`
  gates loopback providers too without making them count as remote.
- The smoke and Try-tab stub runs get `smoke.PLACEHOLDER_KEY`, never the real key.
- `providers.base.post_json()` is the only HTTP call that carries a draft (doctor's PyPI and
  local-server checks make their own GETs); a loopback URL bypasses env/system
  proxies. The gate is a heuristic, not a guarantee (say so in docs).

## Settings

- `config.Settings` is a frozen dataclass; values are parsed strictly and a bad one raises a
  `ValueError` naming the variable (never quoting a secret). API keys are `secret` (out of
  `repr()`), and only `secret_names()` live in the secret store, never in `config.toml`.
- `ConfigLayers.resolve()` merges, lowest first: built-in default < saved settings
  (`config.toml`; `PROMPTMEND_ENV` names a single `.env` instead, the only `.env` ever read) <
  the secret store < the real environment; per-call overrides come after
  (`Settings.for_call()`/`with_overrides()`). It never reads the cwd, never writes
  `os.environ`, and takes only `env_names()` keys (a `.env` cannot set `HTTPS_PROXY` or
  anything else). Strict mode (the triggers, the Try tab) fails closed with a marker; repair
  mode (`strict=False`, the management commands) reports findings and falls back a layer,
  and reports a `.env` left in the config folder (`config.unread_env_file()`, #275).
- Legacy mode: with `PROMPTMEND_ENV` set (the conftest default) there is no `config.toml` and
  no secret store, and `config set`/`secrets set` refuse.
- Adding a setting: a field and parser in `config.py`, a row in docs/configuration.md, a line
  in `.env.example` and an entry in `tui/settings_model.SETTINGS`; `tests/test_docs.py` and
  `tests/test_tui_settings_model.py` fail without the last three.
- File writes go through `config_files.write_atomic()` (mode 600 / owner-only DACL, no
  plaintext fallback).

## Trigger-path import contract

`improve` and `persona` start on every keystroke trigger, so their import set is pinned by
`tests/test_trigger_contract.py` (`FORBIDDEN`, ceilings). New heavy imports
go inside the function that needs them; `cli._LAZY_COMMANDS` loads a management command only
when it runs. `entry.self_command()` is the argv prefix for any child that runs the CLI again.

## Usage history

- Local SQLite (`history.py`), **metadata only**: never prompt, response, body or key text.
  Columns are exactly `OPERATION_COLUMNS` and `ATTEMPT_COLUMNS`; `tests/test_history.py`
  holds the allowlist. Add a column only through a new `_MIGRATIONS` step (forward only).
- Money is exact decimal text plus a unit, never REAL; a missing cost is `unknown`, never 0.
- `record()` never raises and stays within `_BUDGET`; recording never changes stdout, the exit
  code or a retry. With `PROMPT_HISTORY=false` the trigger path never imports `history` or
  `sqlite3`.

## Management commands

- Exit codes 0 ok, 1 failed, 2 usage, 3 needs a terminal, 4 doctor/validate found problems
  (`commands/common.py`; docs/commands.md "Exit codes" is their only doc home).
- Errors go to stderr via `@guard` (never a traceback); never prompt without a TTY
  (`stdin_is_tty()`); a key comes only from stdin or `getpass` (`read_secret()`), never from
  an option or argument; `config set` refuses secret names and key-like values.
- `setup` on a terminal opens the wizard (its flags only prefill or skip steps; nothing is
  written without a press); `--non-interactive`/`--plain` asks nothing, deploys only with
  `--deploy`, and always discloses the usage history (D-HIST-0).
- `tests/test_commands.py` walks the whole command tree (no secret-named option, no key saved
  from a value, no traceback on broken config): add a new command to its `_commands()`.
- `doctor` changes nothing except replaying the history spool and refreshing the
  update-check cache; `doctor --json` keeps
  `schema_version` 1 (only add check ids/keys; `previous_install` was removed in #246 by owner
  decision, its PATH warning kept as `path_shadow`; since #275 `folders` keeps its `legacy`
  and `conflicts` keys, always empty).

## Interface (`tui/`)

- `textual` is imported only under `tui/` (`tui/teach.py` has none), reached only through
  `commands/ui.py`.
- Each action calls the same service as its headless command and adds no logic. Keys show only
  as set/not set. Destructive actions go through a dialog whose Cancel has the focus.
- No provider call except the explicit "Test call" and a confirmed Try tab run; the Try tab
  never touches the clipboard.
- Every button names its headless command: `tui/teach.BUTTONS` (or `NO_COMMAND`);
  `tests/test_tui_teach.py` parses each against the Click tree. New code that shows a
  user-typed value in a command passes it through `common.looks_like_a_key()` and shows
  `teach.WITHHELD` instead of a match.

## Releases

`.github/workflows/release.yml` is the only way a version is tagged and released (CONTRIBUTING
"Releasing"). `scripts/release_notes.py` refuses a CHANGELOG whose newest `## X.Y.Z -
YYYY-MM-DD` heading is not the `pyproject.toml` version (`## Unreleased` may come first). PyPI
upload, the Homebrew formula (`scripts/brew_formula.py`) and the WinGet manifests
(`scripts/winget_manifest.py`) are owner steps: never submit to microsoft/winget-pkgs or the
tap from an agent.
