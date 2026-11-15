# Repository instructions

`CLAUDE.md` at the repository root is the source of truth for this repository's architecture,
commands and rules; read it before changing code. `CONTRIBUTING.md` has the checks and ground
rules. The invariants below must never be broken:

- The CLI prints only through `trigger._emit()` (no trailing newline); every failure is a
  `[promptmend: ...]` marker on stdout with exit code 0, never a traceback.
- Build every provider with `factory.make_provider()`, which wraps any call that can leave the
  machine in the data-protection gate. The only overrides are `ALLOW_CLOUD_OVERRIDE=true` and
  `--allow-flagged` (one call, soft findings only); never add a code path around the gate.
- Every Espanso match that runs the CLI sets `force_mode: clipboard`, and every match sets
  `left_word: true`.
- Every CLI call is a `type: script` var whose `args` list starts with `"__PROMPT_WORKFLOW__"`,
  one quoted string per argument; never a `type: shell` `cmd:` (Espanso runs it through
  PowerShell on Windows, #18). The CLI keeps stderr empty on `improve` and `persona`, since a
  script var fails on any stderr output. Nothing is ever deployed to Espanso's `config/`
  folder.
- Keep existing triggers working and every feature cross-platform (macOS and Windows); a
  renamed or removed trigger needs a migration note and a test.
- Never put API keys, customer data, credentials or confidential prompts in source, tests,
  logs or commits.
- Tests stay offline: never call external APIs from unit tests; mock the HTTP client.
- Update README.md or the relevant `docs/` page, and the CHANGELOG `## Unreleased` section,
  with every user-visible change.
- Do not run the paid bench (`scripts/bench_models.py`), `pytest -m live`, the installers or
  `espanso` commands unless asked.
