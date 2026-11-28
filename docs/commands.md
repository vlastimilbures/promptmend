# Commands

This page is the CLI reference: `improve` (what every trigger runs) and its options, the
contract a script can rely on, every management command, and the exit codes. You need it when
you script PromptMend, try a profile or model from a terminal, or want to know what a command
does before you run it. Run any command with `--help` for its full option list.

## Contents

- [improve](#improve)
- [Scripting contract](#scripting-contract)
- [persona](#persona)
- [Management commands](#management-commands)
- [Reading stats](#reading-stats)
- [Timing start-up](#timing-start-up)
- [How management commands behave](#how-management-commands-behave)
- [Exit codes](#exit-codes)

## improve

The same CLI the triggers call works on its own, which is handy for trying profiles and
models:

```bash
echo "summarise the Q3 incident log for the ops team" | promptmend improve
promptmend improve --provider ollama --profile general --source clipboard
promptmend improve --model openai/gpt-6-luna@openai --source argument --text "your draft"
promptmend improve --tier pro --source clipboard   # the reasoning model behind -ip-
```

| Option | Default | Meaning |
|--------|---------|---------|
| `--provider` | `PROMPT_PROVIDER` (`PROMPT_PRO_PROVIDER` with `--tier pro`) | `ollama`, `lmstudio`, `openrouter`, `anthropic`, `openai` |
| `--profile` | `PROMPT_PROFILE` (`PROMPT_PRO_PROFILE`, if set, with `--tier pro` on the pro tier's own model) | `default`, `general`, or one of [your own profiles](profiles.md#your-own-profiles) |
| `--model` | the provider's configured model | Overrides the model of whichever provider runs; `model@endpoint` also pins the OpenRouter endpoint (`@auto` unpins) |
| `--tier` | `standard` | OpenRouter, Anthropic and OpenAI: `pro` uses the `OPENROUTER_PRO_*` settings on OpenRouter, and the provider's own settings with the pro time limit and profile on Anthropic and OpenAI (see [the pro tier](usage.md#the-pro-tier)) |
| `--effort` | `OPENROUTER_REASONING_EFFORT` (`OPENROUTER_PRO_REASONING_EFFORT` with `--tier pro`) or `OPENAI_REASONING_EFFORT` | OpenRouter and OpenAI only: `none`, `minimal`, `low`, `medium`, `high` |
| `--max-tokens` | `OPENROUTER_MAX_TOKENS` (`OPENROUTER_PRO_MAX_TOKENS`, if set, with `--tier pro`), `ANTHROPIC_MAX_TOKENS` or `OPENAI_MAX_TOKENS`; no cap for Ollama and LM Studio | Output cap for this call (Ollama: `num_predict`) |
| `--timeout` | `PROMPT_TIMEOUT_SECONDS` (`PROMPT_PRO_TIMEOUT_SECONDS` with `--tier pro`) | Time limit in seconds for this call, retry included |
| `--source` | `argument` with `--text`; else `stdin` when stdin is a pipe or a file; else `clipboard` | `clipboard`, `stdin` or `argument` |
| `--text` | — | The draft, with `--source argument` (or no `--source`) |
| `--output` | `PROMPT_OUTPUT` (`paste`) | `paste` prints the rewrite; `clipboard` copies it and prints only markers (see [clipboard output](usage.md#output-paste-or-clipboard)). It overrides `PROMPT_OUTPUT` for this call, so a script that wants the rewrite on the clipboard passes `--output clipboard` |
| `--copy` | off | With `paste` output, also copy the rewrite to the clipboard, without its notes (`sent despite`, the output limit); on success, what is printed does not change. With `clipboard` output (`--output clipboard` or `PROMPT_OUTPUT=clipboard`) it has no effect, so a script that wants the rewrite both printed and copied passes `--output paste --copy`. If the copy fails, the rewrite is still printed after the error marker |
| `--allow-flagged` | off | Send this one draft despite soft findings of the gate (what `-iok-` passes; see [overriding a block](privacy.md#overriding-a-block)) |
| `--fail-on-marker` | off | For scripts: exit 1 when the run ends in a failure marker instead of a rewrite; the output is the same (see [Scripting contract](#scripting-contract)) |

`--tier pro` with Ollama or LM Studio, and `--effort` (other than `default`) with any
provider but OpenRouter and OpenAI, are refused with a `[promptmend: …]` marker, and no call
is made: no other provider has a pro tier, or a reasoning-effort control.

## Scripting contract

`improve` is built for Espanso, which cannot show stderr or exit codes, so it always gives
Espanso something to paste:

- Output is UTF-8 with no trailing newline.
- Every failure is printed as one `[promptmend: …]` marker, with exit code 0 (1 with
  `--fail-on-marker`).
- Without `--source`, a draft piped or redirected to stdin is read; from a terminal, the
  clipboard. An empty pipe gives `[promptmend: Input is empty]` and sends nothing; one left
  open ends at the time limit with `[promptmend: stdin was not closed within …s, so nothing
  was sent: …]`, even when a draft arrived before. On Windows a
  null stdin (`< NUL`) counts as a terminal and reads the clipboard. A script should pass
  `--source` explicitly.
- Drafts over 50,000 characters are refused.
- If the model stops at its output limit, the partial rewrite is printed with
  `[promptmend: the reply hit the model's output limit and is cut off]` at the end.

For scripts, the exit code says nothing by default: `improve` exits 0 whether or not it
rewrote. Pass `--fail-on-marker` to exit 1 whenever a failure marker is printed: a bad
setting, a blocked or failed call, an empty draft, a timeout, and also a copy that failed or
stalled (`--copy`, or `clipboard` output, whose deliverable is the clipboard), even though
the rewrite is then printed after that marker. Only the `sent despite` note and the
output-limit note are not failures: a rewrite with them still exits 0. Stdout and stderr stay
exactly as without the flag. No Espanso match passes it, since Espanso fails the expansion on
a nonzero exit.

Without the flag, a run failed when its whole output is one `[promptmend: …]` marker. A
rewrite starts with a marker only after `--allow-flagged` (`[promptmend: sent despite: …]`)
or a failed or stalled copy (`--copy` or `clipboard` output: `[promptmend: Clipboard
unavailable: …; the rewrite is pasted instead]`, `[promptmend: the clipboard did not answer
in time, …; it is pasted instead]`), and ends with one only when it is cut off. This prefix is stable. With `clipboard` output, a successful run prints nothing,
or only those markers, and the rewrite is on the clipboard. A script passes `--output` as it
passes `--source`, since `PROMPT_OUTPUT` decides otherwise: `--output clipboard` to get the
rewrite on the clipboard, `--output paste` to read it from stdout (with `--copy` to copy it
as well).

## persona

`promptmend persona` prints `PROMPT_PERSONA`, which the `-p-` snippet inserts. It reads the
persona alone, so another setting's bad value does not hide it; only a settings file it cannot
read gives the `[role]` placeholder. See [Persona](profiles.md#persona).

## Management commands

| Command | What it does |
|---------|--------------|
| `promptmend --version` | Prints the installed version |
| `promptmend` / `promptmend ui` | Opens the [full-screen interface](interface.md) when stdin and stdout are a terminal. Otherwise a bare `promptmend` prints the help and exits 2, and `ui` exits 3 |
| `promptmend setup` | First run, in a terminal one step at a time on a full screen ([Setup](interface.md#setup)): which triggers you use (cloud, local or both; local saves `PROMPT_LOCAL_ONLY=true`, cloud and both remove it), each API key those triggers need (hidden), the default profile and paste or clipboard output (these choices are saved in `config.toml`), the match files it deploys only when you press Deploy, and a smoke test on the path of your triggers (`-i-`'s for cloud, the first local trigger's for local, both for both) against a stub on `127.0.0.1` (never a paid call, never your real key). Names a `.env` left in the config folder (or an editable install's checkout), which is no longer read. The options prefill the wizard: `--use cloud\|local\|both`, `--profile` and `--output paste\|clipboard` preselect a choice, `--deploy` focuses Deploy, `--no-deploy` and `--no-smoke-test` skip that step. Without a terminal it exits 3. `--non-interactive` asks nothing and prints each step: the given options (without `--use`, `PROMPT_LOCAL_ONLY` stays as it is), the key only with `--api-key-stdin` (OpenRouter's; local takes none), and the deploy a preview unless `--deploy`. `--provider` is deprecated and goes in a later release: with `--non-interactive` it still saves `PROMPT_PROVIDER` (which no trigger reads; it says so) and `--api-key-stdin` then reads that provider's key; on a terminal it is not saved (Welcome says so) and, without `--use`, preselects local for Ollama or LM Studio and cloud otherwise |
| `promptmend config show [--raw]` | Every setting, its value and where it comes from (default, a file or the environment), and which lower files it overrides. Keys and the persona are shown only as set or not set; a value set to empty on purpose reads `(empty)` |
| `promptmend config get NAME` / `set NAME VALUE` / `unset NAME` | Reads one setting (the raw value; an empty line for an empty one); saves it in `config.toml` after checking it as the CLI reads it; removes it so the default applies. A key is refused here. `set` warns on stderr, naming the findings only, when the change leaves a `PROMPT_PERSONA` that `config validate` would flag; the value stays saved |
| `promptmend config validate` | Checks the settings as `improve` reads them and the profiles they name, and flags a `PROMPT_PERSONA` that the gate's patterns match. A gate pack that `PROMPT_GATE_PACKS` leaves off prints a `note:` line, not a problem |
| `promptmend secrets set NAME [--stdin]` / `status` / `remove NAME` | Saves a key from a hidden prompt or stdin (never an argument); shows whether each key is set and where from, never the value; deletes one |
| `promptmend profiles list` | Lists built-in and your own profiles and their state |
| `promptmend espanso deploy [--dry-run]` / `status [--launcher PATH]` / `detach [--dry-run]` | Writes, checks or removes the match files; each `--dry-run` shows what it would do and changes nothing. `status --launcher` names the CLI path to compare the matches against. See [Managing the match files](usage.md#managing-the-match-files) |
| `promptmend stats [--by trigger\|provider\|model\|day] [--json] [--verbose]` | Calls, latency, tokens and costs from the [usage history](privacy.md#usage-history); `--verbose` also says what the history records and how to read the numbers (below) |
| `promptmend history last [-n N] [--json]` | The newest runs first (10 by default, `-n` from 1 to 1000), one line each: the time in this machine's local time zone, the trigger, the outcome, the error kind, the latency in ms, the provider and the model. After a retry, the provider, model and error kind are the last request's; a run that made no request (`persona`, a draft blocked before it was sent) shows `-` there. With `PROMPT_HISTORY=false` it says so and still lists what was recorded before. `--json` gives the same runs with their stored names (`occurred_at_utc`, `latency_ms`, `attempts`, …) and the times in UTC, as stored |
| `promptmend history export [--format json\|csv] [--file FILE]` / `prune [--older-than DAYS]` / `reset` | Exports the history (metadata only), deletes old records (asking first when the age is shorter than `PROMPT_HISTORY_RETENTION_DAYS`), or deletes them all |
| `promptmend doctor [--json] [--timing [--runs N]]` | Version and whether a newer release exists (information only, with the update command for your install channel; `--json` adds `latest`, `update_available` and `checked_at`; one pypi.org request a day (an hour after a failure), `PROMPT_UPDATE_CHECK=false` turns it off), CLI path and install channel, whether the `promptmend` first on PATH is this install's (an older CLI there runs with other settings), config validity, a gate pack that `PROMPT_GATE_PACKS` leaves off (`gate_packs`, information only), keys set or not (a missing key names the deployed triggers that need it), whether `PROMPT_PRO_PROVIDER` reaches the deployed `-ip-` (`pro_provider`: an older match file still names OpenRouter until a deploy), whether the Ollama or LM Studio a deployed trigger uses answers (on Windows also a `localhost` URL, which costs about 2 s a call: `--json` lists such settings in `slow_localhost`), Espanso found and running, each deployed match file (`in sync`, `stale`, `modified`, `missing`, or `foreign` for one no deploy wrote; a linked Espanso folder is compared as the folder it points to), launcher drift, history health, SQLite version and the history's journal mode, the folders, what https certificates are checked against (`tls`: the operating system's trust store, the bundle `SSL_CERT_FILE`/`SSL_CERT_DIR` names, or certifi's where the trust store cannot load), and a clipboard read test that reports only the length (`--no-clipboard` skips it). The text ends with a verdict line: how many problems (failed checks) and warnings, and the first in this order to fix (`Fix first: <check>: <message>`; with warnings only, `Check first:`), `No problems found.` when nothing is wrong, or, when no match files are deployed and a key or the launcher is wrong or the match files are missing from an Espanso folder that exists, `Next:` and `promptmend setup` (unless another check failed, such as the settings, the profiles or the history: that one is fixed first; a missing Espanso folder or match files that are not ours get `Check first`); `--json` carries it as `verdict` (`problems`, `warnings`, `first`, `setup`, `line`). Safe to paste into an issue: it never shows a key, your persona or clipboard text. `--timing` also measures how fast this install starts (below); without `--timing`, `doctor` runs no PromptMend child and times nothing, and `--runs` without it is a usage error |

## Reading stats

`stats` reports local observations on this device, not provider billing: check your
provider's dashboard for what you were charged.

- Costs are summed per unit as the provider reported them. OpenRouter reports credits, which
  are never converted to USD; a BYOK call's upstream cost (USD) is kept in the history and its
  export but not added to the totals. Estimates from `prices.toml` are shown apart.
- An unknown cost is counted as unknown (`N attempt(s) with an unknown cost`), never as 0.
- A call counts once the CLI rendered its output, which does not mean it was pasted (with
  clipboard output, that the rewrite was copied).
- Triggers that name a provider (`-i-`, `-if-`, `-iok-`, `-il-`, `-ilm-`) ignore
  `PROMPT_PROVIDER`, so changing it does not move their calls to another provider. `-ip-`
  names none and runs on `PROMPT_PRO_PROVIDER`, so changing that one does, once deployed.
- By trigger, a real run from the interface's Try tab is listed as `-try-`, and a bare
  `promptmend improve` as `no trigger (direct call)`.

## Timing start-up

`promptmend doctor --timing` measures how long this install takes to start the way Espanso
runs it, for example when endpoint scanning slows every start on Windows. After the checks it
runs, as child processes, `promptmend --version`, `promptmend persona` and the `-i-` trigger's
`improve` (`--source argument` with the draft as an argument, so the clipboard read a real
trigger makes is not included), each one first through this install's launcher (the one a
deploy writes into the match files, or `--launcher`) and then through the interpreter
directly (`python -P -m promptmend.cli`; a frozen build's exe is both, so it runs once). The difference
between the two rows is the cost of the launcher. It also times importing the interface
(`import promptmend.tui.app` in a child), and reports how long each of `doctor`'s own checks
took.

- Every child runs against a stub on `127.0.0.1`, as setup's smoke test does: no provider is
  called, only a placeholder key is set, the usage history is off, `PROMPT_PERSONA` is empty
  and the output is paste (your clipboard is never written). `PROMPT_LOCAL_ONLY` and
  `PROMPT_EXTRA_PATTERNS` are turned off too, so they cannot refuse or block the stub call;
  that is safe only because every provider URL is the loopback stub and every key the
  placeholder, which `doctor` checks before it starts a child. Your other settings apply.
- The numbers are wall-clock milliseconds: `cold` is the first run in this `doctor` call,
  `warm` the median of the `--runs` (default 5, 1 to 50) runs that follow. They cover start-up
  and PromptMend's own work, never the provider's time, so a real trigger takes longer by
  however long the model takes to answer. A start after a reboot or a fresh install can be
  slower than `cold`, since earlier runs have already warmed the disk cache. The interpreter
  row runs after the launcher's runs of the same command, so its `cold` is effectively warm.
- Through the interpreter, `--version` loads the whole CLI, while the launcher answers it
  before loading anything else: compare `persona` or `improve` for the launcher's own cost.
- When `--version` through the launcher fails or prints another version, the launcher is not
  this CLI, so its `persona` and `improve` rows are `skipped`. When Espanso's deployed matches
  call another launcher than the one timed (launcher drift), a note says so.
- A run that fails (a nonzero exit, anything on stderr, an unexpected output, or no answer in
  60 seconds) is shown as `failed` with the reason; that command's remaining runs are
  skipped, the others still run. The exit code still comes from the checks alone.
- The text report prints the checks first and then runs the timing. `--json` adds a `timing`
  key (the report's `schema_version` stays 1): `warm_runs`, `launcher` (null when it is not
  timed, with `launcher_note` saying why), `drift_note` (null unless the deployed matches
  call another launcher), `interpreter`, `runs` (each with `command`, `via`, `status`, `cold_ms`, `warm_median_ms`, `warm_runs` and
  `message`) and `checks_ms`.

## How management commands behave

Unlike `improve` and `persona`, the management commands print errors to stderr and use
ordinary exit codes.

- None of them asks a question without a terminal: pass `--yes`, `--stdin` or
  `--non-interactive` instead.
- Each runs on a broken `.env` or `config.toml` and reports what is wrong, without a traceback.
- Colour is off when `NO_COLOR` is set or output is not a terminal.
- An option or a mistyped argument that looks like a key is refused and never repeated in an
  error.
- An error is one `error:` line. A key, token, password or card number in it, and anything
  your `PROMPT_EXTRA_PATTERNS` match, is shown as `<redacted, N chars>`, so the line is safe
  to paste into a bug report.

## Exit codes

| Exit code | Meaning |
|-----------|---------|
| 0 | Done |
| 1 | Failed or refused, or you declined a confirmation |
| 2 | Usage error: an unknown option, setting or value |
| 3 | An answer was needed but stdin is not a terminal, or `ui` or `setup` (without `--non-interactive`) ran without a terminal |
| 4 | `doctor` or `config validate` found a problem |

`improve` and `persona` exit 0 (see [Scripting contract](#scripting-contract)); only
`improve --fail-on-marker` exits 1, when the run printed a failure marker. A usage error (an
unknown option, or a value given to a flag, such as `--fail-on-marker=1`) exits 2 with
nothing on stdout and stderr.

## See also

- [Usage](usage.md): the triggers that run `improve`
- [Configuration](configuration.md): every setting the commands read
- [Interface](interface.md): the same commands behind buttons
