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
| `doctor` reports ``path_shadow: `promptmend` on PATH is …, not the installed launcher …`` | Your shell runs another, usually older, CLI with other settings than the one Espanso calls. Follow the hint at the end: `deactivate` an active virtual environment, remove a checkout's `.venv` folder from PATH, or uninstall the old CLI (or put the installed launcher's folder earlier on PATH) |
| `doctor` reports a match file as `foreign` or `modified` | `foreign`: a file of that name that no PromptMend deploy wrote here (yours, or another tool's); `modified`: one PromptMend deployed that was edited since. `promptmend espanso status --diff` shows what a deploy would change, and `promptmend espanso deploy` keeps either unless you choose to replace it. A linked Espanso folder (say, `~/.config/espanso` linked from a dotfiles repository) is compared as the folder it points to, as a deploy records it; through such a link, 0.25.0 and earlier called a file you edited after a deploy `foreign` instead of `modified` |
| `doctor` reports `prompts-template.yml: stale` (or another match file) | The deployed match files are from an older version, so `-p-` and the `-if-` lists differ from the CLI. Run `promptmend espanso deploy` |
| A `base.yml.bak-…` file appeared in Espanso's `match` folder | Versions before 0.9 deployed `-p-` as `match/base.yml`, the file Espanso creates for your own snippets. The installer backed up that copy and replaced it with `prompts-template.yml`. Older installers overwrote `base.yml` without a backup, so snippets you kept there before first installing can only come from your own backups |
| Windows: `doctor` reports `espanso was not found (PATH, …)` | PromptMend looks for `espansod.exe` in the running Espanso, on PATH, in `%LOCALAPPDATA%\Programs\Espanso` and in Espanso's uninstall entry. Not found there means Espanso is not installed, or installed elsewhere and not running: start it once from the Start menu (accept its PATH and autostart offer), then run `promptmend doctor` again |
| Windows: triggers stopped working after closing the interface or the terminal | Releases before 0.23.0 restarted Espanso inside the terminal, so closing it stopped Espanso. Start Espanso from the Start menu once; since 0.23.0 a restart from PromptMend runs in a hidden console of its own |
| Windows: a portable Espanso does not get the triggers | PromptMend uses a `.espanso` folder next to the running `espansod.exe` (a portable install) before `%APPDATA%\espanso`. Start the portable Espanso before `promptmend espanso deploy`, so it is the one found. See [Espanso on Windows](install.md#espanso-on-windows) |
| Windows: `promptmend` or `espanso` is not recognized right after installing it | Windows Terminal gives a new tab or window the environment it started with, so it misses the PATH an install just changed. Close every Windows Terminal window (or sign out and in), then open a new one. For `espanso`, open Espanso once from the Start menu first and accept its PATH offer; see [Espanso on Windows](install.md#espanso-on-windows) |
| Windows: installing with uv fails with `Failed to persist temporary file to …\uv\cache\…: Access is denied` | Another process, usually antivirus or endpoint protection scanning the file, holds a file in uv's cache while uv renames it. Turn uv's cache off for the install: in PowerShell, run `$env:UV_NO_CACHE = "1"` and then the `irm … \| iex` part of the [one-command install](install.md#one-command-with-uv) in that same window (`install_windows.ps1` from a checkout likewise), or add `--no-cache` to a manual `uv tool install`. The default exe channel of the one-command install does not install with uv, so it does not hit this. If it keeps happening, ask for an antivirus exclusion of uv's cache (`%LOCALAPPDATA%\uv\cache` by default; `uv cache dir` prints it), or, if you set `UV_TOOL_DIR` and `UV_PYTHON_INSTALL_DIR` to a [Dev Drive](https://learn.microsoft.com/windows/dev-drive/) (see the slow-start row below), `UV_CACHE_DIR` can point uv's cache there too |
| Windows: two Espanso instances run, or the triggers work only some of the time, after installing Espanso next to a portable copy | Each starts from its own shortcut at sign-in and reads its own configuration folder. Keep one: delete the other's shortcut in `shell:startup`, then run `promptmend espanso status` to see which folder PromptMend deploys to. See [Espanso on Windows](install.md#espanso-on-windows) |
| Expansion is slow | Use a faster model or endpoint (see the [benchmark](benchmark.md)); Espanso waits for the CLI. Or switch to [clipboard output](usage.md#output-paste-or-clipboard) |
| The trigger stays and nothing happens: no rewrite, no marker | Espanso shows nothing while the CLI runs, up to the call's time limit, and a run still going blocks every later trigger. See [The trigger stays and nothing happens](#the-trigger-stays-and-nothing-happens) |
| Slow start on Windows: the first run after an install takes several seconds, and every later command or trigger more than a second | Windows antivirus and endpoint protection scan each Python file a process opens, and a file without its compiled bytecode is compiled on first use. The fix that helps most is the exe: the one-command install puts it in place by default since 0.26.0 (`doctor` then reports the `installer` channel), one process with its Python modules in a single archive, which started about twice as fast on a scanned machine (see [The exe](install.md#the-exe); move a uv install over as [Switch from a uv install](install.md#switch-from-a-uv-install) says). With uv, the installers (`install.ps1`, `install_windows.ps1`) compile the bytecode at install since 0.23.0; after a manual install, run `uv tool install --force --compile-bytecode …` with the same arguments. If every start stays slow, ask for an antivirus exclusion of uv's folders, or put them on a [Dev Drive](https://learn.microsoft.com/windows/dev-drive/) (which scans less): set `UV_TOOL_DIR` and `UV_PYTHON_INSTALL_DIR` to folders there (by default under `%APPDATA%\uv`), then install again. To measure it, run `promptmend doctor --timing`: it times `--version`, `persona` and a stub `-i-` trigger through the launcher and through the interpreter, cold and warm, without calling a provider (see [Timing start-up](commands.md#timing-start-up)) |
| Windows: an update of a uv install stopped with `failed to remove directory …\Scripts: Access is denied`, and since then `promptmend` and every trigger fail with `ModuleNotFoundError: No module named 'promptmend'` | A `promptmend` was still running (usually the open interface) while uv replaced it: uv had removed the old install already when it found `Scripts\python.exe` in use. Close the interface and every running `promptmend`, then run the same update again (see [Update](install.md#update)). The one-command install with `PROMPTMEND_CHANNEL=uv` and `.\scripts\install_windows.ps1` now stop before uv runs while one runs; the check is best effort and runs once, so a trigger started during the update can still cause this |
| Windows: the interface shows its checks, the update check or the setup plan seconds late | On Windows with Python 3.12, background threads get very little time while the interface runs (#348). The interface works around it, but the fix is a newer Python: the exe (the one-command install's default) brings Python 3.14 from 0.27.0 on; a uv install picks its Python at install, so run `uv tool install --force --python ">=3.13" …` with the same arguments (see [Windows](install.md#windows)). `promptmend doctor --json` shows the Python in use (`checks.version.data.python`) |
| `[promptmend: Clipboard unavailable: Pyperclip could not find a copy/paste mechanism …]` on Linux | Install `xclip` or `xsel` (X11) or `wl-clipboard` (Wayland) |
| `[promptmend: The clipboard has no text: copy your draft, then type the trigger again]` | The triggers rewrite what is on the clipboard, and it held no text (or only spaces, or an image). Select the draft, copy it, then type the trigger. A call with `--source stdin` or `--source argument` says `Input is empty` instead |
| `[promptmend: OpenRouter timed out after 30.0s: try again, or raise PROMPT_TIMEOUT_SECONDS]` | The provider did not answer within the call's time limit. Try again; if it keeps happening, raise the setting the marker names (`PROMPT_PRO_TIMEOUT_SECONDS` on the pro tier, `-ip-`), or use a faster model or endpoint. A call whose limit came from `--timeout` (the timeout picked in the [-if- form](usage.md#the--if--form)) ends `try again with a longer --timeout` instead. See [While it runs](usage.md#while-it-runs) |

### The trigger stays and nothing happens

Espanso shows nothing while the CLI runs, up to the call's time limit:
`PROMPT_TIMEOUT_SECONDS`, `PROMPT_PRO_TIMEOUT_SECONDS` on the pro tier (`-ip-`),
or the timeout picked in the [-if- form](usage.md#the--if--form). A run still going blocks
every later trigger until it ends. Every run ends by its limit plus 2 s, plus Python's
start-up, with a rewrite or a marker (see [While it runs](usage.md#while-it-runs)); nothing at
all after that means the process never finished (still starting, say) or Espanso lost the
paste. To tell the cases apart:

- Find the run in `promptmend history export` (`--format csv` puts each attempt on its
  operation's row) by its `occurred_at_utc` time, in UTC. `promptmend stats --by trigger
  --verbose` shows each trigger's last use and its p50/p95 latency, not the last run's.
  - Outcome `ok`, or any outcome but a timeout (`gate_blocked`, `validation_failed`,
    `clipboard_failed`, …), with its `latency_ms`: the CLI printed its rewrite or marker and
    Espanso did not paste it.
  - Outcome `error_marker` with `latency_ms` near the limit and an attempt whose
    `error_kind` is `timeout` (or no attempt, when it stalled before its request): the call
    stalled, and its timed-out marker was not pasted either.
  - No row usually means the process never finished. A row can also be lost (see
    [Usage history](#usage-history) and `promptmend doctor`'s `history` line), and with
    `PROMPT_HISTORY=false` nothing is recorded.
- Run the trigger's own call in a terminal to see the time and the marker without Espanso;
  for `-ip-`: `time promptmend improve --tier pro --source argument --text "…"`
  (in PowerShell: `Measure-Command { promptmend improve … | Out-Default }`). It adds a row of
  its own, as a direct call.
- `promptmend doctor --timing` measures the start-up alone (see
  [Timing start-up](commands.md#timing-start-up)).
- On a slow machine, `PROMPT_OUTPUT=clipboard` is the robust mode: the rewrite is on the
  clipboard even when the paste is lost (see [clipboard output](usage.md#output-paste-or-clipboard)).

## Settings and keys

| Symptom | Fix |
|---------|-----|
| ``[promptmend: OPENROUTER_API_KEY is not set: run `promptmend secrets set OPENROUTER_API_KEY` in a terminal]`` (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY` likewise) | Save the key with `promptmend secrets set OPENROUTER_API_KEY` in a terminal, then type the trigger again. If the marker stays, an empty `OPENROUTER_API_KEY=` in the environment Espanso runs in hides the saved key: `promptmend config show` names where each value comes from |
| `[promptmend: OPENROUTER_API_KEY is not set: add it to the .env that PROMPTMEND_ENV names]` | `PROMPTMEND_ENV` is set, so that `.env` alone holds the settings and `secrets set` refuses. Add the key to it, and check that `PROMPTMEND_ENV` names the file you edit where Espanso sees it: no other `.env` is read ([Settings in a .env](configuration.md#settings-in-a-env)) |
| `[promptmend: the settings did not load within …s]`, `[promptmend: the settings took …s to load, …]` or `… timed out after …s, …s of it loading the settings: try again, …]` | Reading the settings files stalled or was slow: the config folder sits on a redirected, synced or network folder (a redirected `%APPDATA%` on a managed Windows laptop, say), or antivirus holds its files. Run `promptmend doctor` to see which config and data folders are used (its `folders` check), and ask IT to keep them on a local disk or exclude them from scanning. See [While it runs](usage.md#while-it-runs) |
| `[promptmend: starting up took …s, past the …s time limit; nothing was sent]` | Start-up alone used up the call's time limit, so no call was made. See the slow-start row under [Triggers and Espanso](#triggers-and-espanso), or raise `PROMPT_TIMEOUT_SECONDS` |
| `[promptmend: config.toml is not valid TOML (at line …)]` | Fix that line of `config.toml` in the config folder (`secrets.toml` likewise) |
| doctor or `config validate`: `the secrets file can be read by other users` (macOS, Linux) or `the secrets file's access list or owner is not private to your account` (Windows), or `could not check the secrets file's …` | Something widened the permissions of `secrets.toml` after it was written, or they could not be read. On macOS and Linux run `chmod 600` on it; on Windows save a key again with `promptmend secrets set OPENROUTER_API_KEY`, which rewrites the file with an access list for your account alone. See [API keys](configuration.md#api-keys) |
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
| ``[promptmend: …: the server's certificate is not trusted by this computer (TLS verification failed: …); see `promptmend doctor`]`` | The endpoint's TLS certificate is not trusted, often because a company proxy inspects HTTPS with a root that is not in the store it is checked against. The reason after the colon is the operating system's or OpenSSL's, shortened. It is not retried. Run `promptmend doctor`: `tls` says what certificates are checked against. If `SSL_CERT_FILE` or `SSL_CERT_DIR` is set, the bundle it names lacks your company's CA (or is not there: `tls` warns): fix or unset it. Otherwise ask IT to install the company root in the system store, or point `SSL_CERT_FILE` at a CA bundle that includes it, set where Espanso sees it (see [Certificates](configuration.md#certificates)) |
| The same certificate error for every public site, with `tls` reporting `os_trust_store` | The trust store loads but cannot validate the chain: a Linux system without CA files at OpenSSL's default paths (install your distribution's `ca-certificates`), or a Windows machine whose store lacks the public root and cannot fetch it (a locked-down network). Point `SSL_CERT_FILE` at a CA bundle (such as certifi's `cacert.pem` plus your company's CA), set where Espanso sees it (see [Certificates](configuration.md#certificates)) |
| `[promptmend: … HTTP 400: bad request…]` or `… HTTP 404: not found…` | Check the model slug, the endpoint pin and the base URL. A 400 that names `temperature` means the model takes none: leave `ANTHROPIC_TEMPERATURE` empty, or set `PROMPT_TEMPERATURE=` (empty) for the other providers. With `OPENROUTER_DATA_COLLECTION=deny`, a 404 can also mean no endpoint for that model meets the data policy (see [Data collection preference](privacy.md#data-collection-preference)) |
| `… the model stopped early (content_filter)]` at the end, or `… declined the request (refusal)` | A content filter or the model's safety policy stopped the rewrite. Rephrase the draft or use another model |

## Gate and blocked calls

| Symptom | Fix |
|---------|-----|
| `[promptmend: Blocked cloud call. …]` | The [gate](privacy.md#the-data-protection-gate) matched. Remove the content or use a local trigger (with `PROMPT_GATE_LOCAL=true` the message offers none, since those are gated too). If the message offers `-iok-` and the content may leave your machine, use `-iok-` for this draft |
| `[promptmend: Blocked cloud call. Sensitive content detected: payment_card. …]` on a draft with no card number in it | Something in the draft looks like a card to the [gate](privacy.md#what-it-blocks): 13 to 19 digits in groups (split by spaces, dots, dashes or one line break) that pass the card checksum, as one in ten does (a Unix time with a fraction such as `1732700000.123456` is such a group), or a long number without separators that has a card brand's prefix and length and passes the checksum. `-iok-` does not send it: a card number is a hard finding. Replace the number with a placeholder or shorten it, or use a local trigger |
| `[promptmend: Blocked call to the local server …]` | `PROMPT_GATE_LOCAL=true` and the gate matched a `-il-`/`-ilm-` draft. Remove the content, or set `PROMPT_GATE_LOCAL=false` if your `localhost` server runs the model itself (not a relay to a cloud API) |
| `-il-` or `-ilm-` says `Blocked cloud call` | `OLLAMA_BASE_URL` / `LMSTUDIO_BASE_URL` points at another machine, or the Ollama model is a cloud model, so the gate applies. Use a model on `localhost` for sensitive drafts |
| `[promptmend: sent despite: …]` at the top of a rewrite | You used `-iok-`; the draft was sent despite those findings. Delete the line |
| `[promptmend: Input is too long …]` | The input (the clipboard, stdin or `--text`) holds more than 50,000 characters. Copy just the draft |

## Output problems

| Symptom | Fix |
|---------|-----|
| `… the reply hit the model's output limit and is cut off]` at the end, or `… used its whole output limit before writing any text; raise OPENROUTER_MAX_TOKENS]` (the setting that capped this call, or `--max-tokens` when the call passed it) | The model hit its output cap, often by spending it on reasoning. Raise `OPENROUTER_MAX_TOKENS` (`OPENROUTER_PRO_MAX_TOKENS` for the pro tier alone: `-ip-`, `-if-`, `--tier pro`; `ANTHROPIC_MAX_TOKENS` for Anthropic, or a lower `ANTHROPIC_EFFORT`, `OPENAI_MAX_TOKENS` for OpenAI), or pick a larger max tokens (or lower effort) in `-if-`. For Ollama and LM Studio pass `--max-tokens` in your own variant trigger; without it the server's own default applies |
| Reasoning text appears in the output | Set `OLLAMA_THINK=false`. A `<think>` block at the start of the reply is stripped (a later one is kept as answer text); extend `strip_thinking` in `providers/base.py` for other tag formats |
| The rewrite landed in the wrong window | Espanso pastes wherever the focus is when the answer arrives. Do not switch windows while it runs, or use [clipboard output](usage.md#output-paste-or-clipboard) |

## Local models

| Symptom | Fix |
|---------|-----|
| `[promptmend: Ollama is not running on 127.0.0.1:11434: start it, then try again]` (`LM Studio` likewise) | Nothing answers at `OLLAMA_BASE_URL` / `LMSTUDIO_BASE_URL`. Start Ollama (the app, or `ollama serve`) and pull the model (`ollama pull qwen3:8b`); for LM Studio, load a model and enable its local server. The connection is not retried |
| `[promptmend: OpenRouter: no server is running on 127.0.0.1:8080: start it, then try again]` | A cloud provider's base URL points at this computer (a local proxy, say) and nothing answers there. Start that server, or set the `*_BASE_URL` back to the provider's own |
| `[promptmend: Ollama request failed: …]` | The connection broke off, or `OLLAMA_BASE_URL` names another machine that could not be reached (a refused connection there is retried once). Check that Ollama runs there and listens on the network, and that the model is pulled (`ollama pull qwen3:8b`) |
| On Windows, every `-il-` or `-ilm-` call takes about 2 s longer than the model needs, and `promptmend doctor` says `may wait about 2 s for ::1 at localhost` | `OLLAMA_BASE_URL` / `LMSTUDIO_BASE_URL` names `localhost`, which Windows tries as `::1` first, while the server listens on `127.0.0.1` only. Use `127.0.0.1` where doctor says the setting comes from: for a saved setting the command it prints, such as `promptmend config set OLLAMA_BASE_URL http://127.0.0.1:11434`; else the environment or your `PROMPTMEND_ENV` file (see [Ollama](configuration.md#ollama)) |
| Rewrites through Ollama ignore the template | The context window may be too small; see [Ollama context window](configuration.md#ollama-context-window) |

For fully local use, pull a model (`ollama pull qwen3:8b`) or load one in LM Studio and enable
its local server, then check it answers:

```bash
curl http://127.0.0.1:11434/api/tags      # Ollama
curl http://127.0.0.1:1234/v1/models      # LM Studio
```

## Usage history

| Symptom | Fix |
|---------|-----|
| doctor: `history: tracking incomplete: N write(s) lost` | The history write took longer than its time budget, often the first one (it creates the database) or on a machine whose antivirus or endpoint protection scans the data folder. Such a write now waits in `history.spool` and is stored by the next call; the count stays until `promptmend history reset`. Under heavy contention (many triggers at once) some lost writes are counted in `history.lost.*.drop` files in the data folder, which can pile up; `promptmend history reset` clears them too. See [Usage history](privacy.md#usage-history) |
| doctor: `N record(s) waiting in history.spool` | The database was locked or unusable when doctor tried to store them. Run `promptmend stats`, which stores them; if doctor says the database is corrupt, `promptmend history reset` starts a new one (and deletes the spooled records) |
| A call is missing from `promptmend stats` | Run `promptmend doctor` and check its `history` line. A call whose write took too long is stored by the next call or by `stats` itself |

## See also

- [Install](install.md#update): updating and redeploying the match files
- [Configuration](configuration.md): where each setting lives
- [Privacy](privacy.md): why the gate blocked a call
