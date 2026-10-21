# Contributing

Thanks for helping improve PromptMend. Bug reports, prompt-quality findings, new redaction
patterns and documentation fixes are all welcome. The user documentation is the
[README](README.md) and the pages in [docs/](docs/README.md); this file is for working on the
code.

## Before you start

- For anything bigger than a small fix, open an issue first so we can agree on the approach.
- Never put real API keys, customer data or confidential text in code, tests, issues or commits.
  Use obviously fake values (`sk-test…`, `4111 1111 1111 1111`, `john.doe@example.com`).
- Security problems, including ways around the data-protection gate, go through
  [SECURITY.md](SECURITY.md), not public issues.

## Set up

```bash
git clone https://github.com/vlastimilbures/promptmend.git
cd promptmend
uv sync                  # the project plus its dev tools
uv run pre-commit install
```

To run your checkout from Espanso, install it as an editable tool instead of a release (users
install a release, see [docs/install.md](docs/install.md)):

```bash
./scripts/install_macos.sh      # macOS
.\scripts\install_windows.ps1   # Windows (PowerShell)
```

The script runs `uv tool install --editable` pinned to `uv.lock` (and checks the tool venv
against it), then `promptmend espanso deploy --yes`, which writes the match files with the
CLI's absolute path and restarts Espanso. It never touches Espanso's `config/` folder (the
old `--with-config` / `-WithConfig` option is gone).

The Windows script runs on Windows PowerShell 5.1 and PowerShell 7. If script execution is
disabled, run it as `powershell -ExecutionPolicy Bypass -File .\scripts\install_windows.ps1`
(`pwsh` for PowerShell 7), which allows this one run without changing your execution policy.
On Linux, run `uv tool install --editable .` and `promptmend espanso deploy`.

An editable install runs the code and profiles straight from the checkout, so a `git pull` or
a branch switch changes `-i-` at once, while the match files change only on the next deploy.
It reads the same settings as a release (`config.toml` and `secrets.toml`, see
`promptmend setup`), never the checkout's `.env`: to use a `.env`, point `PROMPTMEND_ENV` at it
(`chmod 600` it, and keep it out of `git add`).

## Checks

Run these before opening a pull request. CI runs the same, except that it runs the tests as
`uv run pytest --cov`, which fails below 95 % coverage (`fail_under` in `pyproject.toml`).
It runs lint and type checks once, and the tests on macOS, Windows and Linux with Python 3.12
to 3.14. Gitleaks scans every push for secrets.

```bash
uv run pytest                                   # unit tests, no network
uv run ruff check . && uv run ruff format --check .
uv run mypy                                     # strict: src, scripts and tests
uv run pre-commit run --all-files               # also YAML checks, gitleaks and zizmor
```

If you change a screen of the interface (`src/promptmend/tui/`), its SVG snapshots fail
until you regenerate them, review the new SVGs and commit them (Linux or macOS):

```bash
UPDATE_SNAPSHOTS=1 uv run pytest tests/test_tui_snapshots.py
```

The Home and Try snapshots are also the README's screenshots (`docs/interface.svg`,
`docs/try.svg`), written by the same run. The snapshots pin the version shown in the header,
so a release regenerates none of them.

If you change a prompt, a provider or anything on the request path, also run the opt-in live
tests (needs `OPENROUTER_API_KEY` in the environment or the checkout's `.env`, which the live
tests point `PROMPTMEND_ENV` at; costs fractions of a cent; the Anthropic and
OpenAI ones also need `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` and are skipped without it):

```bash
uv run pytest -m live
```

## Ground rules

