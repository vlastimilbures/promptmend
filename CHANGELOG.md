# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## Unreleased

### Changed
- The `improve` refusal in `promptmend shell` now names the Try tab (7) of `promptmend ui`.
- The interface reloads faster after each change: it asks Espanso for its folder and runs
  each launcher-lookup command once per reload instead of up to three times (#242).
### Fixed
- Setup's Done step no longer says "Match files: not installed" when the only files that
  differ are ones you edited and kept, which the Espanso step already says it leaves alone;
  it says how many of them were kept.
- Setup's Next no longer gets stuck on the provider, profile and API keys steps while
  `PROMPTMEND_ENV` is set: it says the choice is not saved (settings stay in that file) and
  moves on; the keys step and the Done checklist say to add each key to that file.
- Setup logs `config unset` when picking the default removed the setting, instead of a
  `config set` it did not run.
- Home's "Previous install…" stays shown until your settings are saved in `config.toml`, so
  "Enter a path…" works after "Skip this checkout" and when nothing was found.
- The Profiles tab's "Migrate from checkout" asks for the checkout's folder when none was
  found, instead of naming a `--checkout` flag the interface cannot pass.
- Saving a setting or a key no longer replaces a symlinked `config.toml` or `secrets.toml`
  with a plain file: the file the link points to is written, so a copy kept in a dotfiles
  repo keeps getting the changes (#236).
- `improve --copy` no longer loses the rewrite when the copy fails: the error marker is
  followed by the rewrite, as with `PROMPT_OUTPUT=clipboard`, and the history records
  `clipboard_failed`.
- A reply in a code fence that the model then cut off (output limit) or stopped early now
  has its fence removed too, when the closing fence arrived before the stop; the note that it
  stopped early stays at the end. `--copy` copies the rewrite without that note, as
  `PROMPT_OUTPUT=clipboard` does.
- An unexpected error's marker, or a library's `ValueError`, no longer pastes a key its
  message quotes: such a word is described (`<redacted, N chars>`) instead, and the whole
  message when the secret spans several words (`Bearer …`, `password: …`, a spaced card
  number). A failed copy's message is treated the same way.
- `improve --source stdin` reads at most one character past the 50,000-character limit, so
  piping a huge file is refused at once (`Input is too long (over 50000 chars)`) instead of
  being loaded whole first.
- `promptmend config rollback` puts each `.env` back before it moves `config.toml` and
  `secrets.toml` into the backup, so a trigger that fires during the rollback, or a `.env`
  that cannot be put back, still runs on the saved settings and keys instead of the defaults
  (#238).
- When `promptmend config migrate` fails and cannot undo one of its own writes, the error
  now names the reason the migration failed, then `could not undo: <file>`, instead of only
  the error of the undo step. The migration record is then kept, and the message asks to
  run `promptmend config rollback`, which removes what the migration left (#238).

## 0.24.0 - 2026-10-08

### Added
- `promptmend setup` in a terminal now opens a full-screen setup, one step at a time:
  welcome, earlier settings (only when there is a `.env` or an earlier checkout install),
  provider and profile picked from a list that says what each is and which triggers use it,
  the API keys, Espanso's match files in words (not installed yet, up to date, you edited it)
  with an install button, the stub test, and a checklist of what is left with how to try
  `-i-`. Home's "Setup…" opens it, and a bare `promptmend` opens it by itself once while
  nothing is set up. `setup --plain` (or any of `--provider`, `--profile`, `--migrate-from`,
  `--deploy`/`--no-deploy`, `--no-smoke-test`) asks line by line as before;
  `--non-interactive` is unchanged.

### Fixed
- `setup` asks for every key the triggers need, not only the chosen provider's: picking
  Ollama no longer skips `OPENROUTER_API_KEY`, which `-i-` still needs, and no longer ends
  with "Setup finished" while `-i-` cannot work.
- `setup` run again with the same choices no longer rewrites `config.toml`.

### Changed
- `setup` line by line lists the providers and profiles with what each does, shows each
  match file's state in words, says "Not installed" (not "Dry run") when you answer no, and
  ends with how to try `-i-` or how many things are left to do.

## 0.23.0 - 2026-10-08

### Changed
- `config show` and the other commands that list settings show a key only as `<set>` or
  `<not set>`, no longer with its length (#222).
- Interface: in the Settings list, `u` (as `config unset`) puts a setting back to its default
  or opens Remove key; `r` now reloads there too, as on every other tab, so a reload can no
  longer undo a setting (#215).
- Faster start, most of all on Windows with endpoint scanning (#223): `promptmend --version`
  answers before the CLI loads (108 modules instead of 221 on macOS), and the triggers load
  httpx only for the provider call and the clipboard library only when they touch the
  clipboard (149 modules up to the call instead of 221).
- The install scripts (`install.ps1`, `install_windows.ps1`, `install_macos.sh`) compile the
  bytecode at install, so the first run after an install is no longer the slow one (#216).
- `doctor`'s `keys` check names the triggers that need a missing key, read from the match
  files Espanso has (or, before a deploy, the ones a deploy would write): `-i-, -iok-, -ip-,
  -if- use OpenRouter, but OPENROUTER_API_KEY is not set`. `PROMPT_PROVIDER` only counts for a
  bare `promptmend improve` and the Try tab, and alone it is a warning; its data gains
  `triggers` (#217).
- Interface: `PROMPT_PROVIDER` moved from Output to the Models group of the Settings tab, and
  its help says what it affects (a bare `promptmend improve` and the Try tab) (#217).
- `doctor`'s new `local_servers` check (information only) and the Triggers tab's State column
  say `needs Ollama` / `needs LM Studio` when a deployed local trigger's server does not
  answer. It asks only a server on this machine, with one short request; the deploy is
  unchanged: `-il-` and `-ilm-` stay active, `-ic-` stays commented out (#220).
- `promptmend stats` prints one line on the history before the numbers; the full explanation
  and the notes moved to `--verbose` (the JSON is unchanged). A real Try tab run is listed as
  trigger `-try-`, and a bare `promptmend improve` as `no trigger (direct call)` instead of
  `(none)` (#222).
- Interface: Home shows "Previous install…" only while there is one to offer (#222).
- `install.ps1`'s next steps name `promptmend` (the interface) too (#222).

### Fixed
- `doctor` no longer warns about the launcher on Windows when the match files call the same
  path written with `/` (or in another case) as this install (#212).
- The deployed `prompts-template.yml` no longer has the CLI path pasted into its header
  comment (#214).
- Interface: the Triggers tab shows the new states (`in sync`, or `missing` after a detach)
  as soon as a deploy or detach has written the files, without pressing `r` and without
  waiting for Espanso's restart (#215).
- Usage history: a write that misses its time budget (the first one on a slow machine, or
  under antivirus or endpoint scanning) is no longer lost. It waits in `history.spool` in the
  data folder (the same metadata columns, at most 100 records) until the next recorded run,
  `promptmend stats`, `promptmend history export` or `promptmend doctor` stores it.
  `history.lost` now counts only records that could not be kept at all. doctor's `history`
  warning says why and what to do, and its data gains `spooled` (#213).
- `doctor`'s `cli` check names the `.exe` on Windows, as the `install` check does (#222).
- `doctor` says `no calls yet` instead of `0 call(s) recorded in …` while the history file
  does not exist (#222).
- The deploy diff (`espanso status --diff`, Triggers "Show diff") says `(missing)` instead of
  `(on disk)` for a file that is not there yet (#222).
- Windows: PromptMend no longer reads Espanso's output, which `espansod.exe` writes to the
  terminal instead of to PromptMend (#209, #210, #211). The config folder is worked out as
  Espanso does (a portable `.espanso` folder next to the running `espansod.exe`, else
  `%APPDATA%\espanso`), so a portable Espanso gets the triggers; "running" is Espanso's
  named pipe, so `doctor` no longer calls a stopped Espanso running; Espanso is found in the
  running process, on PATH, in `%LOCALAPPDATA%\Programs\Espanso` or its uninstall entry,
  so a stale Windows Terminal PATH no longer reports it missing. A restart after a deploy
  runs Espanso in a hidden console of its own: quitting the interface no longer hangs, and
  Ctrl+C or closing the terminal no longer stops Espanso. `espanso.cmd` is not used any more,
  which also saves about half a second per Espanso call.

### Documentation
- Troubleshooting: slow start on Windows, with bytecode, an antivirus exclusion or a Dev
  Drive for uv's folders (`UV_TOOL_DIR`, `UV_PYTHON_INSTALL_DIR`) (#223, #216).
- Usage: the rule for which triggers a deploy turns on, and what a local trigger needs (#220).
- Install: why `prompt-workflow` is installed next to `promptmend`, and that it goes in 1.0.0
  ("Update", "Uninstall"; #221).
- Troubleshooting: `doctor`'s daily update check (`update-check.json`,
  `PROMPT_UPDATE_CHECK=false`) and its local server check (#222).
- Install: "Espanso on Windows": open Espanso once from the Start menu and accept its PATH
  and autostart offer, close every Windows Terminal window before the first `promptmend`
  command, and where a portable Espanso keeps its configuration (#218).
- Troubleshooting: on Windows, `promptmend` or `espanso` not recognized right after an
  install, and two Espanso instances after installing next to a portable copy (#218).
- Usage: the trigger list gives each trigger's key and whether the draft leaves the machine,
  and links the data-protection gate and `-iok-`; "The -if- form" lists the shipped models
  and how `model@endpoint` sets the endpoint pin (#219).
- Neutral wording: `-risk-` is described as a risk-analysis scaffold, and the gate's ID
  check as national ID numbers (Vietnamese formats only today); the match files and patterns
  are unchanged (#219).

## 0.22.0 - 2026-10-07

### Added
- A portable Windows build (#185): each GitHub Release now carries
  `promptmend-<version>-windows-x64.zip`, the CLI frozen with PyInstaller (a folder with
  `promptmend.exe` and its `_internal` folder, its own Python included), attested like the
  other release files. It is the base of the coming WinGet package `vlastimilbures.PromptMend`;
  until that is published it is a preview (unsigned). See docs/install.md, "Portable zip".
- `doctor` knows the `winget` install channel (the WinGet `Links` alias, else the package
  folder, neither holding the version, so the match files survive `winget upgrade`) and names
  `winget upgrade vlastimilbures.PromptMend` when a newer release exists. Its `cli` check data
  gains `frozen`. A frozen exe still in a temporary or versioned folder (straight from the
  zip or `Downloads\promptmend-<version>-windows-x64`) is refused as a launcher, with a hint
  to move it to a fixed place such as `%LOCALAPPDATA%\Programs\promptmend`.

### Changed
- In the frozen build, the interface's command line, `promptmend shell` and the setup check run
  the exe itself instead of `python -m promptmend.cli`, and the interface's import check says
  it does not apply.
- WinGet manifests for the coming package `vlastimilbures.PromptMend` (#185) are prepared:
  CI validates them and installs the Windows zip through `winget` on every pull request. The
  package is not on WinGet yet.

## 0.21.0 - 2026-10-07

### Added
- `promptmend shell` (#183): a plain command line for the terminal with the completion and
  live help of the interface's command line (Tab completes, the bar below explains the line).
  Enter runs the command in the same terminal, so `espanso deploy`, `config migrate` and
  `secrets set NAME` ask there as usual. `improve`, `persona` and `shell` are refused. A
  line that holds a key or gives a key's value is never run, and Enter replaces it on screen
  with `<value withheld>`; the history (memory only) keeps only lines that ran. It needs a
  terminal (exit 3 otherwise). New dependency: prompt_toolkit, loaded only by `shell`. See
  docs/commands.md, "shell".
- An update check (#197): `promptmend doctor` and a new first Home row, `Version`, say
  whether a newer release exists (`0.22.0 available`); doctor and the About screen give the
  update command for your install channel (for example
  `brew update && brew upgrade promptmend`). It is information only: doctor never warns about
  it, and Home stays "Ready". It asks pypi.org once a day, and at most once an hour after a
  failure (cached in `update-check.json` in the data folder), within 3 seconds, never from a
  trigger; pypi.org sees your IP address and a User-Agent naming PromptMend. New setting
  `PROMPT_UPDATE_CHECK` (default `true`); `false`, a value it rejects or a settings file it
  cannot read makes no request. `doctor --json` adds
  `latest`, `update_available` and `checked_at` to the `version` check. See docs/privacy.md,
  "Update check".

### Changed
- The interface's tab `2 Providers` is now `2 Settings` (#199): every setting and both API
  keys in one keyboard list, grouped (Output, Keys, Privacy, Models, History, Interface), each
  with a dot (a green `●` for a switch on, a key set or a value that is not the default; a
  grey `○` otherwise), its value and the file it comes from. Up/Down (or `j`/`k`) move, Space
  switches a true/false setting, Enter picks from a list or edits the value (checked as the
  CLI reads it, in the dialog), `r` resets it (`config unset`), `/` filters. On a key's row,
  Enter and `r` open the Set key and Remove key dialogs. The persona shows only as
  `<set, hidden>`. Each change logs its `promptmend config set|unset …` command on Home. The
  "Change setting", "Set key" and "Remove key" buttons are gone; the provider overview and
  the `PROMPT_LOCAL_ONLY` sentence moved to the Diagnostics tab. Home's Output row now names
  the tab that switches it, so paste or clipboard output is one Enter away (#198). See
  docs/interface.md, "Settings tab".
- The interface's command line also refuses `config set|get|unset` with a key's name and a
  value, or `config set` with a key-like value, before the value reaches a child's argv.
- A value your `PROMPT_EXTRA_PATTERNS` match now counts as a key wherever a command checks
  for one: `config set` refuses it, `config get` and `config show` do not print it, and the
  command lines of the interface and `promptmend shell` withhold it. The pattern setting
  itself is never hidden by its own patterns.

### Fixed
- The usage history uses SQLite's WAL journal only on SQLite 3.51.3 or later (#196). Before
  3.51.3, WAL could rarely reset a database, and the SQLite bundled with uv's and Homebrew's
  Pythons is older, so the history now keeps the rollback journal there and switches an
  existing WAL database back on its next recorded run. `promptmend doctor`'s `sqlite` check
  is OK on such a SQLite ("SQLite 3.50.4, rollback journal") instead of warning on every
  install, so Home says "Ready"; it warns only while a database is still in WAL on an old
  SQLite, and an unreadable version is INFO. The check's data gains `journal_mode`, and the
  interface's Diagnostics tab shows it.

### Documentation
- README is simpler (#200): no ASCII logo, a flow diagram (`docs/flow.mmd`, rendered into a
  light and a dark SVG by `scripts/render_diagram.py`) instead of the text one, one-sentence
  highlights, only the Home screenshot, the example collapsed, and one install command per OS.
  The channel and update/uninstall tables are in docs/install.md, whose "Update" section now
  also has the Windows redeploy note for upgrades from 0.20.0 or earlier (#18).
- The benchmark figures behind CONTRIBUTING's "Known gaps in the default prompt" moved to
  docs/benchmark.md ("Per-draft findings of the 2026-10 round"), its only home; the gaps link
  there. Gap 10 now says the v0.19.0 prompt copies a short pasted email on flash-lite.

## 0.20.1 - 2026-10-07

### Fixed
- Windows: every trigger that runs the CLI (`-i-`, `-iok-`, `-ip-`, `-if-`, `-il-`, `-ilm-`,
  `-ic-` and `-p-`) failed under Espanso and pasted `[Espanso]: An error occurred during
  rendering` (#18). Espanso runs a `type: shell` var through PowerShell on Windows, which
  cannot run a quoted path followed by arguments. The match files now call the CLI from
  `type: script` vars: Espanso starts the CLI directly with an argument list, no shell, on
  every system. After upgrading, run `promptmend espanso deploy` to replace the deployed
  match files (it recognises the old ones as its own). A trigger of your own copied from an
  older deployed file keeps the shell shape: rewrite it as shown in docs/usage.md, "Your own
  triggers".
- `improve` and `persona` print nothing on stderr, not even a Python warning or a library's
  message (they discard it), since a script var treats any stderr output as a failed
  expansion. A crash still exits nonzero, so Espanso still reports it. The `promptmend` and `prompt-workflow`
  commands now start through `promptmend.entry:main`.
- `promptmend espanso deploy` no longer refuses a CLI path holding `$`, a backtick or (on
  Windows) `% ^ & | < >`, which no shell reads any more. It still refuses a quote, a
  backslash, a control character, and what Espanso itself would replace in the path: `{{`
  (its variables) and `%HOME%`, `%CONFIG%` and `%PACKAGES%`.

### Documentation
- The Homebrew install adds `brew tap` and `brew trust --formula`: recent Homebrew refuses
  to load a formula from an untrusted third-party tap.

## 0.20.0 - 2026-10-07

### Added
- The interface's Home tab has a command line (#111): press `c` from any tab and type a
  `promptmend` command. It completes commands, options, setting names (after `config set`,
  `get` and `unset`; never a key name there), key names (only after `secrets set` and
  `secrets remove`) and profile names, and shows the command's usage and help as you type, or
  the usage error the CLI would print. Up and Down go back through this session's lines;
  Escape leaves it. A line that looks like it holds a key is never shown back or kept in that
  history. The footer shows `c Command`.
- Enter on that command line acts on the command (#111). A command that reads, previews,
  changes a setting or writes an export, asking nothing (`doctor`, `config show|get|set|unset|validate`, `secrets status`, `profiles list`,
  `stats`, `espanso status`, `history export`, any `--dry-run`, `--help` or `--version`) runs
  in a separate process, one at a time, with no input and at most 120 seconds; its output
  (keys redacted) and exit code appear below the line, and Home's session log lists it.
  `doctor` skips the clipboard there unless you pass `--clipboard`. `espanso deploy`,
  `espanso detach`, `config migrate`, `secrets set NAME`, `secrets remove`, `history prune`
  and `history reset` open their tab's dialog, which asks first as the buttons do
  (`--yes` and the like are ignored). `setup`, `config rollback`, `config retire` and
  `profiles migrate` say to run them in a terminal; `improve`, `persona` and `ui` are
  refused. `secrets set` with a value (or `--stdin`) after the key's name is cleared, not
  run or kept.
- Home has two more buttons (#111). Recipes… lists the commands Home suggests before you
  have done anything; picking one puts it on the command line, ready to edit, and runs
  nothing until Enter (`secrets set OPENROUTER_API_KEY` only fills in the key's name: Enter
  then opens the hidden key dialog). Copy last sends this session's latest command, exactly
  as Home's session log shows it (withheld values such as `<value withheld>` and placeholders
  included), to the terminal's clipboard (OSC 52; a terminal may need it allowed, or drop
  it); it is disabled until there is one, and the interface never reads the clipboard.
- The interface has a seventh tab, Try (#111, key `7`): type a draft, pick Local stub or Real
  provider, the provider, profile ("as configured" by default, as a trigger) and tier, and
  press Run to see the rewrite a trigger would paste. It never reads or writes the clipboard
  and runs in process, so the draft never goes into a command line. It reads the settings as
  a trigger does (an invalid one stops it with the marker), every run goes through the
  data-protection gate (a stub run exactly as its real call would), and a blocked draft
  shows the trigger's marker with nothing sent. Local stub (the default) answers on
  `127.0.0.1` with a placeholder key, calls no provider and records nothing. Real provider asks
  first (provider, model, base URL) and is recorded in the usage history as a direct call;
  Home's session log shows it as
  `promptmend improve --provider openrouter --source argument --text '<draft withheld>'`
  (with its profile and tier too). A line under the result gives the time, requests, tokens and the cost as
  reported, `unknown` or `not applicable`, never 0 for a cost nobody reported. The command
  line's refusal of `improve` now points at the Try tab, and the set-up smoke test's stub
  replies report token counts.
- `OLLAMA_NUM_CTX` sets Ollama's context window (`options.num_ctx`); empty (the default)
  sends nothing (#168).
- Windows: a one-command install (#186). Each GitHub Release now attaches `install.ps1`, with
  its version filled in; `powershell -ExecutionPolicy ByPass -c "irm
  https://github.com/vlastimilbures/promptmend/releases/latest/download/install.ps1 | iex"`
  installs uv if it is missing (winget, else uv's official installer), then the attested
  wheel and `constraints.txt` attached to that same Release, runs `uv tool update-shell`
  and `promptmend doctor --no-clipboard`, and prints `promptmend setup` and
  `promptmend espanso deploy` as the next steps without running them. It prints every
  command it runs; running it again updates in place. `PROMPTMEND_VERSION` picks another
  release (0.19.0 or later). CI runs it on Windows against the wheel it built, once with no uv
  on PATH, and dry-runs a rendered copy.

### Changed
- The interface's second tab is now labelled "2 Providers" (it was "Providers & keys"), so
  all seven tabs fit on an 80-column terminal. It still holds the keys.
- The interface's intro and About screen show the new PromptMend logo (a mended speech bubble
  beside the name), the same one that opens the README.

### Documentation
- The README is rewritten as a short landing page: what PromptMend does, screenshots of the
  Home and Try tabs, an example, install paths for macOS (Homebrew, uv), Windows (the
  one-command install, uv) and Linux, a quick start, the triggers and the key commands. It
  renders on PyPI too (absolute links, no GitHub-only syntax).
- The details moved to pages in `docs/`: install (verify, update, switch channel, uninstall,
  upgrading from older versions), usage, commands (options, scripting contract, exit codes),
  configuration (the complete settings reference), profiles, privacy, interface and
  troubleshooting. `docs/benchmark.md` opens with the current results at a glance, the only
  place the numbers are kept, which removes the README's contradicting figures.
- The package metadata names the project, its keywords and classifiers, and links the docs,
  issues and source on PyPI. The bug report form asks for the install channel,
  `promptmend --version` and `promptmend doctor --json`.

## 0.19.0 - 2026-10-06

### Breaking
- The project is renamed **PromptMend** (#169): the package (PyPI and wheel) is `promptmend`
  instead of `espanso-prompt-rewriter`, the command is `promptmend` instead of
  `prompt-workflow`, and the Python package is `promptmend` instead of `prompt_workflow`.
  `prompt-workflow` stays installed as a deprecated alias until 1.0.0: the triggers,
  `--help` and `--version` behave exactly as with `promptmend`, and every other command
  (and the interface) first prints one stderr line, "`prompt-workflow` is deprecated; use
  `promptmend` (removed in 1.0.0)". Usage lines name `promptmend` either way, and the
  interface's header, intro and About show PromptMend.
- The marker the triggers paste for an error or a note is `[promptmend: …]` (was
  `[prompt-workflow: …]`): a script that looks for the old prefix must look for the new one.
- The match labels in Espanso's search bar start with `PromptMend:` (was `prompt-workflow:`),
  and a deployed file's stamp is `# promptmend <version> (managed; edit at your own risk)`.
  A file an earlier release deployed, with its `# prompt-workflow` stamp and `prompt-workflow`
  launcher, still counts as ours (`stale`), so the next deploy replaces it without a
  conflict. A side-by-side copy is now `<name>.promptmend-new`. The `__PROMPT_WORKFLOW__`
  placeholder in the packaged match files is unchanged.
- OpenRouter calls carry `X-Title: promptmend` (was `espanso-prompt-rewriter`; the benchmark
  sends `promptmend-bench`), so the dashboard lists them under the new name.
- Upgrade: first `uv tool uninstall espanso-prompt-rewriter` (the triggers stop until the
  deploy), then `uv tool install promptmend==<version> -c <the release's constraints.txt>`,
  `promptmend espanso deploy` and `promptmend doctor`. The old tool must go first: both
  install a `prompt-workflow` launcher, so uv refuses `promptmend` next to it, and after a
  `--force` install, uninstalling the old tool later deletes the alias. If you already used
  `--force`, run `uv tool uninstall espanso-prompt-rewriter`, then install `promptmend` again
  with `--force`. `scripts/install_macos.sh` and `install_windows.ps1` uninstall
  `espanso-prompt-rewriter` themselves (one line says so) before installing. `doctor` warns while the deployed
  matches still call a `prompt-workflow` launcher, and its previous-install check, `setup` and
  `profiles migrate` still find a checkout from before the rename (its old project name,
  `src/prompt_workflow/prompts`, a `.venv/bin/prompt-workflow` launcher or uv receipt).
- The repository moved to https://github.com/vlastimilbures/promptmend (#169). GitHub
  redirects the old `espanso-prompt-rewriter` URLs, but update bookmarks and remotes
  (`git remote set-url origin https://github.com/vlastimilbures/promptmend.git`). The PyPI
  project is `promptmend`, and the Homebrew formula is `promptmend`
  (`brew install vlastimilbures/tap/promptmend`).

### Added
- `promptmend doctor` has a `folders` check (#169): it warns while an old
  `prompt-workflow` folder is still in use, or holds entries left behind because the new
  folder has its own.
- `OPENROUTER_DATA_COLLECTION` (`allow` or `deny`, empty by default) sets OpenRouter's
  `provider.data_collection` routing field on every OpenRouter call, both tiers (#29). `deny`
  routes only to endpoints that do not store or train on requests; empty, the default, leaves
  the request body unchanged. The default `google-ai-studio/flex` pin served a `deny` call
  when checked; if a pinned endpoint does not qualify, OpenRouter falls back to one that does
  while `OPENROUTER_ALLOW_FALLBACKS=true`, and otherwise the trigger pastes an HTTP error
  marker. README "What is sent, and to whom" and the Configuration table describe it.
- The release workflow can upload each release's sdist and wheel to PyPI as
  `promptmend`, by trusted publishing (no stored token), once the owner turns it
  on with the repository variable `PYPI_PUBLISH` (#95). The new `pypi` job runs after the
  GitHub Release is published, holds only the OIDC token, and uploads the files the release
  job attested, never a rebuild.
- `scripts/brew_formula.py` writes the Homebrew formula for the project's own tap
  (`vlastimilbures/homebrew-tap`, formula `promptmend`) from `uv.lock`: the Release's
  sdist plus one resource per runtime dependency, each pinned by SHA-256 (#95).
- README "Install", "Updating" and "Uninstall" cover each channel: uv from PyPI or from a
  GitHub Release, and Homebrew (the PyPI and Homebrew channels start with 0.19.0), with the
  tap trust note and how to switch channels (#96). Scoop and WinGet are no longer planned.
- `promptmend espanso status` and the interface's Triggers tab also list every other
  `.yml`/`.yaml` file in Espanso's `match/` folder, such as Espanso's own `base.yml` or your
  own variants, as `yours`: not managed, never touched by `deploy` or `detach` (#38). Our
  backups, side-by-side copies and subfolders such as `packages/` are not listed; a symlinked
  file is listed and marked as a link. These lines never change the exit code.

- Clipboard output: with `PROMPT_OUTPUT=clipboard` (`promptmend config set PROMPT_OUTPUT
  clipboard`, or the interface) the improve triggers put the rewrite on the clipboard and paste
  nothing, so the trigger just vanishes and you paste when ready; switching windows during a
  long wait no longer sends the paste elsewhere. Error markers, the `-iok-` "sent despite" note
  and the cut-off note still paste; if the clipboard cannot be written, the rewrite is pasted
  after a marker. `improve --output paste|clipboard` overrides the setting for one call. README
  "Clipboard output" (#134, #23).
- The interface opens with a short intro: an ASCII wordmark (text only on a terminal of 70
  columns or fewer), the name, version and a tagline (#112). It closes after 0.8 s or on any
  key or click, and that key does nothing else. `promptmend ui --no-intro`, or the new
  setting `PROMPT_UI_INTRO=false` (default `true`; the triggers ignore it), skips it. When an
  earlier checkout install is found, its screen opens once the intro has closed.
- `a` in the interface opens an About screen: version, install channel, Python and Textual
  versions, the settings and history folders, licence and repository (#112).
- The interface teaches its headless commands (#111). Each button's tooltip shows the command
  that does the same in a terminal; each result of a change starts with the command it was
  (`$ promptmend config set PROMPT_TIMEOUT_SECONDS 45`, a value that looks like a key
  shown as `<value withheld>`); and Home lists this session's commands, or six recipes such
  as `promptmend espanso status --diff` before there are any. A test parses every one
  of these commands against the CLI.

### Fixed
- `promptmend espanso deploy` finds Homebrew's stable `bin/` launcher also when Python
  reports the formula's virtualenv through Homebrew's `opt/<formula>` link rather than its
  Cellar path; before, it fell back to the running script, labelled `script` instead of
  `homebrew` (#95).
- Interface: the header puts the status (`ok`, `1 problem, 2 warnings`) before "set up and
  manage", so a narrow terminal cuts the tagline instead of the status (#174).
- Interface: the Diagnostics settings table and the Providers & keys key table show a file
  under your home folder as `~/…` (in "From", "Also set in" and the "overrides …" and
  "invalid value in …" notes), so the column keeps the file name; `config show` still prints
  the full path (#174).
- Interface: Home's headline no longer shows a check's Markdown backticks around a command
  (#174).

### Changed
- The user folders are named `promptmend` now (#169): settings in `~/.config/promptmend/`
  (`%APPDATA%\promptmend\`), the usage history and deploy manifest in
  `~/.local/share/promptmend/` (`%LOCALAPPDATA%\promptmend\`). The first management command
  or the interface (not `improve`, `persona`, help, `--version`, an unknown command or shell
  completion) renames the old `prompt-workflow` folders and prints one line per folder on
  stderr; until then the triggers keep using the old ones. A config folder holding the `.env`
  that `PROMPTMEND_ENV`/`PROMPT_WORKFLOW_ENV` names, or a symlinked old folder, is not moved
  (doctor says what to do). A rename keeps `secrets.toml` private; when both folders
  exist only what the new one lacks is moved and nothing is overwritten; `migration.json`'s
  paths follow, so `config rollback` still works; a move that fails is retried by the next
  command. README "Updating" describes it.
- `PROMPTMEND_ENV` names the legacy-mode `.env` (#169). `PROMPT_WORKFLOW_ENV` still works as
  its old name; when both are set, `PROMPTMEND_ENV` wins.
- Interface: the intro no longer closes by itself after 0.8 s; it stays until you press
  Enter, Escape or any other key, or click, and that key does nothing else (#173). A muted
  line under it says so and names the command that turns it off:
  `promptmend config set PROMPT_UI_INTRO false`. `ui --no-intro` still skips it once.
- README "CLI" documents the `[promptmend: …]` marker as the stable way for a script to
  tell a failed `improve` run from a rewrite, since the exit code is always 0 (#32).
- The `default` prompt's two examples now open `CONTEXT` with your `PROMPT_PERSONA` (a new
  `{{PERSONA_OPENING}}` token, filled like `{{PERSONA_RULE}}`; without a persona they read as
  before), so a rewrite no longer drops the persona the rule asks for (#49). On the bench with
  a fictitious persona it opened `CONTEXT` in 105 of 105 scored runs on both default models
  (before: 100 on flash-lite, 96 on gpt-6-luna).
- The inputs step no longer contradicts "Execute, but state assumptions up front": it adds
  "if step 1 says execute, state your assumption instead and ask only about a gap that blocks
  the task" (#46). The independent review now reads "Use a separate agent with [domain] domain
  knowledge if you can run one, otherwise review as an independent expert in that domain
  would: …", so it also works in a plain chat; "validate them with me" is kept. The `-p-`
  snippet carries both wordings. docs/benchmark.md has the before/after results.
- The `default` prompt no longer quotes the bench's own drafts (#48): its two examples are new
  situations (a summary of training notes, a catering delivery time), and its decision rules
  name other readers and lengths ("a brief letter to a tax office" instead of "a quick email
  to a regulator"; no "Herr Maier", landlord, help-center, "two-line" or "one-paragraph").
  The rules themselves are unchanged. On the bench (docs/benchmark.md) flash-lite passed 122
  of 132 runs (before: 115), with 3 `<CONTEXT>…</GOAL>` slips as before, and copied pasted
  material in every `pasted` run (before: none).
- Bench (`scripts/bench_models.py`, #48): a frozen `holdout` suite of 8 new drafts no prompt
  was tuned on (`--suite holdout`, not part of `all`); a test that fails when a profile or the
  `-p-` template quotes any bench draft (a three-word phrase or a name) or any holdout
  specific; the contested review labels of `quick-ceo`, `memo` and `outliers` accept either
  answer; and the report adds the mean `[REVIEW: …]` count per rewrite for each draft.
- README's example output was regenerated with the v0.19.0 prompt on the default standard
  model, without a persona, and is labelled with both (#40).
- README "Privacy and data protection" and `.env.example` explain that a local alias of an
  Ollama cloud model (`ollama cp gpt-oss:120b-cloud my-model`) has no `cloud` tag, so it counts
  as local: the gate does not run and `PROMPT_LOCAL_ONLY` does not refuse it. Set
  `PROMPT_GATE_LOCAL=true` to gate it (#33).
- Development: strict mypy now checks `tests/` as well as `src/` and `scripts/` (#41). The
  tests see the real types of the bench, release and lock-check scripts they load, instead of
  an untyped module.

### Removed
- The static `-prompt-` form ("Act as {{role}}", objective, context, constraints, output) in
  `prompts-core.yml` (#38). `-p-` already gives the golden CONTEXT…OUTPUTS template to fill
  in. `prompts-core.yml` now holds only `-risk-`. Your deployed copy shows as `stale` until you
  run `promptmend espanso deploy`, which updates it like any unedited file (no conflict
  question, no backup).
- The interface's Home tab is redesigned (#112). A headline names the most urgent problem
  and the tab that fixes it ("Almost ready: one match file was edited since the last deploy.
  -> 4 Triggers"), or says "Ready: type -i- in any text field." Below it, one row each for
  what `-i-` and `-ip-` run (provider, model, whether the key is set), the match files and
  Espanso, the usage history, where the rewrite goes (`PROMPT_OUTPUT`) and every doctor check counted, each with a status word (ok,
  warn, FAIL), never colour alone. The header shows a status pill after the version: `ok`,
  or how many checks fail and warn (`1 problem, 2 warnings`). The full list of checks moved
  to the Diagnostics tab, where it already was.

## 0.18.0 - 2026-10-06

### Security
- Every repeat in the gate's built-in patterns is now bounded, as the code comment already
  claimed (#36). The JWT pattern's three unbounded segments made a 50,000-character draft of
  `eyJ-` repeats take about 0.29 s of CPU to scan (and 4.5 s at 200,000 characters); the worst
  50,000-character case found now takes about 0.05 s. A pattern now reads only what it needs
  to know a secret is there: a vendor key's minimum length, a JWT's header and the start of
  its payload (`eyJ` and 7 more characters; the signature is no longer required), and a
  Bearer token's first 16 characters, one of them a letter, digit or underscore (no word
  boundary required after it). A JWT header longer than 8,192 characters or a private-key
  label longer than 64 counts as a match. So the gate flags what it flagged before, plus a
  JWT without its signature, a very long `eyJ…` run and a long upper-case `-----BEGIN` label,
  with three exceptions it no longer flags: a "JWT" whose payload does not start with `eyJ`
  (real payloads are JSON objects, so they always do), a Bearer token whose first 16
  characters are only dots and hyphens, and more than 256 whitespace characters between
  `Bearer` and its token. A test checks that no built-in pattern has an unbounded repeat.
- An invalid `PROMPT_EXTRA_PATTERNS` regex (including a repeat count too large for the regex
  engine, or nesting too deep to compile) is now rejected when settings load, naming the
  entry but never the pattern (#36). `config validate`, `config set`, `doctor` and the
  interface report it instead of only the cloud triggers, and every rewrite trigger, local
  ones too, prints a marker until it is fixed (`-p-` still pastes the persona; before, the
  local triggers ran and the usage history silently dropped each record). README and `.env.example` now warn against patterns with
  nested quantifiers, which can take exponential time on every draft.
- An Ollama cloud model is recognised in any letter case and with a pinned digest
  (`GPT-OSS:120B-CLOUD`, `gpt-oss:Cloud`, `gpt-oss:120b-cloud@sha256:…`) (#33). Before, such a
  spelling counted as local: the data-protection gate did not run, `PROMPT_LOCAL_ONLY=true` did
  not refuse it, the interface's Providers tab showed it as local and the usage history
  recorded its cost as `not_applicable`. Only the tag counts, so a name such as
  `cloudy-llama:7b` stays local.
- An error that quotes a rejected value no longer repeats one that matches your
  `PROMPT_EXTRA_PATTERNS` (#32): for example `PROMPT_PROVIDER=PRJ-12345` with the pattern
  `PRJ-\d+` now pastes `Unknown provider <redacted, 9 chars>`, and a bad
  `PROMPT_TIMEOUT_SECONDS` matching a pattern is described in the marker and in
  `config validate`. `config show` still shows the
  patterns themselves.
- `--tier` with an unknown value is quoted like every other rejected value (#32), so a
  key-shaped one is described instead of pasted back.

### Added
- `config validate`, `doctor` and the interface's Home and Diagnostics tabs now scan
  `PROMPT_PERSONA` with the data-protection gate's patterns (the built-in ones and
  `PROMPT_EXTRA_PATTERNS`) once (#29). The persona goes with every cloud call in the system
  prompt and the gate never scans it, so a match is now reported by finding name only, never
  the text: `config validate` lists it as a problem (exit 4) and doctor's new `persona` check
  warns. Nothing changes on the triggers. A persona that is never sent is not flagged: when
  neither `PROMPT_PROFILE` nor `PROMPT_PRO_PROFILE` uses `{{PERSONA_RULE}}` (e.g. `general`),
  or with `PROMPT_LOCAL_ONLY=true` and `PROMPT_GATE_LOCAL` off. A persona that cannot be read
  shows as such in doctor instead of "not set".
- `PROMPT_GATE_LOCAL` (default `false`) (#33): `true` runs the data-protection gate for Ollama
  and LM Studio on `localhost` too, with the same override rules, for a local server that
  relays to a cloud API (LiteLLM, an SSH tunnel). `PROMPT_LOCAL_ONLY` still allows them. A
  draft it blocks pastes `[prompt-workflow: Blocked call to the local server …]`.
- The interface's header now shows the installed version next to the name
  (`prompt-workflow 0.17.0 — set up and manage`) on every screen (#112).
- `OPENROUTER_PRO_MAX_TOKENS` (default empty) (#31): the output cap for the pro tier (`-ip-`,
  `-if-`, `--tier pro`), whose reasoning counts against it. Empty keeps today's behaviour, the
  pro tier sharing `OPENROUTER_MAX_TOKENS` (`2400`) with the standard tier. `--max-tokens` and
  a max-tokens pick in `-if-` still win; the popup's `default` now keeps this setting.

### Changed
- `config set` and `config unset` (and the interface's settings and profile forms) warns, naming the findings
  only, when a saved change leaves a `PROMPT_PERSONA` that the gate's patterns match and that
  the profiles send, as `config validate` would flag it. The value is still saved (exit 0).
- With `PROMPT_GATE_LOCAL=true`, a blocked cloud call no longer suggests a local trigger
  (`-il-`) or a local model, which the gate would block too. The message without it is
  unchanged.
- An invalid `PROMPT_EXTRA_PATTERNS` regex is now named as `entry N (custom_N)`, the name a
  finding of that entry gets (both count the non-empty entries from 1).
- `config show` prints `(empty)` for a setting set to an empty value on purpose (such as
  `PROMPT_TEMPERATURE=`), instead of a blank; `config get` still prints the raw value.
- README Triggers: a tip to use `-ip-` for a draft that pastes an email or thread, since `-i-`'s
  faster model often summarises short pasted material instead of copying it (#42).
- Match files (#38, #23, #41): every match, the commented-out `-ic-` too, has a `label:`
  (`prompt-workflow: …`), so Espanso's search bar names each trigger instead of showing
  `{{output}}`. The `-if-` model list now starts with, and defaults to, `default`, which keeps
  `OPENROUTER_PRO_MODEL` and its `OPENROUTER_PRO_PROVIDER` pin, as the comment and README
  already said (with the shipped settings that is the same `openai/gpt-6-luna@openai` as
  before; with a custom pro model it now follows that model and uses `PROMPT_PRO_PROFILE` if
  set, where the old fixed default used `PROMPT_PROFILE`). The `-if-` timeout list stops at 120 s (`180` removed), since Espanso blocks every
  other trigger while a call runs. The `-ip-` comment quotes the 2026-10-04 bench (about 6 s
  median, 9 s p95) instead of "~5-8 s". Run `prompt-workflow espanso deploy` to update the
  deployed matches; until then `doctor` reports them as `stale`.
- README and `.env.example` say that an `http://` Ollama or LM Studio base URL on another
  machine sends the draft in clear text over the network (only the cloud providers require
  `https://`) (#33).
- README "Privacy and data protection" has a new "What is sent, and to whom" section (#29): the
  system prompt, including your `PROMPT_PERSONA`, goes with every call and is not scanned by the
  data-protection gate; OpenRouter calls reach OpenRouter and an upstream endpoint (the pinned
  one, or another while `OPENROUTER_ALLOW_FALLBACKS` is `true`, the default) and carry an
  `X-Title` header naming this app; keys travel only as the authentication header. The
  introduction no longer suggests that only the draft is sent.
- Docs (#41): the README, `.env.example` and CONTRIBUTING say that on macOS and Linux the config
  folder (`config.toml`, `secrets.toml`, `.env`, `profiles/`) is `$XDG_CONFIG_HOME/prompt-workflow/`
  when `XDG_CONFIG_HOME` is set (Windows always uses `%APPDATA%\prompt-workflow\`); the pro
  tier's latency is quoted from the 2026-10-04 bench (median 5.6 s, p95 9.2 s) everywhere;
  CONTRIBUTING says CI runs the tests with `--cov` and a 95 % floor, and its project tree lists
  `previous_install.py` (a test now checks the tree names every module).
- README tips (#23, #38): do not type or switch windows while a rewrite runs, since Espanso
  pastes it wherever the focus is when the answer arrives (the usage history write adds up to
  0.25 s, 1 s on the first write); on Linux the clipboard needs `xclip`, `xsel` or
  `wl-clipboard`; select, copy and type `-i-` to rewrite text in place; with a release wheel,
  put your own trigger variants in a match file of your own, which `prompt-workflow espanso
  deploy` never touches, since editing a deployed file stops its updates. The Windows install
  script is now run with `-ExecutionPolicy Bypass -File` instead of changing the policy.
- The benchmark's method and result tables moved from README to `docs/benchmark.md` (#40);
  README keeps a short "Model benchmark" summary (the default models, their pass counts and
  latency) and links there. Each results heading now names the release whose prompt it scored
  (the current table: the prompt of v0.14.0, unchanged since), and the README's
  example output says it came from v0.7.0 on `google/gemini-3.5-flash-lite`.
- The bench (`scripts/bench_models.py`) reports what its pass rates measure (#48). Each check's
  name starts with its kind (`struct:`, `branch:` or `draft:`), and below the unchanged `pass`
  column the report gives each kind's pass rate, over the runs scored on it, and the overall
  one with a Wilson 95% interval. Two new checks: the configured persona must be in `CONTEXT`
  unless the draft states its own role (not scored on a `--persona none` run; #49), and an
  `INPUTS` or `OUTPUTS` that opens with "None" must not then ask for something with
  `[REVIEW: …]`. `kept` no longer counts a key found only in `INPUTS`, unless the draft pastes
  material. So pass counts and `kept` from earlier runs are not directly comparable with new
  ones (`docs/benchmark.md`). The bench's note on the drafts once held out now says they are no
  longer held out, and the benchmark notes say the blind pairwise judgement of 0.11.0 was a
  one-off by hand whose harness is not in the repository.
- A `.env` that is not UTF-8 (UTF-16, say) is now a marker on the triggers,
  `[prompt-workflow: the checkout's .env is not UTF-8 text; save it as UTF-8]` (or "the .env
  in the user config folder", "the .env named by PROMPT_WORKFLOW_ENV"), instead of being
  skipped silently for the next `.env` or the defaults (#32). A persona set in the real
  environment is still printed by `persona`.
- An unquoted `PROMPT_EXTRA_PATTERNS` value cut at ` #` (a comment) now stops every rewrite
  trigger with `[prompt-workflow: the value of PROMPT_EXTRA_PATTERNS was cut at ' #' (a comment);
  quote the value]` instead of running the gate on part of the patterns (#32). A `PROMPT_PERSONA`
  cut that way is still read as before. For both, `config validate` (exit 4), `doctor` and
  the interface report the cut, and `config migrate` refuses until the value is quoted, so a
  regex such as `ticket #\d{5}` is never carried into `config.toml` as `ticket`. A ` # note`
  after any other setting stays an ordinary comment.
- `--tier pro` and `--effort` apply only to OpenRouter (#31): with `--provider ollama`,
  `lmstudio` or `anthropic` (or that `PROMPT_PROVIDER`), `improve` now pastes
  `[prompt-workflow: --tier pro applies only to OpenRouter, not 'ollama']` and makes no call,
  where it used to ignore the option silently. `--tier standard` and `--effort default` still
  pass. No trigger is affected: none passes either option to another provider.
- An empty `PROMPT_TEMPERATURE` (set, but to nothing: `PROMPT_TEMPERATURE=` in a `.env`, `""`
  in `config.toml`, or `prompt-workflow config set PROMPT_TEMPERATURE ""`) now sends no
  temperature to any provider, for models that reject the field (#31); before, it was an
  error. Unset (or `config unset`) keeps the default `0.2`.

### Removed
- The unshipped candidate prompts `docs/prompt-candidates/B.md` to `I2.md` (#41). They stay in
  git (`git show v0.17.0:docs/prompt-candidates/I2.md`); `PLAN.md` remains as the decision
  record and now says which candidate shipped.

### Fixed
- The bench's accounting (#39): latency (`p50`, `p95`) times only the HTTP attempt that
  answered, not a failed first attempt or the wait before a retry, and `results.json` records
  `retries` (the HTTP attempts after the first) instead of `retried`. Runs skipped once the
  budget is spent no longer count as failures: the report shows them in a new `skip` column
  and prints `-` instead of `nan` for a setup with no finished run. `p95` is the nearest-rank
  95th percentile and is shown only from 20 runs on. A `null` cost no longer crashes a run,
  any other unexpected error becomes that run's error instead of stopping the bench, and
  `results.json` is rewritten after every finished run. Every HTTP attempt's reported cost,
  failed and retried ones included, now counts against `--budget` (through the usage observer
  of #87); an attempt that reports no cost is counted apart, never as 0, and named in the
  total. `cost` in `results.json` is `null` when no attempt reported one.
- `persona` (`-p-`) prints your `PROMPT_PERSONA` even while another setting is invalid or the
  secret store is broken (#32); `improve` still reports the problem. Only a settings file the
  persona cannot be read from (a broken `config.toml`, a `.env` that is not UTF-8) or a persona
  line run into the next one gives the `[role]` placeholder.
- A `.env` saved with a UTF-8 byte order mark (Windows Notepad) no longer loses its first
  setting (#32), and `config migrate` no longer drops that setting as "not a setting".
- Anthropic replies split into several text blocks are joined in order instead of keeping
  only the first (#32).
- The truncation note is provider-neutral, `[prompt-workflow: the reply hit the model's output
  limit and is cut off]`, and a reply that used the whole limit before writing any text
  suggests `--max-tokens` only when the request carried that cap (#32): always for OpenRouter
  and Anthropic, for Ollama and LM Studio only with `--max-tokens` (#31).
- `--max-tokens` now reaches Ollama (`options.num_predict`) and LM Studio (`max_tokens`)
  (#31); before, only OpenRouter and Anthropic received it. Without the option their requests
  carry no cap, as before. A capped Ollama reply that used its whole budget before writing any
  text now suggests raising `--max-tokens`.
- `--tier pro` with a provider other than OpenRouter used `PROMPT_PRO_TIMEOUT_SECONDS` and a
  set `PROMPT_PRO_PROFILE` (#31). It is refused now (see Changed), and the per-call settings
  keep the standard timeout and profile for any provider but OpenRouter.

## 0.17.0 - 2026-10-05

Upgrading from the install scripts (an editable checkout install) to the release wheel: you no
longer need to migrate from the old install first. Install the wheel, then run
`prompt-workflow setup` (or open the interface): it finds the checkout, copies its settings,
key and edited profiles with your consent, and deploys the match files. If it finds nothing,
name the checkout: `prompt-workflow setup --migrate-from PATH`. The checkout's `.env` stays in
place until `prompt-workflow config retire --from PATH`.

### Added
- `prompt-workflow doctor` has a new `previous_install` check (#110). Before any setting is
  saved (no `config.toml`, no secret store, not in `PROMPT_WORKFLOW_ENV` mode) it looks for an
  earlier checkout install whose `.env` a wheel install does not read: the launcher in the
  deployed match files or the deploy manifest (`<checkout>/.venv/bin/prompt-workflow`, or
  `.venv\Scripts\prompt-workflow.exe` on Windows) and the uv tool receipt of an editable
  install. It never scans the disk and never reads that `.env`, and only a folder whose
  `pyproject.toml` names this project counts. It also warns when the `prompt-workflow` that
  `PATH` finds first is not the installed launcher, for example while a checkout's `.venv` is
  active. `doctor --json` adds the check with the data `gated`, `roots`, `signals`,
  `env_file`, `retire_pending` and `shadow`.
- `prompt-workflow config migrate --from PATH` copies the settings and key of an earlier
  checkout's `.env` (#110). It fills only the settings still at their default, so nothing you
  use today changes (the others are listed as kept), and it leaves that `.env` where it is, so
  the old triggers keep working until the match files are deployed again. A `.env` in use is
  migrated in the same step as before. `prompt-workflow config retire --from PATH` then moves
  the old `.env` into the backup; it is refused while a match file still runs that checkout's
  CLI. Both show a preview first, and `config rollback` undoes either. `prompt-workflow setup`
  offers the copy (and the checkout's edited profiles) when it finds such a checkout, or the
  one `--migrate-from PATH` names, and the retire after the deploy step; `doctor` warns while
  a copied `.env` is still in place. A broken `.env` in a checkout setup found by itself is a
  to-do, not a failure, and setup then saves nothing, so the offer stays open.
- The interface (`prompt-workflow ui`) opens a "Previous install" checklist once per session
  when it finds such a checkout, or a copied `.env` still in place (#110): copy the settings
  and key, copy the edited profiles, deploy the match files, then retire the old `.env`. Each
  step shows its preview in a dialog whose Cancel has the focus and runs the same service as
  its command; keys are named, never shown. "Enter a path…" looks at a checkout you name, and
  "Skip this checkout" stops the offer for it. Home has a "Previous install…" button and the
  `previous_install` check, and the Profiles tab's "Migrate from checkout" uses the checkout
  found.

### Changed
- `prompt-workflow profiles migrate`, the interface's profile copy and setup now say how to use
  every copied file named like a built-in profile, not only `default.md`: it replaces the
  built-in only once `PROMPT_PROFILE_OVERRIDES` lists it, and the message gives the
  `prompt-workflow config set PROMPT_PROFILE_OVERRIDES ...` command (keeping the names already
  listed). `doctor`'s `profiles` warning for such a file gives the same command.
- When `profiles migrate` finds nothing to copy, it says what it compared with (the commit the
  branch shares with its upstream, or HEAD without one) and how to compare with an older
  commit (`--rev <commit>`), since a profile committed on a branch without an upstream counts
  as unchanged.
- `prompt-workflow config rollback` after `config migrate --from` (#110) no longer says "the
  .env is read again" when only the earlier checkout's `.env` comes back: this install never
  reads it. The preview says so, notes that the match files still call this install, and names
  the way back (that checkout's install script) or `config migrate --from` to copy again.
- Setup's to-do lines name what they are about ("copy the settings of PATH", "copy the profiles
  added or edited in PATH"), and its migration preview no longer reads as if the copy ran. The
  interface's "Previous install" screen notes that rollback leaves the match files deployed.

## 0.16.1 - 2026-10-05

### Fixed
- The comments at the top of the deployed `prompts-template.yml` and `prompts-llm.yml` no
  longer say the persona and settings come from the repository's `.env`. A wheel install has
  none: they now point at `prompt-workflow espanso deploy` and the `PROMPT_PERSONA` setting
  (`prompt-workflow config set`). Comment only; `prompt-workflow espanso deploy` shows both
  files as `stale` and rewrites them (#123).
- `prompt-workflow doctor` no longer fails on `launcher: the deployed matches call …, which is
  gone` because of a deploy-manifest entry for a match file that no longer exists (its folder
  was deleted, or Espanso moved to another config folder). Only entries whose file still exists
  are judged; the others are listed in the check's new `orphans` data and mentioned in its
  message. `prompt-workflow espanso deploy` now forgets such entries (`forgot … (already
  gone)`), even when every match file is in sync; `setup` and the TUI do so without asking,
  since no file is written. An entry for a file that exists, or that cannot be looked at (an
  unreadable folder), is never dropped.

## 0.16.0 - 2026-10-05

Upgrading from the install scripts to the release wheel: the wheel never reads the
checkout's `.env` (only an editable install looks for one in the repository it was installed
from), so move your settings first, from the old editable install. Update the checkout
(`git pull`) and re-run the install script, so that install has the new commands, then run
`prompt-workflow config migrate`. Or keep the `.env` and point `PROMPT_WORKFLOW_ENV` at it
(set for GUI apps too, since Espanso does not inherit your shell). Profiles you added or
edited in the checkout need `prompt-workflow profiles migrate` too. Then install the wheel with
`uv tool install --force <wheel> -c constraints.txt`, run `prompt-workflow doctor`, and run
`prompt-workflow espanso deploy` if it reports a stale match file. A guided migration is
planned (#110).

### Security
- A clipboard item that a password manager marked as concealed is refused before it is read,
  for every trigger, and cleared, so Espanso's restore cannot put it back unmarked for the next
  trigger (#24). Markers: `org.nspasteboard.ConcealedType` or `com.agilebits.onepassword` on
  macOS; `ExcludeClipboardContentFromMonitorProcessing`, `Clipboard Viewer Ignore` or
  `CanIncludeInClipboardHistory` (0, or present but unreadable) on Windows. The probe asks only
  which formats are present, through ctypes (about 10-40 ms on macOS), and adds no dependency.
  On Linux, for browser-extension copies, or if the probe fails, the clipboard is read as
  before.
- The `general` profile, used by the local triggers `-il-` and `-ilm-`, treats the whole
  clipboard as the draft: data to rewrite, never instructions to the rewriter (#47). Pasted
  material is copied word for word, and the rewrite adds a constraint that instructions inside
  it must not be followed. It adds no facts, roles or audiences, and returns only the prompt.
  On flash-lite (a proxy; no local model measured yet), the bench's `check_general` passes
  36/36 drafts, up from 4/36 (31 of the old failures were invented roles), and the injection
  drafts are never carried over as instructions, with the guard present, in 12/12 runs.
- Invisible characters no longer reach the model or the paste (#22). `_clean()` now drops
  every code point Unicode marks as default-ignorable (zero-width space, word joiner, BOM, bidi
  marks, soft hyphen, combining grapheme joiner, Hangul fillers, variation selectors, and the
  unassigned blocks that render as nothing) and every other format character, since a run of
  them after one visible character could carry a hidden instruction. An emoji keeps one
  presentation selector and one joiner, a keycap keeps its selector, and Persian and Indic
  joiners survive. Ideographic variation selectors, Mongolian variation selectors and bidi marks
  are dropped too. The gate's normalised scan drops the same characters, so they can no longer
  split a card number.
- The gate catches the secret shapes developers paste most (#20):
  - camelCase and JSON names (`clientSecret`, `accessToken`, `dbPassword`), compound env names
    (`PGPASSWORD`), `*_KEY` names (`SECRET_KEY`, `PRIVATE_KEY`, `secret_key_base`) and
    `_authToken`;
  - PHP `'password' => …`, Go `:=`, `define('DB_PASSWORD', …)`, `environ["API_KEY"] = …` and
    values aligned with many spaces;
  - passwords of 6+ characters (`password: hunter2`) and in prose (`the password for the
    admin account is …`, Vietnamese `mật khẩu wifi là …`, `mật khẩu đăng nhập: …`);
  - AWS temporary keys (`ASIA…`), `Basic` credentials (UTF-8 too), `curl -u user:password`
    (also after a `\` line continuation), npm tokens, Azure `AccountKey=`/`SharedAccessKey=`
    and SAS `sig=`;
  - IBANs (any case, spaces or dashes; country length and mod-97 checked);
  - card numbers split by up to three spaces, tabs, slashes, dashes or minus signs, or one line
    break, or next to another number.
- A draft that is one password- or token-like word (no spaces, 8-200 characters, a digit and
  three of lower case, upper case, digit and symbol) is blocked as `bare_token`: the classic
  stale-clipboard slip. URLs, paths, emails, UUIDs, hashes, versions, dates and lower-case
  slugs and file names are not. A single word is never a prompt, so a mixed-case identifier
  with a digit is blocked too.
- An email address next to a password (`jane@example.com:…`) is a hard `credential_pair`
  finding, and `scheme://:password@host` (no user name) counts as `url_credentials` (#21).

### Added
- A full-screen interface for setting up and managing prompt-workflow (#93, #64): run
  `prompt-workflow` in a terminal, or `prompt-workflow ui`. Six tabs (Home, Providers & keys,
  Profiles, Triggers, History, Diagnostics) show and change what the headless commands do,
  through the same services: keys only as set or not set, each trigger with its fixed provider
  and deploy state, a diff preview, deploy (an edited file is kept unless you choose otherwise),
  detach, `.env` and profile migration, export, prune and reset of the usage history, config
  provenance, SQLite and lost-write state, and an import-time check. Every change asks first
  (Cancel has the focus; deploy and detach refuse a plan that changed since their preview) and
  reloads every tab; credentials in a base URL are never shown; no provider is called except by the Test call button (a stub on
  127.0.0.1). Keys are letters and digits, `t` switches to a high-contrast theme, and
  `NO_COLOR` is honoured. Without a terminal a bare `prompt-workflow` still prints the help and
  exits 2 (the help now lists `ui`), and `ui` exits 3. Textual is a new dependency
  (`textual>=8.2.8,<9`), loaded only when the interface opens: no trigger or other command
  imports it. The `doctor` service gains `import_check()`, `factory` gains `routes()` and
  `assets` gains `triggers()`, read-only helpers the interface shows.
- Headless management commands (#92), each a thin wrapper over a service and safe to script:
  `prompt-workflow --version`; `setup` (provider, default profile, API key, a deploy preview and
  a smoke test that runs `improve` against a stub on 127.0.0.1, never a paid call; it offers a
  `.env` migration and applies it only on a yes; `--non-interactive`, `--api-key-stdin`,
  `--deploy`); `config show [--raw]|get|set|unset|validate|migrate|rollback` (show gives each
  value's source and what it overrides; migrate and rollback need a confirmation, or
  `--yes --preview-token` from the preview); `secrets set [--stdin]|status|remove`;
  `profiles list|migrate`; `stats [--by trigger|provider|model|day] [--json]` with its caveats
  (local observations, not billing; rendered is not pasted; triggers with an explicit provider
  ignore `PROMPT_PROVIDER`); `history export|prune|reset`; and `doctor [--json]` (#25), which
  reports each deployed match file (`prompts-template.yml: stale` in the drift case), launcher
  drift, keys set or not, Espanso found and running, history health and the clipboard's length,
  never a key, persona or clipboard text. `espanso deploy` gains `--dry-run`. A key is never a
  command-line argument (stdin or a hidden prompt only; `--stdin` at a terminal asks hidden),
  `config set` refuses one, and neither a usage error nor `config show|get` repeats one. These
  commands use exit codes 0-4 (README, "CLI"), never prompt without a terminal (exit 3), run on
  a broken `.env`/TOML in repair mode, and honour `NO_COLOR`. `setup` and `stats` disclose the
  usage history and how to switch it off (`prompt-workflow config set PROMPT_HISTORY false`).
  The commands load lazily, so the triggers import none of them.
- The usage history records every `improve` and `persona` run (#89): one operation per run
  (origin, trigger, kind, profile, outcome, latency until the output was printed) and one
  attempt per HTTP attempt, a retry's two included, with its tokens and reported charge. The
  run is written after its output is printed and flushed, in one transaction, so history never
  changes what is pasted, the exit code or a retry, and the charge of a reply whose content was
  then rejected, or whose `--copy` failed, is kept. Outcomes: `ok`, `error_marker`,
  `gate_blocked` (the gate, `PROMPT_LOCAL_ONLY` or a non-https cloud URL), `validation_failed`, `clipboard_failed`, `concealed_refused`,
  `unexpected_error`. Settings that fail to load record nothing (whether history is
  off is then unknown). With `PROMPT_HISTORY=false`
  nothing is written and `sqlite3` is never imported. Each recorded run also prunes up to 100
  records past `PROMPT_HISTORY_RETENTION_DAYS`, in its own transaction after the record is
  committed, and the write that creates the database gets
  0.75 s more than the usual ~0.25 s budget (a slow disk dropped it).
- `improve` and `persona` take a hidden `--trigger-id` from a fixed allowlist (`i`, `iok`,
  `ip`, `if`, `il`, `ilm`, `ic`, `p`); every managed Espanso match now passes its own as a
  literal argument (the commented-out `-ic-` too, and `-p-` through its one `persona` call).
  Without it a run is recorded as `direct`; an unknown value is recorded unattributed (a managed run
  with no trigger) and never fails. What a trigger pastes is unchanged. Run `prompt-workflow espanso deploy` to update the
  deployed matches; older ones keep working and are recorded as `direct`.
- `scripts/update_match_history.py` never drops a digest `match_history.py` already lists, and
  also records the match files at every commit that changed them on the branch and on the
  default branch, so a file an editable install of an untagged commit deployed is recognised
  as ours (`stale`), not `foreign`.
- Managed Espanso deployment (#86): `prompt-workflow espanso deploy|status|detach`. `deploy`
  shows a plan and a diff and asks first (`--yes` skips); a second run with nothing to change
  does nothing. A manifest in the per-device data dir (`~/.local/share/prompt-workflow/`,
  `%LOCALAPPDATA%\prompt-workflow\` on Windows) records each file it wrote, so `status` reports
  `missing`, `in sync`, `stale` (an older deploy of ours, the #25 drift case), `modified` or
  `foreign`. Each deployed file opens with `# prompt-workflow <version> (managed; edit at your own
  risk)`; the rest is byte-identical to what the install scripts wrote. A file you edited is never
  overwritten silently: keep yours (the default with `--yes`), take ours with a
  `.bak-<timestamp>` backup (only our last 2 backups are kept), or write ours side by side
  (`--on-conflict keep|ours|side`); a deploy that kept a file ends with a `WARNING` naming it.
  A file any earlier release's installer wrote, unedited, is recognised as ours and updated
  (by digest of its source, `match_history.py`). `detach` keeps `-prompt-`/`-risk-` by default
  (`--keep-static`) or removes every deployed file (`--remove-all`), and changes only files
  still as we wrote them. The launcher is the install channel's stable entry point (uv's tool
  bin, Homebrew's `bin/`, Scoop's shim), never a versioned path an upgrade removes.
- The install scripts pin the tool to `uv.lock` (`uv export` constraints for `uv tool install`)
  and check the result with `scripts/check_tool_lock.py` (#34), then call
  `prompt-workflow espanso deploy --yes`.
- Per-attempt usage metadata for every provider (#87), the data source for the coming usage
  history. `make_provider(..., observer=...)` passes an observer to each provider, inside the
  gate, and every HTTP attempt (both attempts of a retry, a timeout, a non-2xx reply) reports
  one `AttemptUsage`: status, error kind, latency, tokens (uncached input, cache read and
  write, output, reasoning), the cost as a `Decimal` with its unit, and the cost state. A
  missing cost is `None` with state `unknown`, never 0; a reported 0 stays 0. OpenRouter's
  `usage.cost` is in credits, and a BYOK call's upstream cost is kept apart from it; local
  Ollama and LM Studio are `not_applicable`. Usage is recorded before the reply is
  finalised, so a reply that then fails still has its tokens on record. Records never hold the
  prompt, the response, a raw body or a key. No trigger output changes: nothing passes an
  observer yet.
- A release workflow (#94). Run by hand with *dry-run* unticked (or, with the repository
  variable `RELEASE_ON_PUSH` set to `true`, on a push to `main`) for the head of `main` whose
  version has no published Release yet, it builds the sdist and wheel,
  installs the wheel in a clean venv on macOS, Windows and Linux and checks its match files,
  profiles and `persona`, attests build provenance, and creates the annotated tag and a GitHub
  Release with the version's CHANGELOG notes. Each Release also carries `constraints.txt`, the
  runtime pins from `uv.lock`, so `uv tool install <wheel-url> -c <constraints-url>` installs
  exactly the locked dependencies. Release builds pin the build backend (hatchling 1.32.4).
  CHANGELOG headings now carry their release date (`## 0.15.0 - 2026-10-04`); a test checks that the newest one is the `pyproject.toml` version.
- Saved settings (#84). Once `config.toml` exists in the config folder
  (`~/.config/prompt-workflow/`, `%APPDATA%\prompt-workflow\` on Windows), it is the saved
  configuration and no `.env` is read, so an old `.env` never overrides a saved value. API keys
  live apart in `secrets.toml`, private to your user (mode 600 from creation on macOS/Linux, a
  user-only access list on Windows; if that cannot be set, nothing is written). Precedence:
  default < `config.toml` or `.env` < `secrets.toml` < real environment < a trigger's options.
  With `PROMPT_WORKFLOW_ENV` set, that `.env` is used alone, as before, and without
  `config.toml` nothing changes. Services (the commands follow in #92) save settings validated
  and atomically, refuse a write over an edit made since the file was read (naming the changed
  settings), never write a secret to `config.toml`, treat a newer `config_version` as
  read-only, and migrate a `.env` after a preview and explicit confirmation: back up, write,
  verify by reloading, then move the `.env` into `backups/` (never deleted); a rollback
  restores the exact files. New dependency: `tomli-w`, loaded only when saving.
- Your own profiles live in `~/.config/prompt-workflow/profiles/<name>.md`
  (`%APPDATA%\prompt-workflow\profiles\` on Windows), next to the user `.env`, where an
  upgrade cannot replace them (#85). A new name works with `--profile <name>`, `PROMPT_PROFILE`
  or `PROMPT_PRO_PROFILE`. A file named like a built-in (`default.md`) is ignored unless the new
  setting `PROMPT_PROFILE_OVERRIDES` lists that built-in (comma-separated, built-in names only),
  so a stray copy cannot silently change what every trigger sends. The package's own profiles
  are never written. Service functions for the coming `doctor`/`profiles` commands report each
  user file (added, overrides, shadowed, invalid name, missing) and copy profiles a checkout
  added or edited under `src/prompt_workflow/prompts/` into the new folder, without deleting or
  overwriting anything.
- The wheel ships the Espanso match files at `prompt_workflow/espanso/match/`, read through the
  new `prompt_workflow.assets` module, so an installed (non-editable) package can deploy its
  triggers (#85). `espanso/config/` is not shipped. A new CI job builds the sdist and wheel,
  installs the wheel into a clean venv outside the checkout on Linux, macOS and Windows, and
  checks that every match file and profile resolves and that `prompt-workflow persona` runs.
- `-iok-` (`--allow-flagged`) sends one draft that the gate blocked only for soft findings: a
  confidentiality label, a Vietnamese ID number, an email address or an IBAN (#21). The paste
  starts with `[prompt-workflow: sent despite: …]`, and the next draft is checked as usual. Keys,
  tokens, passwords, cards, private keys, a bare token and `PROMPT_EXTRA_PATTERNS` matches are
  never sent this way. The block message now offers `-iok-` when it applies, and no longer
  recommends `ALLOW_CLOUD_OVERRIDE`.
- Tests freeze the trigger contract (#82): the exact stdout bytes of `improve` and `persona`
  (success, each error marker, the `sent despite` note, the persona placeholder), and an import
  guard that runs both in a fresh interpreter and fails if they load `textual`, `rich.console`,
  `sqlite3`, `tomli_w`, `tomlkit` or `keyring`, or more than 400 modules. CONTRIBUTING records
  the measured start-up time under "Trigger start-up budget". Nothing the CLI prints changes.
- A local usage-history store (`history.py`, #88): a per-device SQLite file in the user data
  dir (`~/.local/share/prompt-workflow`, `%LOCALAPPDATA%\prompt-workflow` on Windows) that
  holds metadata only, from a fixed column allowlist, and never prompt, clipboard, output,
  persona, key, form or raw-body text. Money is exact decimal text with its unit (`credits` or
  `USD`). Writes are fail-open and bounded (about 0.25 s), keyed by (operation, attempt) so a retry
  never duplicates, and a dropped write is counted in a `history.lost` marker. Services: stats
  by trigger, provider, model or day (reported, estimated and unknown costs kept apart),
  CSV/JSON export, prune, reset and health. New settings `PROMPT_HISTORY` (default `true`) and
  `PROMPT_HISTORY_RETENTION_DAYS` (default `365`, at most 36500). Optional estimates come from a user
  `prices.toml` in the config dir. Nothing records yet; the CLI starts recording in #89.

### Changed
- README: the release wheel with `constraints.txt` is now the install path, and the install
  scripts moved to Development as the contributor path (#96). New sections: First run (the
  interface, `setup`, `setup --non-interactive --api-key-stdin --deploy`), Updating (install
  the new wheel with `--force`, then `doctor` and `espanso deploy` for stale match files, #28)
  and Uninstall (`espanso detach` first, `espanso status` to check no CLI-calling file is left,
  then optionally `history reset` and `secrets remove`, then `uv tool uninstall
  espanso-prompt-rewriter`; a broken launcher is reinstalled first or
  cleaned up from the deploy manifest, #38). The usage history gains its export, prune and reset
  commands, `stats` its caveats (credits are not USD, BYOK upstream costs stay out of the totals,
  unknown is not 0), Configuration the `config migrate`/`rollback` commands and legacy mode,
  and Troubleshooting rows for launcher drift, stale match files and Espanso's rendering error.
  A test checks every `prompt-workflow …` invocation in the README and these notes, and every
  standalone option the README names, against the CLI.
- `espanso deploy` and `detach` no longer read an answer from a non-terminal stdin: without
  `--yes` there they stop with exit code 3. The release workflow checks the installed wheel with
  `prompt-workflow --version`.
- `.env.example` sets only `OPENROUTER_API_KEY` and `PROMPT_PERSONA`; every other setting is
  shown commented out with its default, so a copied file no longer pins the model, endpoint or
  effort, and later default changes reach you (#28). The README now recommends keeping the file
  with your key in the config folder, outside the repository (#35).
- A `config.toml` or `secrets.toml` that cannot be parsed fails closed: `improve` pastes its
  error marker without calling a provider, and `persona` its placeholder.
- Loading settings no longer copies `.env` values into the process environment (#83). A pure,
  layered merge (`ConfigLayers`: built-in default < `.env` < real environment, then per-call
  overrides) builds `Settings`, so loading again in the same process sees an edited `.env`, child
  processes inherit nothing from it, and each setting records where its value came from,
  including a real environment variable that shadows a `.env` value. A repair mode, for
  management commands, returns problems as findings instead of raising: a line that runs into
  the next, an invalid value (the setting falls back to the next layer, which records the
  refused one), a line without `=` (by line number) and a `.env` that exists but cannot be read.
  The trigger path raises the same errors as before, word for word, and still skips the last
  two silently. Precedence, the setting allowlist, merged-line
  rejection and the no-current-directory rule are unchanged.
- The `general` profile writes in the language of the user's own request instead of
  translating (#47).
- A reply wrapped in one code fence is pasted without the fence, for every profile. Fences
  inside the reply, and an empty fenced reply, are kept (#47).
- The bench takes `--profile` and scores a profile without the golden template on its output
  contract (`check_general`): no answer instead of a rewrite, no preamble, role or carried-over
  injection, the draft's language and its pasted material. Fenced replies are counted (#47).
- The gate stops blocking ordinary drafts (#21). A confidentiality label counts only when
  written as one: upper case, alone on a line or opening one before `:` or a dash, in
  brackets, a `Classification:`/`Sensitivity:`/`Độ mật:` field, *highly/company/strictly
  confidential*, *internal only* or *do not distribute*. "Output restricted to 5 bullets",
  "confidential information" and "customer data" in prose pass. Vietnamese *mật* counts in
  upper case, alone or opening a line, or as *tài liệu/văn bản/thông tin mật*, *tối mật* or
  *tuyệt mật*, so *bảo mật* (security), *mật độ* (density) and *mật khẩu* (password) in prose
  pass. A 12-digit number counts as a CCCD only with a valid province and century code, and
  never inside an AWS ARN; a 9-digit number needs a document word next to it (CMND, CCCD, CMT,
  *chứng minh nhân dân*, *căn cước*, *hộ chiếu*, *passport*, *national ID*, *ID card*), not a
  bare "ID" or *số*.
- Carriage returns, vertical tabs, form feeds, NEL and the Unicode line and paragraph
  separators become newlines instead of disappearing.
- A draft over 50,000 characters is refused before it is cleaned, so a pasted multi-megabyte
  log no longer stalls the expansion.
- The built-in profiles are listed in name order on every OS (`default`, `general`). Linux
  listed them in the file system's order, so an unknown-profile error or `profiles list` could
  name `general` first.

### Removed
- `espanso/config/default.yml` and the installers' `--with-config` / `-WithConfig` option (#37).
  Nothing deploys to Espanso's `config/` folder any more, so your own `default.yml` (and any
  symlink to it) is never replaced; set `toggle_key` or `search_shortcut` there yourself if
  you want them. The scripts now refuse the old option with a message.

### Fixed
- `setup`'s smoke test and the interface's Test call no longer add a row to the usage history
  (#116): the `improve` they run against the stub on 127.0.0.1 runs with
  `PROMPT_HISTORY=false`, so `stats` shows only real use.
- `doctor` no longer says "espanso was not found on PATH" when Espanso is installed but
  `espanso path config` fails, as it does before Espanso has started once (#115). It now tells
  the three cases apart: not on PATH; found, but the query failed (its exit code and first
  useful stderr line, with a hint to run `espanso start` once); found, but the query timed out.
  In `doctor --json`, `espanso.data.found` now means on PATH, and new keys `query_failed`,
  `exit_code`, `timed_out` and `error` give the detail. `setup` and `espanso deploy`, `status`
  and `detach` say on stderr when they fall back to Espanso's default folder because the query
  failed, instead of doing it silently, and the interface's Triggers tab shows the same notice.
  The commands `doctor`, `setup` and `espanso` run (`espanso`, `uv`, `brew`) are now found
  through `PATH` the way a shell finds them, so on Windows `espanso.cmd` is no longer reported
  missing, and their output is read as UTF-8, so a byte the locale cannot decode no longer
  aborts the command.

## 0.15.0 - 2026-10-04

Upgrading: `OPENROUTER_REASONING_EFFORT` and `OPENROUTER_PRO_REASONING_EFFORT` are now checked
when settings load. A value other than empty, `none`, `minimal`, `low`, `medium` or `high`
turns every trigger into an inline error naming the variable, so fix it in `.env`.

### Added
- A call that hits a rate limit (429, unless `Retry-After` asks for more than 3 s), an
  unavailable upstream (502, 503, 504, 529, or such an error inside a 200 reply) or a refused
  connection to another machine is tried once more, within the same time limit. A timeout,
  a 500 or a 4xx is never retried.

### Changed
- `PROMPT_TIMEOUT_SECONDS`, `PROMPT_PRO_TIMEOUT_SECONDS` and `--timeout` limit the whole call,
  retry included. Before, httpx's read timer restarted with every chunk, so a server sending a
  byte now and then could hold the call (and Espanso) far longer. Connecting is capped at 10 s.
- The bench repeats a call when its error is `transient` instead of matching the message text.
- Error markers say why a call failed. A non-2xx reply shows the status, a hint
  (`out of credits`, `rate limited, try again shortly`, `provider unavailable, try again`, …)
  and the provider's own reason, cut to 160 printable characters and dropped if it matches a
  sensitive pattern. `ProviderError` carries `status` and `transient`.
- The `fake_http` test fixture runs real httpx over `httpx.MockTransport`, so provider tests
  exercise httpx's own request building (header encoding, JSON body, timeouts) instead of a
  stand-in client.

### Fixed
- An error OpenRouter returns with HTTP 200 (raised after generation started) was reported as
  "response was malformed". It is now reported with its code and message, and the partial
  text is not pasted.
- An API key containing a non-ASCII character, such as a smart quote, was reported as
  "returned invalid JSON". The key is now refused by name before any request is made.
- A mistyped `OPENROUTER_REASONING_EFFORT` or `OPENROUTER_PRO_REASONING_EFFORT` was sent to
  OpenRouter and came back as a bare HTTP 400. It is now rejected when settings load.
- A reply that stopped on `error` was pasted as complete; it is now an inline marker. A reply
  stopped by `content_filter` or `refusal` gets a visible "stopped early" note, or says the
  request was declined when it has no text.
- `pytest -m live` runs again. Since 0.11.0 it failed with a `TypeError` before any network
  call. An offline twin now runs the same command and checks on a canned reply in every
  default test run.
- A rewrite that mentions `<think>` or `</think>` after its start, such as a prompt about
  reasoning tags, is no longer cut short or pasted with the tags removed. Only reasoning at the
  start of the reply (a closed or unclosed `<think>` block, or a stray `</think>`) is stripped.

## 0.14.0 - 2026-10-04

Upgrading: re-run the installer (`./scripts/install_macos.sh` or
`.\scripts\install_windows.ps1`) to deploy the `-p-` template fix. If your `.env` sets
`PROMPT_PRO_PROFILE=default-pro`, remove the line or leave it empty: both tiers now send
`default`, and `default-pro` only still works as an alias. If you set `PROMPT_PROFILE`, `-ip-`
and `-if-` now use it too, unless `PROMPT_PRO_PROFILE` is set.

### Added
- The bench checks that sentences from the start, middle and end of pasted material reach
  `INPUTS` word for word (`pasted material not copied`) on `pasted`, `outage`,
  `pasted-injection` and a new 35-line `long-thread` edge draft. On the 2026-10 review's saved
  outputs (local, not in the repo) it flags the shipped prompt on `long-thread` in 9 of 9 runs
  and on flash-lite `pasted` in 6 of 6.
- `scripts/bench_models.py --max-tokens` sets the output cap per call (default 6000), so a run
  can score what a trigger returns under the CLI's `OPENROUTER_MAX_TOKENS`.
- The bench has three new edge drafts: `cap-thread` (a 57-line thread, just under the copy
  limit), `supplier` and `status-page` (outside readers in a short text).

### Changed
- The bench's `kept` metric matches a key only at the start of a word, and an acronym only in
  capitals, so "ID" no longer matches inside "validate" or "MAD" inside "fixes made". A test
  checks that no key matches the fixed template wordings.
- `scripts/bench_models.py` renders a fixed fictitious persona by default (`--persona example`)
  instead of the runner's `PROMPT_PERSONA`, so every machine scores the same system prompt and
  no private persona ends up in bench outputs. `--persona none` renders none, and
  `--persona env` the runner's own. Each run writes `meta.json` (git commit and dirty flag,
  SHA-256 of the template and, for the shared personas, of the rendered system prompt, persona
  mode, temperature, models, drafts, runs) and goes to `bench-out/<UTC timestamp>/` unless
  `--outdir` is given. It refuses an `--outdir` that is not empty.
- The `default` prompt, after the 2026-10 review:
  - pasted material up to about 60 lines (was about 20) is copied into `INPUTS` in full,
    keeping its line breaks, because the other assistant never sees the original draft (#42);
  - the review step is decided only by who reads the result: the user, a colleague, their
    manager or their team take the self-review, and everyone else, including the CEO and
    published text, the independent review; "quick", "short" and "brief" never decide it (#43);
  - the `- Out of scope:` bullet appears only when the draft implies concrete exclusions, and
    the first example no longer invents a one-page length or a `.md` output (#50).

  With `--persona example` and `--max-tokens 2400` (3 runs per draft, 6 on 14 drafts on
  flash-lite and 5 on gpt-6-luna), flash-lite passes 30/30 core and 102/120 edge, and gpt-6-luna
  23/24 core and 98/99 edge. The 0.13.0 prompts scored 22/27 and 66/90 on flash-lite and 24/24
  and 75/84 on gpt-6-luna with the same checks, on a different mix of drafts and runs, partly
  from outputs saved before the bench had a fixed persona. flash-lite still summarises a short pasted email and
  gives a reply to an outside sender the self-review when the draft says "for me" (see
  CONTRIBUTING's known gap 10).
- Both tiers send `default`: `PROMPT_PRO_PROFILE` is now empty by default, so `-ip-` and `-if-`
  use `PROMPT_PROFILE` like `-i-` (#44). A plain bench run now scores what every OpenRouter
  trigger sends (#61).
- The `-p-` template no longer puts a `[Constraints etc.]` placeholder in `CONTEXT` (#50).

### Deprecated
- The `default-pro` profile is an alias of `default` and will be removed in a later release.
  Set `PROMPT_PRO_PROFILE` empty, or to a profile of your own.

### Fixed
- The bench output directories that the docs suggest (`--outdir bench-A`) were not gitignored,
  so `git add .` could commit outputs that carried the runner's persona. Every `bench-*/`
  directory is now ignored, and a test checks each documented `--outdir`.
- A rewrite whose `<CONTEXT>` section the model closed with `</GOAL>` right before `<GOAL>` (a
  slip seen only on flash-lite) is now repaired before it is pasted. Only that exact pattern in
  the rewrite's first section is changed; other malformed output, and tags inside pasted
  material, are pasted as returned. Of 4,842 saved bench rewrites (local, not in the repo), the
  repair fixed all 29 with the slip and left every well-formed one unchanged. The bench scores
  the repaired text, keeps the raw one as `*.raw`, and reports the count in a `rep` column.

## 0.13.0 - 2026-10-04

Upgrading: re-run the installer (`./scripts/install_macos.sh` or
`.\scripts\install_windows.ps1`). Every trigger that runs the CLI changed (`force_mode`,
`left_word`, no `--profile` on `-i-`, `-ip-` and `-if-`), and the fix for
GHSA-9v85-6m6j-8529 takes effect only once the match files are redeployed. A proxy or CA
variable set only in `.env` no longer applies; set it for GUI apps instead (see the README).

### Security
- [GHSA-pchf-7qh9-8xpc](https://github.com/vlastimilbures/espanso-prompt-rewriter/security/advisories/GHSA-pchf-7qh9-8xpc)
  (medium): with an HTTP proxy set in the environment, in `.env` or in the system settings,
  `-il-` and `-ilm-` sent the draft to the proxy without the data-protection gate. Local calls
  now connect directly, and `.env` can no longer set proxy or CA variables (see Changed).
- [GHSA-6mvg-6wv7-5f2m](https://github.com/vlastimilbures/espanso-prompt-rewriter/security/advisories/GHSA-6mvg-6wv7-5f2m)
  (low): a settings error could paste a value holding an API key (for example, two `.env` lines
  saved without the newline between them) into the focused app. Settings errors now redact
  such values (see Fixed).
- [GHSA-9v85-6m6j-8529](https://github.com/vlastimilbures/espanso-prompt-rewriter/security/advisories/GHSA-9v85-6m6j-8529)
  (medium): by default, Espanso typed replies shorter than 100 characters as keystrokes, so each
  newline pressed Enter in the focused app, a terminal included. Every trigger that runs the CLI
  now pastes through the clipboard, so replies are no longer typed as keystrokes (see Fixed). A
  pasted newline can still run a line in a shell without bracketed paste.

### Added
- `PROMPT_LOCAL_ONLY` (default `false`): `true` refuses every provider that can send the draft
  off this machine, including the ones the cloud triggers name, with an inline marker and no
  request. `-il-` and `-ilm-` on localhost keep working.

### Changed
- Calls to a base URL on this machine (`localhost`, `127.0.0.0/8`, `::1`; any provider) now
  connect directly and ignore `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and the system proxy.
  Remote endpoints still use them.
- `.env` now sets only the settings in the README's configuration table (except
  `PROMPT_WORKFLOW_ENV`, which only the real environment can set). Any other variable there, such
  as `HTTPS_PROXY` or `SSL_CERT_FILE`, is ignored; set it for GUI apps instead (see README).

### Fixed
- Settings errors repeat a rejected value only when it is short and does not look like a key;
  otherwise they show `<redacted, N chars>`. The same applies to an unknown provider or profile.
- A `.env` value that contains another `NAME=` assignment (two lines saved without the newline
  between them) is reported by setting name instead of being read as one long value.
- Spaces around an API key are dropped, and a key with a control character (such as a newline)
  is reported as `invalid header value (check the API key)`.
- Every trigger that runs the CLI (`-i-`, `-ip-`, `-if-`, `-il-`, `-ilm-`, `-ic-`, `-p-`) now
  sets `force_mode: clipboard`, so Espanso always pastes the result instead of typing short
  replies key by key. This overrides any Espanso `backend` setting for these triggers. Re-run
  the installer to redeploy the match files.
- `-ip-` and `-if-` now use `PROMPT_PRO_PROFILE` (default `default-pro`), as documented. They
  passed `--profile default`, which took precedence. `-i-` likewise follows `PROMPT_PROFILE`.
  In `-if-`, a model other than `OPENROUTER_PRO_MODEL` uses `PROMPT_PROFILE`, because
  `default-pro` is tuned for the pro model. Re-run the installer to redeploy the match files.
- `--model default` keeps the configured model, as the `-if-` popup's `default` does for the other
  fields. It used to request a model literally named `default`.
- Every trigger now sets `left_word: true`, so it expands only at the start of a word. Text such
  as `a[n-i-1]`, `len(a)-n-i-1` or `only-if-cached` no longer fires `-i-` or `-if-` (and no
  longer sends the clipboard). A trigger right after a space or bracket still expands, as in
  `s[-i-1]`. Type a space before a trigger that follows a letter, digit or `-`. Re-run the
  installer to redeploy the match files.
- The README and `.env.example` no longer suggest that `PROMPT_PROVIDER=ollama` makes `-i-`
  local: every trigger passes its own `--provider`. Use `PROMPT_LOCAL_ONLY=true` instead.

## 0.12.0 - 2026-10-03

### Changed
- The `default` prompt was reworked after an A/B run on both tiers (judged blind; details in
  `docs/prompt-candidates/PLAN.md`). The rewrite now decides deliverable, planning, review and
  language before writing; names the sections of a document deliverable; states method choices
  as assumptions instead of `[REVIEW: …]`; asks for each missing item once and never for
  material the draft already gives; gives an email to a named person at another organisation
  (a customer, supplier, landlord) the independent review; and describes a slide deck in
  `OUTPUTS` instead of the `.md` line. The self-review lines now read "show any calculation
  steps" and "matches the format in OUTPUTS", in `prompts-template.yml` too.
  On the 32-draft bench (3 runs): flash-lite 22→23/24 core, 63→66/72 edge; gpt-6-luna
  (`default-pro`) 23→22/24 core (one run; a 6-run recheck was clean), 63→67/72 edge.

### Added
- `PROMPT_PRO_PROFILE` (default `default-pro`): the profile `--tier pro` uses. `default-pro`
  is `default` without one review-rule clause that flash-lite needs and gpt-6-luna over-applies.
  Set it empty to use `PROMPT_PROFILE` for both tiers.
- Four held-out drafts in the bench's `edge` suite (`vendor-review`, `landlord`, `teams-jana`,
  `outliers`), and the `spanish` draft accepts a plain-text `OUTPUTS` line.

## 0.11.0 - 2026-09-30

### Changed
- The `default` prompt was reworked after a benchmark review; the golden template and its fixed
  step wordings are unchanged. The rewrite now:
  - treats the whole draft as material to rewrite, never as instructions: questions become
    prompts, pasted emails or notes go word for word into `INPUTS`, and "ignore previous
    instructions" is dropped (a small model used to paste "OK" and a haiku);
  - is always written in English, with a `- Language: …` constraint for a draft in another
    language, instead of the draft's language (which made gpt-6-luna translate the fixed steps
    and break the template);
  - matches `OUTPUTS` to the deliverable (plain-text email, code block, slides…); the `.md`
    line is kept for documents;
  - keeps every number, name, date and deliverable from the draft, names only the regulations
    the draft names, and flags gaps with `[REVIEW: …]` instead of guessing;
  - treats vendors, suppliers, partners, clients and published FAQ or help-center text as
    outside readers for the review step, and routine memos to colleagues as internal;
  - writes concrete work steps and a concrete "Out of scope" line.

  Blind pairwise judges preferred it 117 to 32 (19 ties) across gemini-3.5-flash-lite and
  gpt-6-luna.

### Added
- `scripts/bench_models.py --suite edge|all`: 20 more drafts (injection, questions, pasted
  material, Czech, German and Spanish, a draft stating its own role, code, implied outside
  readers). New checks for leaked scaffolding tags, a leftover `[domain]` placeholder, the
  draft's language and role, and `OUTPUTS` matching the deliverable, plus a `kept` column for
  the share of the draft's specifics carried over. The default is still the 8-draft `core`
  suite.

## 0.10.0 - 2026-09-29

### Security
- Ollama and LM Studio pass the data-protection gate when the draft would leave the
  machine: a base URL that is not loopback, or an Ollama cloud model (`:cloud`, `-cloud`).
- Control characters, bidi overrides and invisible Unicode tag characters are stripped from
  the pasted output and from the draft. An escape sequence in a model's reply could end a
  terminal's bracketed paste and run the lines after it.
- The gate also catches `DB_PASSWORD=…`, `access_token=…`, `AWS_SECRET_ACCESS_KEY=…`,
  JSON `"client_secret": "…"`, PGP private key blocks, and GitLab and Hugging Face tokens.
- API keys no longer appear in the `repr()` of the settings or providers.
- Gitleaks allowlists two historical test values by fingerprint instead of all of
  `tests/test_redaction.py`. Pre-commit hooks are pinned to commit SHAs and updated by
  Dependabot, zizmor checks the workflows, and Dependabot waits 7 days before proposing a
  new release.

### Fixed
- Output is always UTF-8. On Windows, a rewrite with letters outside the ANSI code page
  (Czech, Vietnamese) crashed into a blank expansion.
- Building a wheel (`uv build`, any non-editable install) failed because
  `prompts/default.md` was added twice.
- `PROMPT_TEMPERATURE` also applies to Ollama.
- The macOS installer no longer needs a system `python3`, and makes `.env` readable by
  the current user only.

### Changed
- Settings are validated strictly: booleans must be `true` or `false` (`OLLAMA_THINK=1`
  used to mean false), and timeouts and token caps must be finite numbers above 0.
- `--model` is applied to the settings of whichever provider runs, so the gate sees the
  model actually used.
- Development: the dev tools are a uv dependency group (`uv sync`, no `--extra dev`), mypy
  runs strict over `src` and `scripts`, and CI adds Python 3.14 and a 95% coverage floor.

## 0.9.0 - 2026-09-29

### Security
- The redaction gate scans in linear time. An 80,000-character clipboard used to take
  15 s and freeze Espanso before any request timeout applied.
- The gate also scans a Unicode-normalised copy of the draft, so no-break or zero-width
  spaces, soft hyphens and fullwidth digits no longer hide a card number or API key.
- Drafts over 50,000 characters are refused.

### Fixed
- The installers no longer overwrite Espanso's own `match/base.yml`. The `-p-` template
  now ships as `prompts-template.yml`. A `base.yml` deployed by an earlier version is
  backed up and removed, and any match or config file whose content differs is backed
  up to `.bak-<timestamp>` before it is replaced.
- Windows installer: match files are read as UTF-8 (Windows PowerShell 5.1 turned the
  em dashes into mojibake), a failing `uv` or `espanso` command stops the script, and
  `espanso start` is used when Espanso is not running.
- A rewrite cut off at the max-tokens cap ends with
  `[prompt-workflow: output truncated at max tokens]` instead of passing as complete.
  A reasoning model that spent the whole budget thinking asks for a larger
  `--max-tokens`.
- A `<think>` block left open by truncation is dropped instead of pasted.
- Anthropic: an unexpected content block is reported as a malformed response.
- The `-p-` template's independent-review step matches the `default` profile word for
  word.
- `.env`: an inline ` # comment` after an unquoted value is no longer part of the value.
- `bench_models.py`: `model@auto` means OpenRouter's own routing, as in `--model`.

### Changed
- CI: checkout no longer persists credentials; tests run on pushes to `main` and on
  pull requests. Ruff's version is pinned once, in `uv.lock`.

## 0.8.0 - 2026-09-29

### Changed
- Bumped `typer` to 0.27.2.

### Fixed
- The `prompts-llm.yml` header comment no longer contains the CLI placeholder, so the
  installers stop writing your absolute CLI path into it.

## 0.7.0 - 2026-09-29

First public release.

### Features
- `prompt-workflow improve` rewrites a clipboard, stdin or argument draft into a precise
  prompt through OpenRouter (default), Anthropic, Ollama or LM Studio. Output has no
  trailing newline, and every failure is printed inline as `[prompt-workflow: …]` with
  exit code 0, so Espanso always has something to paste.
- `default` profile: rewrites the draft into the golden template
  (`CONTEXT / GOAL / INSTRUCTIONS / CONSTRAINTS / INPUTS / OUTPUTS`), choosing plan-first
  or execute-now, and independent or self review, from the draft. `general` profile: a
  lighter "make this prompt precise" rewrite. Any `*.md` in `prompts/` becomes a profile.
- `PROMPT_PERSONA` opens the rewrite's CONTEXT with your role, and fills the `-p-` snippet
  via `prompt-workflow persona`.
- Triggers: `-i-` (standard tier), `-ip-` (pro tier: a reasoning model), `-if-` (a popup
  picks the model, effort, max tokens and timeout for one call), `-il-` (Ollama), `-ilm-`
  (LM Studio), `-ic-` (Anthropic, commented out), plus the static `-p-`, `-prompt-` and
  `-risk-` snippets.
- OpenRouter endpoint pinning (`OPENROUTER_PROVIDER`, `OPENROUTER_ALLOW_FALLBACKS`) and
  reasoning effort (`OPENROUTER_REASONING_EFFORT`), with the reasoning trace excluded from
  the pasted text. `<think>` blocks from local models are stripped.
- `scripts/bench_models.py` scores models on template fidelity, latency and real cost from
  OpenRouter's usage data.
- macOS and Windows installers that write the absolute CLI path into the Espanso match files.

### Security
- Data-protection gate: every OpenRouter and Anthropic call is scanned first and blocked on
  payment cards (Luhn-checked, space/dot/dash separated), emails, vendor API keys
  (OpenRouter, Anthropic, OpenAI, Stripe, GitHub, Slack, Google, xAI, AWS), JWTs, bearer
  tokens, PEM private keys, `user:password@` URLs, `password=…`-style assignments,
  confidentiality labels and Vietnamese ID formats, unless `ALLOW_CLOUD_OVERRIDE=true`.
  Providers are built only through `make_provider()`, which wraps cloud ones in the gate.
- `PROMPT_EXTRA_PATTERNS` adds your own `;`-separated regexes to the gate. Matches are
  reported as `custom_1`, `custom_2`, … so the pattern text never appears in the output.
- Cloud base URLs must be `https://` (plain `http` only to loopback), so a key is never
  sent in clear text.
- `.env` is read from `PROMPT_WORKFLOW_ENV`, the installed repository, or the user config
  dir, never from the current directory, so a planted `.env` cannot redirect the endpoint
  or switch off the gate.
- Match files quote the CLI path (paths with spaces work) and no longer `cd`; the
  installers refuse a CLI path containing characters the shell or YAML would interpret.
- The installers leave Espanso's `config/default.yml` alone unless `--with-config` /
  `-WithConfig` is passed.
- CI actions are pinned to commit SHAs, dependencies install from the lockfile
  (`uv sync --locked`), Dependabot keeps both current, and gitleaks scans every push.
