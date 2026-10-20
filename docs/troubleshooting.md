# Troubleshooting

This page lists what to do when a trigger does not fire, pastes a `[promptmend: …]` marker
instead of a rewrite, or the output looks wrong. Start with `promptmend doctor`, then find the
symptom in the table for its area.

## Contents

- [Start with doctor](#start-with-doctor)
- [Triggers and Espanso](#triggers-and-espanso)
- [Settings and keys](#settings-and-keys)
- [Provider errors](#provider-errors)
- [Gate and blocked calls](#gate-and-blocked-calls)
- [Output problems](#output-problems)
- [Local models](#local-models)
- [Usage history](#usage-history)

## Start with doctor

```bash
promptmend doctor            # install, settings, keys, Espanso, match files, history, folders
promptmend config validate   # the settings alone, as a trigger reads them
```

`doctor` exits 4 when it finds a problem and names it. Its output is safe to paste into an
issue: it never shows a key, your persona or clipboard text. The interface's Diagnostics tab
shows the same checks.

`doctor` also asks pypi.org whether a newer release exists, at most once a day, and keeps the
answer in `update-check.json` in the data folder (see
[Files and folders](configuration.md#files-and-folders)); `PROMPT_UPDATE_CHECK=false` turns
that off. It asks a loopback Ollama or LM Studio whether it answers, once per run, only when a
deployed trigger uses it.

## Triggers and Espanso

| Symptom | Fix |
|---------|-----|
| Trigger does not expand right after a letter, digit, `-` or `=`, or in a field you emptied with the keyboard | Triggers only fire at the start of a word, judged by what you last typed. Type a space first, or click into the field. See [When a trigger fires](usage.md#when-a-trigger-fires) |
| Trigger does not expand | Run `espanso status`, then `promptmend espanso status`, then `promptmend espanso deploy` |
| `[Espanso]: An error occurred during rendering` | The CLI the matches call is gone or broken: the checkout of an editable install was moved or deleted, or the tool was uninstalled without `promptmend espanso detach`. Once a CLI runs again, `promptmend doctor` reports `launcher: the deployed matches call …, which is gone`. [Install](install.md) it again, then run `promptmend espanso deploy`, or `promptmend espanso detach` to remove the triggers. On Windows, match files deployed by 0.20.0 or earlier (#18) (or a trigger of your own copied from one) run the CLI through PowerShell, which fails on every trigger: run `promptmend espanso deploy`, and give your own triggers the `type: script` shape (see [Your own triggers](usage.md#your-own-triggers)). `espanso log` shows the underlying error |
| `doctor` reports `launcher: the deployed matches call …, this install is …` | Launcher drift: the matches call another install of the CLI than the one you just ran (say, an old checkout after switching to a release). Run `promptmend espanso deploy` from the install you want the triggers to use |
| `doctor` reports ``path_shadow: `promptmend` on PATH is …, not the installed launcher …`` | Your shell runs another, usually older, CLI with other settings than the one Espanso calls. Follow the hint at the end: `deactivate` an active virtual environment, remove a checkout's `.venv` folder from PATH, or uninstall the old CLI (or put the installed launcher's folder earlier on PATH). `prompt-workflow` is checked the same way |
| `doctor` reports `prompts-template.yml: stale` (or another match file) | The deployed match files are from an older version, so `-p-` and the `-if-` lists differ from the CLI. Run `promptmend espanso deploy` |
| A `base.yml.bak-…` file appeared in Espanso's `match` folder | Versions before 0.9 deployed `-p-` as `match/base.yml`, the file Espanso creates for your own snippets. The installer backed up that copy and replaced it with `prompts-template.yml`. Older installers overwrote `base.yml` without a backup, so snippets you kept there before first installing can only come from your own backups |
| Windows: `doctor` reports `espanso was not found (PATH, …)` | PromptMend looks for `espansod.exe` in the running Espanso, on PATH, in `%LOCALAPPDATA%\Programs\Espanso` and in Espanso's uninstall entry. Not found there means Espanso is not installed, or installed elsewhere and not running: start it once from the Start menu (accept its PATH and autostart offer), then run `promptmend doctor` again |
| Windows: triggers stopped working after closing the interface or the terminal | Releases before 0.23.0 restarted Espanso inside the terminal, so closing it stopped Espanso. Start Espanso from the Start menu once; since 0.23.0 a restart from PromptMend runs in a hidden console of its own |
| Windows: a portable Espanso does not get the triggers | PromptMend uses a `.espanso` folder next to the running `espansod.exe` (a portable install) before `%APPDATA%\espanso`. Start the portable Espanso before `promptmend espanso deploy`, so it is the one found. See [Espanso on Windows](install.md#espanso-on-windows) |
| Windows: `promptmend` or `espanso` is not recognized right after installing it | Windows Terminal gives a new tab or window the environment it started with, so it misses the PATH an install just changed. Close every Windows Terminal window (or sign out and in), then open a new one. For `espanso`, open Espanso once from the Start menu first and accept its PATH offer; see [Espanso on Windows](install.md#espanso-on-windows) |
| Windows: two Espanso instances run, or the triggers work only some of the time, after installing Espanso next to a portable copy | Each starts from its own shortcut at sign-in and reads its own configuration folder. Keep one: delete the other's shortcut in `shell:startup`, then run `promptmend espanso status` to see which folder PromptMend deploys to. See [Espanso on Windows](install.md#espanso-on-windows) |
| Expansion is slow | Use a faster model or endpoint (see the [benchmark](benchmark.md)); Espanso waits for the CLI. Or switch to [clipboard output](usage.md#output-paste-or-clipboard) |
| Slow start on Windows: the first run after an install takes several seconds, and every later command or trigger more than a second | Windows antivirus and endpoint protection scan each Python file a process opens, and a file without its compiled bytecode is compiled on first use. The installers (`install.ps1`, `install_windows.ps1`) compile the bytecode at install since 0.23.0; after a manual install, run `uv tool install --force --compile-bytecode …` with the same arguments. If every start stays slow, ask for an antivirus exclusion of uv's folders, or put them on a [Dev Drive](https://learn.microsoft.com/windows/dev-drive/) (which scans less): set `UV_TOOL_DIR` and `UV_PYTHON_INSTALL_DIR` to folders there (by default under `%APPDATA%\uv`), then install again. Compare with `Measure-Command { promptmend --version }` |
| `[promptmend: Clipboard unavailable: Pyperclip could not find a copy/paste mechanism …]` on Linux | Install `xclip` or `xsel` (X11) or `wl-clipboard` (Wayland) |

## Settings and keys

| Symptom | Fix |
|---------|-----|
| `[promptmend: OPENROUTER_API_KEY is not configured]` | Save the key with `promptmend secrets set OPENROUTER_API_KEY`. With a `.env`, check it is in one of the [places the CLI looks](configuration.md#where-settings-live); once `config.toml` exists, a `.env` is no longer read |
| `[promptmend: config.toml is not valid TOML (at line …)]` | Fix that line of `config.toml` in the config folder (`secrets.toml` likewise) |
| `[promptmend: …_API_KEY contains a non-ASCII or invisible character…]` | The key was pasted with a smart quote or an invisible character. Save it again as plain text |
| `[promptmend: … request failed: invalid header value (check the API key)]` | The key contains a line break or another character a key never has. Save it again |
| `[promptmend: OPENROUTER_REASONING_EFFORT must be empty or one of …]` | Fix the value with `promptmend config set` (`OPENROUTER_PRO_REASONING_EFFORT` likewise) |
| `[promptmend: OLLAMA_THINK must be true or false, got …]` (or `must be a number above 0`) | Fix that value. A long value, or one that looks like a key, is shown as `<redacted, N chars>` |
| `[promptmend: … in .env runs into the next line; add the missing newline]` | Two lines of the `.env` were saved as one. Split them |
| `[promptmend: … must be an https:// URL]` | A cloud `*_BASE_URL` uses `http`. Switch it to `https` |

## Provider errors

The text after each hint is the provider's own reason.

| Symptom | Fix |
|---------|-----|
| `[promptmend: OpenRouter returned HTTP 401: check the API key…]` | Wrong key. Replace it with `promptmend secrets set OPENROUTER_API_KEY` |
| `[promptmend: OpenRouter returned HTTP 402: out of credits…]` | Add credits to your OpenRouter account |
| `[promptmend: … returned HTTP 429: rate limited…]`, `… HTTP 5xx: provider unavailable…` or `… returned an error (code …)` | The provider is busy or down. A rate limit (unless it asks to wait more than 3 s), a 502/503/504/529 or a refused connection to another machine was already retried once within the time limit. Trigger again in a moment, or pick another endpoint in `-if-` |
| `[promptmend: … request failed: [SSL: CERTIFICATE_VERIFY_FAILED] …]` | The endpoint's TLS certificate is not trusted, often because a company proxy inspects HTTPS. It is not retried. Point `SSL_CERT_FILE` at a CA bundle that includes your company's CA, set where Espanso sees it (see [Proxies](configuration.md#proxies) for setting variables for GUI apps), or ask IT |
| `[promptmend: … HTTP 400: bad request…]` or `… HTTP 404: not found…` | Check the model slug, the endpoint pin and the base URL. A 400 that names `temperature` means the model takes none: leave `ANTHROPIC_TEMPERATURE` empty, or set `PROMPT_TEMPERATURE=` (empty) for the other providers. With `OPENROUTER_DATA_COLLECTION=deny`, a 404 can also mean no endpoint for that model meets the data policy (see [Data collection preference](privacy.md#data-collection-preference)) |
| `… the model stopped early (content_filter)]` at the end, or `… declined the request (refusal)` | A content filter or the model's safety policy stopped the rewrite. Rephrase the draft or use another model |

## Gate and blocked calls

| Symptom | Fix |
|---------|-----|
| `[promptmend: Blocked cloud call. …]` | The [gate](privacy.md#the-data-protection-gate) matched. Remove the content or use a local trigger (with `PROMPT_GATE_LOCAL=true` the message offers none, since those are gated too). If the message offers `-iok-` and the content may leave your machine, use `-iok-` for this draft |
| `[promptmend: Blocked call to the local server …]` | `PROMPT_GATE_LOCAL=true` and the gate matched a `-il-`/`-ilm-` draft. Remove the content, or set `PROMPT_GATE_LOCAL=false` if your `localhost` server runs the model itself (not a relay to a cloud API) |
| `-il-` or `-ilm-` says `Blocked cloud call` | `OLLAMA_BASE_URL` / `LMSTUDIO_BASE_URL` points at another machine, or the Ollama model is a cloud model, so the gate applies. Use a model on `localhost` for sensitive drafts |
| `[promptmend: sent despite: …]` at the top of a rewrite | You used `-iok-`; the draft was sent despite those findings. Delete the line |
| `[promptmend: Input is too long …]` | The input (the clipboard, stdin or `--text`) holds more than 50,000 characters. Copy just the draft |

## Output problems

| Symptom | Fix |
|---------|-----|
| `… the reply hit the model's output limit and is cut off]` at the end, or `… used its whole output limit before writing any text` | The model hit its output cap, often by spending it on reasoning. Raise `OPENROUTER_MAX_TOKENS` (`OPENROUTER_PRO_MAX_TOKENS` for the pro tier alone: `-ip-`, `-if-`, `--tier pro`; `ANTHROPIC_MAX_TOKENS` for Anthropic, `OPENAI_MAX_TOKENS` for OpenAI), or pick a larger max tokens (or lower effort) in `-if-`. For Ollama and LM Studio pass `--max-tokens` in your own variant trigger; without it the server's own default applies |
| Reasoning text appears in the output | Set `OLLAMA_THINK=false`. A `<think>` block at the start of the reply is stripped (a later one is kept as answer text); extend `strip_thinking` in `providers/base.py` for other tag formats |
| The rewrite landed in the wrong window | Espanso pastes wherever the focus is when the answer arrives. Do not switch windows while it runs, or use [clipboard output](usage.md#output-paste-or-clipboard) |

## Local models

| Symptom | Fix |
|---------|-----|
| `[promptmend: Ollama request failed: …]` | Start Ollama (`ollama serve`) and pull the model (`ollama pull qwen3:8b`) |
| Rewrites through Ollama ignore the template | The context window may be too small; see [Ollama context window](configuration.md#ollama-context-window) |

For fully local use, pull a model (`ollama pull qwen3:8b`) or load one in LM Studio and enable
its local server, then check it answers:

```bash
curl http://localhost:11434/api/tags      # Ollama
curl http://localhost:1234/v1/models      # LM Studio
```

## Usage history

| Symptom | Fix |
|---------|-----|
| doctor: `history: tracking incomplete: N write(s) lost` | The history write took longer than its time budget, often the first one (it creates the database) or on a machine whose antivirus or endpoint protection scans the data folder. Such a write now waits in `history.spool` and is stored by the next call; the count stays until `promptmend history reset`. See [Usage history](privacy.md#usage-history) |
| doctor: `N record(s) waiting in history.spool` | The database was locked or unusable when doctor tried to store them. Run `promptmend stats`, which stores them; if doctor says the database is corrupt, `promptmend history reset` starts a new one (and deletes the spooled records) |
| A call is missing from `promptmend stats` | Run `promptmend doctor` and check its `history` line. A call whose write took too long is stored by the next call or by `stats` itself |

## See also

- [Install](install.md#update): updating and redeploying the match files
- [Configuration](configuration.md): where each setting lives
- [Privacy](privacy.md): why the gate blocked a call