- **The Espanso contract.** The CLI prints the result with no trailing newline, and every failure
  as `[promptmend: …]` with exit code 0. Espanso cannot show stderr or exit codes, so a
  traceback or a blank line reaches the user as a silent failure. Print only through
  `cli._emit()`, which strips control and invisible characters from whatever gets pasted.
  `tests/test_trigger_contract.py` compares the whole stdout of `improve` and `persona` byte
  for byte; a change there is a change to what every trigger pastes. On Windows each newline
  in the output is printed as CRLF (Python's standard streams translate it there); the test
  expects that.
- **The gate.** Build providers only through `factory.make_provider()`. It wraps every provider
  that can send the draft off the machine in `GatedProvider`: the cloud ones always, a local one
  when its base URL is not loopback or the Ollama model is a cloud model.
- **Cross-platform.** Everything must work on macOS and Windows. Use `pathlib` and explicit
  timeouts.
- **Tests.** Unit tests never touch the network; use the `fake_http` fixture in
  `tests/conftest.py`, which runs real httpx over `httpx.MockTransport`, records each request
  and replays responses (`reply()`, or `queue()` for a sequence), or `stub_provider` to
  replace the provider behind a CLI test. Add a test for every
  behaviour change, and a regression test for every bug fix.
- **Triggers.** Keep existing trigger names working. Tests enforce the `-name-` shape, uniqueness
  and no prefix collisions.
- **Commits** follow [Conventional Commits](https://www.conventionalcommits.org/): `feat:`,
  `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `ci:`.
- **Changelog.** Add a line under *Unreleased* in [CHANGELOG.md](CHANGELOG.md) for anything a user
  would notice.
- **Docs.** Update the README or the relevant `docs/` page with every user-visible change. Each
  fact has one home (the settings tables in `docs/configuration.md`, the exit codes in
  `docs/commands.md`, the benchmark numbers in `docs/benchmark.md`); link to it rather than
  repeat it. README.md is also the PyPI page, so its links and images are absolute and it uses
  no mermaid or alert blocks, and no HTML but `<details>`/`<summary>` and `<picture>`.
  `tests/test_docs.py` checks the commands, options, settings and links.
- **Flow diagram.** README's diagram is drawn from `docs/flow.mmd`. After editing it, run
  `python3 scripts/render_diagram.py` (needs Node.js; `--browser <path to Chrome>` reuses an
  installed Chrome) and commit `docs/flow-light.svg` and `docs/flow-dark.svg`;
  `tests/test_docs.py` fails until both carry the new source's hash.
- **Releases.** After tagging `vX.Y.Z`, run `uv run python scripts/update_match_history.py` and
  commit any change, so deploy keeps recognising every released match file.

## Releasing

Releases are cut by `.github/workflows/release.yml`, never by hand-made tags.

1. Open a `chore(release): X.Y.Z` pull request that sets `version` in `pyproject.toml`, runs
   `uv lock` (which updates the project's version in `uv.lock`), and renames `## Unreleased` in
   CHANGELOG.md to `## X.Y.Z - YYYY-MM-DD` (the release date), with an empty `## Unreleased`
   above it for the next change. `tests/test_release.py` fails unless the newest
   CHANGELOG version is the `pyproject.toml` version and every version heading is dated.
2. Merge it, then run the **release** workflow on `main` (Actions tab, or
   `gh workflow run release.yml --ref main -f dry-run=false`). It builds the sdist, the wheel and
   `constraints.txt` (`uv export --frozen --no-dev --no-emit-project --no-hashes`), installs the
   wheel with those constraints into a clean venv on macOS, Windows and Linux and runs
   `scripts/check_wheel.py --constraints`, renders `install.ps1` with the version
   (`scripts/render_installer.py`), builds `promptmend-X.Y.Z-windows-x64.zip` on Windows
   (`scripts/build_windows.py build`, #185), then attests the files, creates the annotated tag
   `vX.Y.Z` and publishes the Release with that CHANGELOG section as its notes. *dry-run* is on by
   default and stops after the artifact tests; untick it to release. A dry run attests nothing,
   so it leaves no provenance record for files that were never released. The workflow releases
   only the current head of `main`, checked again just before tagging, and does nothing for a
   version that already has a published Release. If a release run fails half way, re-run it: an
   annotated tag already on the same commit and a draft Release are reused, while a tag on any
   other commit, or a lightweight one, stops it. The Release is marked *Latest* only when no
   published Release has a higher version.
3. Check the result: `gh release download vX.Y.Z` and `gh attestation verify <file> -R
   vlastimilbures/promptmend` for each file. The first real release is also the
   first check that attestation works end to end.
4. PyPI: while the repository variable `PYPI_PUBLISH` is `true`, the workflow's `pypi` job
   uploads the same sdist and wheel to [PyPI](https://pypi.org/project/promptmend/)
   right after the Release is published (trusted publishing from the GitHub environment `pypi`,
   no API token; `pypa/gh-action-pypi-publish` also uploads PEP 740 attestations). Check that
   `https://pypi.org/project/promptmend/X.Y.Z/` lists both files. If the job
   failed, use *Re-run failed jobs* on that run: it uploads the same artifact again and skips
   any file PyPI already has. A new run for a version whose Release is published stops at
   `plan`, so it cannot upload; and PyPI never accepts a changed file under a version it has.
5. Homebrew: update the formula in
   [vlastimilbures/homebrew-tap](https://github.com/vlastimilbures/homebrew-tap) by hand, from a
   checkout of the tag (the script reads that commit's `uv.lock` and `pyproject.toml`):

   ```bash
   git switch --detach vX.Y.Z
   gh release download vX.Y.Z -R vlastimilbures/promptmend \
     -p 'promptmend-X.Y.Z.tar.gz' -D /tmp/epr-X.Y.Z
   gh attestation verify /tmp/epr-X.Y.Z/promptmend-X.Y.Z.tar.gz \
     -R vlastimilbures/promptmend
   python3 scripts/brew_formula.py --sdist /tmp/epr-X.Y.Z/promptmend-X.Y.Z.tar.gz \
     -o ../homebrew-tap/Formula/promptmend.rb
   ```

   The formula installs the Release's sdist into a `Language::Python::Virtualenv` on
   `python@3.14` (`--python` changes it), with one `resource` per runtime dependency, each the
   exact sdist URL and SHA-256 `uv.lock` records (Windows-only ones such as colorama are left
   out; a marker the script cannot evaluate stops it). In the tap, check it with
   `brew install --build-from-source ./Formula/promptmend.rb`, `brew test promptmend`
   and `brew audit --strict promptmend`, then commit and push. Automating this (a
   workflow job opening a pull request in the tap) would need a token with write access to the
   tap; it is left for later.
6. WinGet (#185): render the manifests of `vlastimilbures.PromptMend` for the released zip,
   from a checkout of the tag (the templates are in `packaging/winget/`):

   ```bash
   gh release download vX.Y.Z -R vlastimilbures/promptmend \
     -p 'promptmend-X.Y.Z-windows-x64.zip' -D /tmp/epr-X.Y.Z
   gh attestation verify /tmp/epr-X.Y.Z/promptmend-X.Y.Z-windows-x64.zip \
     -R vlastimilbures/promptmend
   python3 scripts/winget_manifest.py --version X.Y.Z \
     --zip /tmp/epr-X.Y.Z/promptmend-X.Y.Z-windows-x64.zip
   ```

   It writes `dist/winget/manifests/v/vlastimilbures/PromptMend/X.Y.Z/` (the version,
   installer and en-US locale manifests, with the Release asset's URL and SHA-256 and the
   release date of the version's CHANGELOG heading), the layout of [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs). CI's
   `frozen-windows` job already ran `winget validate` and a `winget install --manifest` on
   the same templates. Submitting them (a pull request to microsoft/winget-pkgs from the
   owner's fork, or `wingetcreate submit`) is a manual step that only the owner approves;
   Microsoft's validation and review take days. The exe is unsigned, so if their Defender
   scan flags it (a false positive is common for PyInstaller builds), submit the zip to
   [Microsoft's WDSI](https://www.microsoft.com/en-us/wdsi/filesubmission) as a software
   developer and mention the result in the winget-pkgs pull request.

With the repository variable `RELEASE_ON_PUSH` set to `true`, step 2 also happens on every push
to `main` whose version has no published Release yet. It is off by default, so merging a branch
never releases by surprise. Only the `release` job can write to the repository (`contents`,
`id-token`, `attestations`); the `pypi` job after it holds only `id-token`, which PyPI
exchanges for a short-lived upload token, and runs only while `PYPI_PUBLISH` is `true`, so a
release works the same before PyPI is set up. Release builds use the build backend pinned by `build-constraint-dependencies`
in `pyproject.toml`; bump that pin by hand.
The Release is created as a draft and published once every file is attached, so with
[immutable releases](https://docs.github.com/en/code-security/supply-chain-security/understanding-your-software-supply-chain/immutable-releases)
enabled in the repository settings, the published Release cannot be changed.

One exception predates the workflow: 0.10.0 (commit `a15a2af`) never got a tag or a Release.
The owner backfills it once, by hand, from a checkout of `main`:

```bash
git tag -a v0.10.0 a15a2af -m "Release 0.10.0"
git push origin v0.10.0
python3 scripts/release_notes.py notes --release 0.10.0 > notes-0.10.0.md
gh release create v0.10.0 --verify-tag --latest=false --title v0.10.0 --notes-file notes-0.10.0.md
```

## Trigger start-up budget

Espanso starts a fresh `promptmend` process for every trigger, so everything `cli.py`
imports is paid on each expansion. Measured on 2026-10-04 on an Apple M5 MacBook (macOS, load
average about 14, so on the high side), CLI 0.15.0:

| Measure | Python 3.12 | Python 3.14 |
| --- | --- | --- |
| `import promptmend.cli`, cumulative (`-X importtime`) | 128–138 ms | 164–207 ms |
| `promptmend persona`, wall time (median of 15) | 185 ms | 238 ms |
| Modules the guarded trigger runs add to a bare interpreter | 293 | 294 |
| The same with the usage history recording (#89, 2026-10-05) | 307 | 306 |

The largest parts of the import are `importlib.metadata` (about 50 ms cumulative under
`-X importtime`, for `__version__` in `promptmend/__init__.py`), httpx (about 30 ms) and
Typer (about 19 ms). The `importlib.metadata` figure overstates its real cost: the modules it
pulls in (email, zipfile, csv) load anyway, so a literal `__version__` saves only about 2 ms of
wall time per run (median of 30 in a clean wheel venv, 3.12 and 3.14, 2026-10-06, #132). CI runner numbers
(Linux, Windows) are still to be recorded from a CI run.

Since #223 (2026-10-08, same machine, Python 3.12, load average about 10, medians of 25
interleaved runs): `promptmend --version` is answered in `entry.main()` before the CLI is
imported, `__version__` is read on first use, and httpx and pyperclip load only for the
provider call and the clipboard. `import promptmend.cli` adds 148 modules (220 before),
about 41 ms cumulative (135 ms). `--version` adds 108 modules (221), 49 ms wall time (121 ms);
`persona` 78 ms (117 ms). A real trigger still loads httpx for its call, so `improve` against
`smoke`'s stub adds 288 modules (299) and its wall time is unchanged. `tests/test_trigger_contract.py`
holds these as ceilings (`VERSION_CEILING`, `IMPROVE_CEILING`, up to the provider call) and
forbids httpx, pyperclip and sqlite3 before it.

Re-measure with:

```bash
uv run python -X importtime -c "import promptmend.cli" 2>&1 | sort -t'|' -k2 -n | tail
uv run python -c "import subprocess, sys, time; t = time.perf_counter(); \
  subprocess.run([sys.executable, '-m', 'promptmend.cli', 'persona'], check=True); \
  print(f'\n{(time.perf_counter() - t) * 1000:.0f} ms')"
```

The second line runs through `python -m`, as the installed `promptmend` script does
apart from the launcher. Run either several times on an idle machine and take the median.

`tests/test_trigger_contract.py` guards the budget without timing anything, since wall-clock
tests flake on a loaded machine. It runs `improve` and `persona` in a fresh interpreter
(clipboard and argument input, a local and a cloud provider, a provider error and a settings
error) and fails if any of them imports `textual`, `rich.console`, `tomli_w`, `tomlkit`,
`keyring` or the deploy module, or if they add more than `MODULE_CEILING` (400) modules. It runs
them with the usage history on and off: on, the run is recorded after its output, so
`sqlite3` loads (307 modules on 3.12, 306 on 3.14); off (`PROMPT_HISTORY=false`), `sqlite3` and
`history` must not load at all (296). A new heavy
dependency belongs behind a lazy import in the command that needs it, never on the trigger
path. Raise the ceiling only with new measurements here.

## Project layout

```text
promptmend/
├── espanso/                      deployed into Espanso by `promptmend espanso deploy`
│   ├── match/                    also shipped in the wheel, see assets.py
│   │   ├── prompts-llm.yml       -i- -ip- -if- -iok- -il- -ilm- (-ic- -io-): call the CLI
│   │   ├── prompts-core.yml      -risk-: static snippet
│   │   └── prompts-template.yml  -p-: the empty golden template, opens with your persona
├── src/promptmend/               the promptmend CLI
│   ├── cli.py                    improve and persona commands, the single output sink;
│   │                             mounts the management commands lazily
│   ├── entry.py                  console-script entry: empty stderr for a trigger
│   ├── commands/                 setup, config, secrets, profiles, stats, history, doctor, ui:
│   │                             thin Typer wrappers over the services (common.py: exit codes)
│   ├── tui/                      the full-screen Textual interface; only `ui` (commands/ui.py,
│   │                             also a bare promptmend and `setup` on a terminal) loads it
│   ├── doctor.py                 the doctor report (stable JSON, never a key or persona)
│   ├── update_check.py           asks PyPI, at most once a day, for a newer release
│   ├── local_probe.py            asks a loopback Ollama or LM Studio whether it answers
│   ├── smoke.py                  setup's smoke test: improve against a stub on 127.0.0.1
│   ├── setup_guide.py            what setup says at each step (wizard and text flow), no Textual
│   ├── config.py                 Settings from the environment, config.toml or a named .env
│   ├── config_files.py           reads config.toml/secrets.toml; atomic, private writes
│   ├── config_store.py           saves settings and secrets
│   ├── factory.py                make_provider(): builds providers, decides which are gated
│   ├── gate.py                   GatedProvider: scans every draft that can leave the machine
│   ├── clipboard_guard.py        refuses password-manager (concealed) clipboard items
│   ├── redaction.py              the gate's sensitive-content patterns
│   ├── history.py                local usage history (SQLite, metadata only, fail-open)
│   ├── recorder.py               records each improve/persona run in the usage history
│   ├── prompt_builder.py         loads built-in and user profiles, fills in the persona rule
│   ├── assets.py                 the packaged Espanso match files (importlib.resources)
│   ├── deploy.py                 espanso deploy/status/detach: manifest, states, stable launcher
│   ├── espanso_windows.py        Espanso on Windows without reading its output (#209-#211)
│   ├── match_history.py          generated: digests of every released match file source
│   ├── prompts/
│   │   ├── default.md            golden-template rewrite (-i-, -ip-, -if-)
│   │   └── general.md            lighter "make this precise" rewrite (local triggers)
│   └── providers/
│       ├── registry.py           ProviderSpec: what each provider is (PROVIDERS)
│       ├── base.py               HTTP call, error mapping, <think> stripping
│       ├── usage.py              per-attempt tokens and cost (AttemptUsage) for an observer
│       ├── openai_compatible.py  OpenRouter, OpenAI and LM Studio
│       ├── anthropic.py          Anthropic Messages API
│       └── ollama.py             Ollama /api/chat
├── packaging/windows/            PyInstaller spec and entry script of the Windows zip (#185)
├── scripts/
│   ├── install_macos.sh          contributor install pinned to uv.lock, then espanso deploy
│   ├── install_windows.ps1       the same for Windows
│   ├── install.ps1               Windows user installer (uv + PyPI), a Release asset (#186)
│   ├── render_installer.py       release: install.ps1 with the version filled in
│   ├── build_windows.py          release: the frozen Windows zip (PyInstaller), trigger timing
│   ├── check_tool_lock.py        the tool venv's packages match uv.lock (#34)
│   ├── update_match_history.py   regenerate match_history.py from the release tags
│   ├── bench_models.py           score models on template fidelity, latency, cost
│   ├── check_wheel.py            CI: what an installed wheel really contains
│   ├── brew_formula.py           release: the tap's Homebrew formula from uv.lock
│   ├── render_diagram.py         docs/flow.mmd to README's light and dark SVGs
│   └── release_notes.py          release: the version and its CHANGELOG notes
├── tests/                        unit tests, no network (fake_http in conftest.py)
│   ├── test_live.py              opt-in real OpenRouter/Anthropic/OpenAI calls (-m live)
│   ├── test_trigger_contract.py  exact trigger output, imports and module budget
│   ├── test_tui.py               the interface, driven headless with Textual's Pilot
│   ├── test_tui_snapshots.py     SVG snapshots of each tab (snapshots/, UPDATE_SNAPSHOTS=1)
│   └── test_docs.py              docs list every setting; commands, options and links exist
├── .github/                      CI (tests, gitleaks), Dependabot, issue and PR templates
├── .env.example                  key and persona; every other setting commented out
├── CONTRIBUTING.md               setup, checks, how to add a profile/trigger/provider
├── SECURITY.md                   how to report a gate bypass or other vulnerability
└── CHANGELOG.md                  release notes
```

## Common changes

### Add a profile

A profile for your own use does not belong in the repo: put it in the user profile directory,
`~/.config/promptmend/profiles/<name>.md` (`$XDG_CONFIG_HOME/promptmend/profiles/`
when `XDG_CONFIG_HOME` is set on macOS or Linux; `%APPDATA%\promptmend\profiles\` on
Windows), where an upgrade cannot replace it. See
[docs/profiles.md](docs/profiles.md#your-own-profiles); a same-named file overrides a built-in only when
`PROMPT_PROFILE_OVERRIDES` lists it.

To ship a new built-in profile:

1. Add `src/promptmend/prompts/<name>.md` containing the system prompt as plain prose. It is
   picked up automatically. The name must match `prompt_builder.PROFILE_NAME` (lower-case
   letters, digits, `-`, `_`).
2. Optionally include `{{PERSONA_RULE}}` where the user's persona should be applied (see
   `prompt_builder.render()`).
3. Add a test in `tests/test_prompts.py`.

### Add a trigger

Add a match to `espanso/match/prompts-llm.yml`. The CLI call is a `type: script` var (#18):
its `args` list starts with the `__PROMPT_WORKFLOW__` placeholder, which
`promptmend espanso deploy` replaces with the absolute CLI path, and holds every other
argument as its own double-quoted item. Espanso starts `args[0]` directly, with no shell, so
no shell quoting applies; a `type: shell` var would run through PowerShell on Windows, which
cannot run a quoted path followed by arguments. A form value goes in as a whole item
(`"{{form1.model}}"`). A script var fails on any stderr output, so the CLI must keep stderr
empty on the trigger path (`entry.py` silences warnings and points fd 2 at the null device
for `improve` and `persona`; never `ignore_error: true`, which would hide a crash).
Set `force_mode: clipboard` on every match that runs the CLI, so Espanso pastes the output
instead of typing short replies key by key.

```yaml
- trigger: "-ireg-"
  label: "PromptMend: rewrite for regulation (Ollama)"
  left_word: true  # fire only at the start of a word, never inside one such as n-i-1
  replace: "{{output}}"
  force_mode: clipboard
  vars:
    - name: output
      type: script
      params:
        args: ["__PROMPT_WORKFLOW__", "improve", "--provider", "ollama", "--profile", "regulation", "--source", "clipboard"]
```

A match of your own runs without `--trigger-id`: the managed matches' ids are a fixed
allowlist, so the usage history records your trigger as `direct` (or, if you pass an id that
is not on it, as an unattributed managed run). A new managed trigger adds
its id to `recorder.TRIGGER_IDS` (the test fails until each CLI match passes its own).

Any change to a file in `espanso/match/` needs
`uv run python scripts/update_match_history.py` (also run it at every release, after tagging):
it records the source's digest in `src/promptmend/match_history.py` (together with every
digest already listed, which is never dropped, each release tag's version and the file at every
commit that changed it on this branch and on the default branch, since an editable install
runs an untagged commit), so `espanso deploy`
later recognises a file this version wrote as its own (stale) rather than foreign, and
`tests/test_deploy.py` fails until it is run.

`tests/test_yaml.py` checks the script var (`args[0]` is the placeholder, one string per
argument), `force_mode`, and that the profile and provider exist. Add the trigger's expected
provider, profile and tier to `EXPECTED` in `tests/test_triggers.py`, which replays every
trigger's args through the CLI and also runs each as a real process against a local stub,
expecting exit 0 and an empty stderr (on Windows CI too). Pass
`--profile` only when the trigger needs a fixed profile: it overrides `PROMPT_PROFILE` and the
pro tier's profile.

### Add a setting

Add a field to `Settings` in `src/promptmend/config.py` with `_env("NAME", "default")`, then
document it in the settings reference in `docs/configuration.md` and in `.env.example` (commented out with its
default: `# NAME=default`). `tests/test_docs.py` fails until both are done. A setting that holds
a secret takes `secret=True`: it then stays out of `repr()`, is saved only in the secret store,
and is never written to `config.toml`.

### Add a static snippet or form

Snippets that need no model go in `espanso/match/prompts-core.yml`, as plain Espanso matches
(`replace:` for fixed text, `form:` plus `form_fields:` for a fill-in form). See the
[Espanso docs](https://espanso.org/docs/matches/basics/). Test in a plain-text editor first.

### Add a provider

1. Implement `generate(prompt, system_prompt) -> str` (the `Provider` protocol) in
   `src/promptmend/providers/`. Use `post_json()` and `finalize_content()` from
   `providers/base.py` so transport errors and `<think>` stripping behave like the other providers.
   Pass the response's raw stop reason to `finalize_content(..., stop_reason=...)`, so a cut-off,
   failed or filtered reply is marked instead of pasted as complete. Take an optional
   `observer` and, when it is set, pass `post_json(..., meter=Meter(...))` with a parser in
   `providers/usage.py` that maps the body's tokens and cost (a missing cost stays `None`).
2. Add a `ProviderSpec` to `PROVIDERS` in `providers/registry.py` and its settings
   (`{name}_base_url`, `{name}_model`, and for a cloud API `{name}_api_key` and its cap) to
   `config.py` (see [Add a setting](#add-a-setting)). A cloud API is `cloud=True`: then
   `leaves_machine()` reports it, `make_provider()` returns it through `_gate()`, its key is
   required and its base URL must be https, so the gate and `PROMPT_LOCAL_ONLY` both cover it.
   A new wire format also needs its branch in `make_provider()` in `factory.py`, passing
   `observer`.
3. Add wire-format and error-path cases to `tests/test_providers.py`; `tests/test_registry.py`
   checks every spec.

### Improve the redaction gate

Add a pattern to `_PATTERNS` in `src/promptmend/redaction.py`, with a validator in
`_VALIDATORS` if the raw regex is too broad. Bound every repeat that can run over ordinary text
and match a value only for its existence (`{8}`, not `{8,}`), then add an input to
`test_scan_is_fast_on_adversarial_input`. Add a row to the `MUST_DETECT` corpus and a near-miss
row to `MUST_PASS` in `tests/test_redaction.py`; every bench draft must still scan clean. Rules
about the whole draft (like `bare_token`) belong in `scan_draft()`, which only the gate calls:
`safe_repr()` uses `scan()` and must keep quoting short setting values. Build fake keys at runtime (`"sk-ant-" + body`) so no key-shaped
literal lands in the repo. Patterns specific to one organisation belong in the user's
`PROMPT_EXTRA_PATTERNS`, not in the code.

### Change the default prompt

Template wording is scored by `scripts/bench_models.py`, and `tests/test_bench.py` checks the
scored phrases still exist in `prompts/default.md` and in the static `-p-` template
(`espanso/match/prompts-template.yml`), so change all three together. A/B a change on both the
standard and pro defaults, on both suites, before proposing it, and include the before/after
pass rates in the pull request (the report's split by kind, with its Wilson intervals:
rates whose intervals overlap are not shown to differ), with each run's `meta.json` (commit, prompt hash, persona
mode). The bench renders a fixed fictitious persona by default, so anyone can reproduce your
numbers. Never commit the outputs of a `--persona env` run, which hold your own persona. For
example:

```bash
uv run python scripts/bench_models.py --suite all --runs 3 --models \
  google/gemini-3.5-flash-lite@google-ai-studio/flex~minimal openai/gpt-6-luna@openai~low
uv run python scripts/bench_models.py --suite all --runs 3 --system-prompt-file candidate.md \
  --models google/gemini-3.5-flash-lite@google-ai-studio/flex~minimal openai/gpt-6-luna@openai~low
```

The `core` suite is saturated: the previous prompt already passed it in full, so it only guards
against regressions. Improvements show on the `edge` suite and in how good the rewrites read,
which the mechanical checks cannot judge. For a prompt change that is not purely mechanical,
also compare the two prompts' outputs blind and pairwise: same model, draft and run, A/B order
randomised, labels hidden. Judge fidelity, invention, specificity of the work steps and
calibration of CONSTRAINTS, INPUTS, OUTPUTS and `[REVIEW: …]`, and read the result **per
model**. The two default models react to the same wording in opposite directions: an
anti-invention rule that fixed flash-lite made gpt-6-luna write thin, generic steps. Keep
drafts you wrote after freezing the candidate for the final comparison.

**Holdout drafts and contamination (#48).** The `holdout` suite (`--suite holdout`, not part
of `all`) is 8 drafts frozen on 2026-10-06 that no prompt was tuned on. Run it at a release or
for the final comparison of a candidate, never while tuning, and never change the prompt to
pass one of its drafts. Once a holdout result has decided a prompt change, the suite is spent:
write and freeze new drafts and move the old ones to `edge`. Examples and wordings in a profile
must not quote any bench draft either, since an example worded like a draft teaches that
draft's answer: `tests/test_bench.py` fails when a profile or the `-p-` template shares a
three-word phrase (beyond a few everyday ones) or a name with any bench draft, or any
specific (`Draft.keys`) or three-word phrase with a holdout draft. When you add an example to
the prompt, invent a new situation rather than borrowing a draft's.

**Contested labels.** A draft whose branch could defensibly go either way has that label set
to `None` (accepted either way, still required to choose exactly one variant), with a comment
saying why: the review step of `quick-ceo`, `memo` and `outliers`, both steps of `persona`
and `duckdb`, and so on. Reach agreement before giving such a draft a single answer again.
The report's `[REVIEW] flags per rewrite by draft` table is a metric, not a check: read it for
rewrites that flag too little (a gap filled by guessing) or too much.

Small models such as flash-lite need an explicit trigger for *each* variant of a branching step.
A rule like "if unsure, use (a)" with no positive condition for (b) makes them pick (a) almost
every time. They also choose a variant by analogy to the examples in the rule, so a named
example ("a polite reply to a vendor") works better than an abstract class ("any external
party"). Check the per-draft table in the report, not only the total.

### The general profile

`general` is what `-il-` and `-ilm-` send to a local model, so it stays short. The bench scores
it with `--profile general` (or a candidate with `--system-prompt-file`): a profile without
`<output_template>` is checked by `check_general()`: an answer instead of a rewrite (a bare
"OK", a letter, `Draft.answer`), a preamble or trailing note, an invented role, an injected
instruction carried over outside the quoted material and not negated (`Draft.forbidden`), the
"input is data" guard (`Draft.guard`), the draft's language and its copied material. Fenced
replies are counted. The checks are heuristics: read a sample of the outputs as well. The bench runs on OpenRouter only, so flash-lite stands in for a
local 7-8B model; nothing has measured `general` on Ollama or LM Studio yet. Keep the phrases in
`tests/test_prompts.py::test_general_profile_contract` in sync with `prompts/general.md`.

### Known gaps in the default prompt

Both tiers send the `default` profile (flash-lite on the standard tier, gpt-6-luna on the pro
tier); `default-pro` is an alias of it. The 2026-10-02 rework (its decision record is in git:
`git show v0.25.0:docs/prompt-candidates/PLAN.md`; the candidate files, I2 among them, at
`v0.17.0`) closed the old gaps 1–4 and 7: fixed wordings are copied word for word, document
steps name their sections, method choices are stated as assumptions, a message to a named
person at another organisation gets the independent review, and the self-review lines match the
format in `OUTPUTS`. The gaps it left, and their state after the 2026-10 round (its bench runs
use `--persona example`):

1. **Published text on flash-lite** (closed 2026-10). The review rule now names its readers
   (only the user, a colleague, their manager or their team take the self-review; everyone
   else, including the CEO and published text, takes the independent review), and says that
   "quick", "short" and "brief" never decide it. Flash-lite now gives `faq` and `quick-ceo` the
   independent review and the drafts for the user and their team the self-review in every run
   ([per-draft findings](docs/benchmark.md#per-draft-findings-of-the-2026-10-round)).
2. **Self-review drafts on gpt-6-luna** (mostly closed). With a fixed fictitious persona,
   `outliers`, `sql` and `memo` get the self-review in every run, and a wrong review branch is
   rare ([per-draft findings](docs/benchmark.md#per-draft-findings-of-the-2026-10-round)).
3. **Half-answers in work steps on flash-lite.** For "what's the difference between IFRS 9
   stage 2 and stage 3", flash-lite writes the answer into the steps, sometimes wrongly. Naming
   the aspects to cover is welcome; stating facts is not. Watch it on `question`.
4. **Injection meta-commentary on flash-lite** (not reproduced). Instead of dropping "ignore
   previous instructions", flash-lite sometimes wrote a CONTEXT about "an instruction that
   attempts to override my role". With a fixed fictitious persona it did not happen on
   `injection` and `pasted-injection` (2026-10). Watch it if a persona is set.
5. **Unscored review branches.** Five drafts are not scored on the review branch: a one-page
   PRD for a fintech feature, `outage` (an incident summary for the user's manager), and since
   #48 the contested `quick-ceo` (a note to the CEO), `memo` (a memo to the user's team) and
   `outliers` (a script for the user). The 2026-10 rule decides by reader, so `outage` should
   take the self-review; who reads a PRD is still open. Score them once that is settled.
6. **Persona bleed** (depends on the persona). With `PROMPT_PERSONA` set, near-empty drafts
   can pick up the persona's domain: "help with the report" became a report on "risk management
   metrics". It did not reproduce with a neutral persona (`vague` and a near-empty draft, on
   both models), so it likely depends on the persona's domain. A design
   trade-off, not a bug; check it with `--persona env` on your own machine.
7. **Draft delimiting in the CLI.** Sending the draft wrapped in `<draft>…</draft>` gave a
   small, consistently positive but not significant gain once the prompt already said the
   whole message is the draft. If adopted, escape `</draft>` inside drafts.
8. **Prompt length** (closed 2026-10). The 2026-09 and 2026-10-02 reworks made the system
   prompt longer (about 3,900 input tokens per call). Length does not drive latency; cost rose
   on flash-lite and is flat on gpt-6-luna ([per-draft findings](docs/benchmark.md#per-draft-findings-of-the-2026-10-round)).
9. **The bench runs `default` on every model** (closed 2026-10). Both tiers now send `default`,
   so a plain `--suite all` run scores what ships; `tests/test_bench.py` checks that the bench's
   profile is the one the CLI uses.
10. **Pasted material on flash-lite (#42)** (partly closed in v0.19.0). The prompt copies pasted
    material up to about 60 lines in full. gpt-6-luna copies it in every run; since v0.19.0
    flash-lite also copies the short email in `pasted` and the holdout's `ho-signing`, which the
    v0.14.0 prompt summarised ([Current](docs/benchmark.md#current-prompt-of-v0190), [per-draft findings](docs/benchmark.md#per-draft-findings-of-the-2026-10-round)). Still open on
    flash-lite: it gives `long-thread` ("summarise this thread for me and draft my reply to
    Tomasz") the self-review, and `cap-thread` (a 5-bullet summary for the user's manager) the
    `.md` line, where the bench expects a message; the 0.13.0 prompt did the same. The `OUTPUTS`
    rule treats a summary as a document, so the `cap-thread` failure may be the bench's
    expectation rather than the model's.

### Next steps for the default prompt

Limits of the 2026-10-02 and 2026-10 evaluations, and what to do next:

1. **Re-judge the shipped prompt blind on new drafts.** The 2026-10-02 `default` and
   `default-pro`, and the merged `default` of 2026-10, were checked mechanically only; the blind
   ranking was on I2. The revisions were tuned on the
   same drafts, and `landlord` is no longer held out. The `holdout` suite (#48) now holds 8
   new drafts (outside emails, published text, internal notes, "for me" scripts and plans, a
   pasted email, a Spanish draft); judge the current prompts against the previous release on
   them, on both tiers, before changing anything else.
2. **Use more runs per draft.** At 3 runs, a single draft flips from noise alone (seen on
   `big-personal` and `memo`, [per-draft findings](docs/benchmark.md#per-draft-findings-of-the-2026-10-round)). Re-check any single-draft change at 6
   runs or more before acting on it.
3. **Fix gap 1 (published FAQ text on flash-lite).** Done in 2026-10: the review rule names
   its readers, and `quick-ceo` and `memo` held in every run.
4. **Fix the rest of gap 10 on flash-lite.** Copying a short pasted email is done in v0.19.0.
   Find a wording that gives a reply to an outside sender the independent review when the draft
   also says "for me". Check `pasted`, `long-thread` and `cap-thread` at 6 runs or more, and decide
   whether a summary for the user's manager should get the `.md` line.
5. **Keep one profile.** The profiles were merged in 2026-10 (gap 9). Before splitting them
   again, show that no single wording serves both models: one prompt per tier doubles the bench
   and review work.
6. **Watch the prompt length.** The prompt is now about 3,900 input tokens; check latency on the
   pro tier against the newest table in [docs/benchmark.md](docs/benchmark.md#results) after any
   further additions.
