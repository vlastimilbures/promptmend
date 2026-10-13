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
- [shell](#shell)
- [Reading stats](#reading-stats)
- [How management commands behave](#how-management-commands-behave)
- [Exit codes](#exit-codes)
- [The prompt-workflow alias](#the-prompt-workflow-alias)

## improve

The same CLI the triggers call works on its own, which is handy for trying profiles and
models:

```bash
echo "summarise the Q3 incident log for the ops team" | promptmend improve --source stdin
promptmend improve --provider ollama --profile general --source clipboard
promptmend improve --model openai/gpt-6-luna@openai --source argument --text "your draft"
promptmend improve --tier pro --source clipboard   # the reasoning model behind -ip-
```

| Option | Default | Meaning |
|--------|---------|---------|
| `--provider` | `PROMPT_PROVIDER` | `ollama`, `lmstudio`, `openrouter`, `anthropic` |
| `--profile` | `PROMPT_PROFILE` (`PROMPT_PRO_PROFILE`, if set, with `--tier pro` on `OPENROUTER_PRO_MODEL`) | `default`, `general`, or one of [your own profiles](profiles.md#your-own-profiles) |
| `--model` | the provider's configured model | Overrides the model of whichever provider runs; `model@endpoint` also pins the OpenRouter endpoint (`@auto` unpins) |
| `--tier` | `standard` | OpenRouter only: `pro` uses the `OPENROUTER_PRO_*` settings |
| `--effort` | `OPENROUTER_REASONING_EFFORT` (`OPENROUTER_PRO_REASONING_EFFORT` with `--tier pro`) | OpenRouter only: `none`, `minimal`, `low`, `medium`, `high` |
| `--max-tokens` | `OPENROUTER_MAX_TOKENS` (`OPENROUTER_PRO_MAX_TOKENS`, if set, with `--tier pro`) or `ANTHROPIC_MAX_TOKENS`; no cap for Ollama and LM Studio | Output cap for this call (Ollama: `num_predict`) |
| `--timeout` | `PROMPT_TIMEOUT_SECONDS` (`PROMPT_PRO_TIMEOUT_SECONDS` with `--tier pro`) | Time limit in seconds for this call, retry included |
| `--source` | `clipboard` | `clipboard`, `stdin` or `argument` |
| `--text` | — | The draft, with `--source argument` |
| `--output` | `PROMPT_OUTPUT` (`paste`) | `paste` prints the rewrite; `clipboard` copies it and prints only markers (see [clipboard output](usage.md#output-paste-or-clipboard)) |
| `--copy` | off | With `paste` output, also copy the result to the clipboard (no effect with `clipboard`); if the copy fails, the result is still printed after the error marker |
| `--allow-flagged` | off | Send this one draft despite soft findings of the gate (what `-iok-` passes; see [overriding a block](privacy.md#overriding-a-block)) |

`--tier pro` and `--effort` (other than `default`) with any provider but OpenRouter are
refused with a `[promptmend: …]` marker, and no call is made: no other provider has a pro tier
or a reasoning-effort control.

## Scripting contract

`improve` is built for Espanso, which cannot show stderr or exit codes, so it always gives
Espanso something to paste:

- Output is UTF-8 with no trailing newline.
- Every failure is printed as one `[promptmend: …]` marker, with exit code 0.
- Drafts over 50,000 characters are refused.
- If the model stops at its output limit, the partial rewrite is printed with
  `[promptmend: the reply hit the model's output limit and is cut off]` at the end.

For scripts, the exit code says nothing: `improve` exits 0 whether or not it rewrote. A run
failed when its whole output is one `[promptmend: …]` marker. A rewrite starts with a marker
only after `--allow-flagged` (`[promptmend: sent despite: …]`) and ends with one only when it
is cut off. This prefix is stable. With `clipboard` output, a successful run prints nothing,
or only those markers, and the rewrite is on the clipboard.

## persona

`promptmend persona` prints `PROMPT_PERSONA`, which the `-p-` snippet inserts. It reads the
persona alone, so another setting's bad value does not hide it; only a settings file it cannot
read gives the `[role]` placeholder. See [Persona](profiles.md#persona).

## Management commands

| Command | What it does |
|---------|--------------|
| `promptmend --version` | Prints the installed version |
| `promptmend` / `promptmend ui` | Opens the [full-screen interface](interface.md) when stdin and stdout are a terminal. Otherwise a bare `promptmend` prints the help and exits 2, and `ui` exits 3 |
| `promptmend shell` | A command line with completion and live help that runs each command in this terminal; see [shell](#shell). Exits 3 without a terminal |
| `promptmend setup` | First run, in a terminal one step at a time on a full screen ([Setup](interface.md#setup)): provider and default profile (saved in `config.toml`), each API key the triggers need (hidden), the match files it installs only when you press Install, and a smoke test against a stub on `127.0.0.1` (never a paid call, never your real key). Offers to migrate a `.env`, and for an earlier checkout install it finds (or the one `--migrate-from PATH` names) to copy its settings and edited profiles and to retire its `.env` once the match files no longer run it, changing nothing unless you say yes. The options prefill the wizard: `--provider` and `--profile` preselect a choice, `--deploy` focuses Install, `--no-deploy` and `--no-smoke-test` skip that step. Without a terminal it exits 3. `--non-interactive` (or its alias `--plain`) asks nothing and prints each step: the given options or the current settings, the key only with `--api-key-stdin`, the deploy a preview unless `--deploy`, and a `.env` migration, copy, profile copy or retire as a to-do with its command |
| `promptmend config show [--raw]` | Every setting, its value and where it comes from (default, a file or the environment), and which lower files it overrides. Keys and the persona are shown only as set or not set; a value set to empty on purpose reads `(empty)` |
| `promptmend config get NAME` / `set NAME VALUE` / `unset NAME` | Reads one setting (the raw value; an empty line for an empty one); saves it in `config.toml` after checking it as the CLI reads it; removes it so the default applies. A key is refused here. `set` warns on stderr, naming the findings only, when the change leaves a `PROMPT_PERSONA` that `config validate` would flag; the value stays saved |
| `promptmend config validate` | Checks the settings as `improve` reads them and the profiles they name, and flags a `PROMPT_PERSONA` that the gate's patterns match |
| `promptmend config migrate` / `rollback` | Moves the `.env` in use to `config.toml` and its keys to the secret store, with a backup, or undoes that. See [Migrate a .env](configuration.md#migrate-a-env). `--from PATH` copies an earlier checkout's `.env` into every setting still at its default; that `.env` stays in place, so the old triggers keep working until you deploy again (see [From a checkout install](install.md#from-a-checkout-install)) |
| `promptmend config retire --from PATH` | Moves that checkout's `.env` into the backup once its settings were copied. Refused while a match file still runs the checkout's CLI (`promptmend espanso deploy` first); same preview and `--yes --preview-token` as migrate, and `rollback` puts it back |
| `promptmend secrets set NAME [--stdin]` / `status` / `remove NAME` | Saves a key from a hidden prompt or stdin (never an argument); shows whether each key is set and where from, never the value; deletes one |
| `promptmend profiles list` / `migrate` | Lists built-in and your own profiles and their state; copies profiles you added or edited in a checkout to your profile folder (copies only, never overwrites) |
| `promptmend espanso deploy [--dry-run]` / `status` / `detach` | Writes, checks or removes the match files. See [Managing the match files](usage.md#managing-the-match-files) |
| `promptmend stats [--by trigger\|provider\|model\|day] [--json] [--verbose]` | Calls, latency, tokens and costs from the [usage history](privacy.md#usage-history); `--verbose` also says what the history records and how to read the numbers (below) |
| `promptmend history export [--format json\|csv] [-o FILE]` / `prune [--older-than DAYS]` / `reset` | Exports the history (metadata only), deletes old records (asking first when the age is shorter than `PROMPT_HISTORY_RETENTION_DAYS`), or deletes them all |
| `promptmend doctor [--json]` | Version and whether a newer release exists (information only, with the update command for your install channel; `--json` adds `latest`, `update_available` and `checked_at`; one pypi.org request a day (an hour after a failure), `PROMPT_UPDATE_CHECK=false` turns it off), CLI path and install channel, config validity, keys set or not (a missing key names the deployed triggers that need it), whether the Ollama or LM Studio a deployed trigger uses answers, Espanso found and running, each deployed match file (`in sync`, `stale`, `modified`, `missing`), launcher drift, history health, SQLite version and the history's journal mode, the folders, and a clipboard read test that reports only the length (`--no-clipboard` skips it). Safe to paste into an issue: it never shows a key, your persona or clipboard text |

## shell

`promptmend shell` is a plain command line for the terminal, for when you would rather not
use the full-screen interface. It completes and explains commands the way Home's
[command line](interface.md#command-line) does, but runs each one in the same terminal.

- Type a command without `promptmend` (a leading `promptmend` is dropped). Tab completes the
  word you are typing (commands, options, setting names after `config set`, key names only
  after `secrets set` or `secrets remove`, profile names); the Right arrow takes the grey
  suggestion. The bar below the line shows the command's usage and help as you type, or the
  error the CLI would print.
- Enter runs the command as `promptmend …` in this terminal. A command that asks (such as
  `espanso deploy`, `config migrate` or `secrets set NAME`, which asks for the key hidden)
  asks here. A nonzero exit prints `exit N`; Ctrl-C stops the running command, not the
  shell. A usage error runs nothing.
- Refused: `improve` and `persona` (the triggers run them, and `improve` reads the
  clipboard; to try a rewrite, use the interface's [Try tab](interface.md#try-tab)) and
  `shell` itself. Their `--help` runs only when the rest of the line parses.
- A line that looks like it holds a key (the built-in patterns and your
  `PROMPT_EXTRA_PATTERNS`, read when the shell starts and after each command),
  `secrets set` with anything after the key's name,
  or `config set|get|unset` with a key's name and a value never runs: Enter replaces it on
  screen with `<value withheld>`. Up and Down go through this session's lines that ran; the
  history stays in memory, never in a file, and keeps no line that was refused or did not
  parse.
- An empty line or `help` lists a few useful commands; `exit`, `quit` or Ctrl-D leaves (exit
  0). Ctrl-C at the prompt clears the line.

## Reading stats

`stats` reports local observations on this device, not provider billing: check your
provider's dashboard for what you were charged.

- Costs are summed per unit as the provider reported them. OpenRouter reports credits, which
  are never converted to USD; a BYOK call's upstream cost (USD) is kept in the history and its
  export but not added to the totals. Estimates from `prices.toml` are shown apart.
- An unknown cost is counted as unknown (`N attempt(s) with an unknown cost`), never as 0.
- A call counts once the CLI rendered its output, which does not mean it was pasted (with
  clipboard output, that the rewrite was copied).
- Triggers that name a provider (`-i-`, `-ip-`, `-if-`, `-iok-`, `-il-`, `-ilm-`) ignore
  `PROMPT_PROVIDER`, so changing it does not move their calls to another provider.
- By trigger, a real run from the interface's Try tab is listed as `-try-`, and a bare
  `promptmend improve` as `no trigger (direct call)`.

## How management commands behave

Unlike `improve` and `persona`, the management commands print errors to stderr and use
ordinary exit codes.

- None of them asks a question without a terminal: pass `--yes`, `--stdin` or
  `--non-interactive` instead.
- `config migrate`, `rollback` and `retire` show a preview first and apply only once you
  confirm it; in a script, pass `--yes --preview-token <token>` from that preview.
- Each runs on a broken `.env` or `config.toml` and reports what is wrong, without a traceback.
- Colour is off when `NO_COLOR` is set or output is not a terminal.
- An option or a mistyped argument that looks like a key is refused and never repeated in an
  error.

## Exit codes

| Exit code | Meaning |
|-----------|---------|
| 0 | Done |
| 1 | Failed or refused, or you declined a confirmation |
| 2 | Usage error: an unknown option, setting or value |
| 3 | An answer was needed but stdin is not a terminal, or `ui` or `shell` ran without a terminal |
| 4 | `doctor` or `config validate` found a problem |

`improve` and `persona` always exit 0 (see [Scripting contract](#scripting-contract)).

## The prompt-workflow alias

The command's old name, `prompt-workflow`, stays installed as a deprecated alias of
`promptmend` until 1.0.0. The triggers and `--help`/`--version` behave exactly the same; every
other command first prints one line on stderr: "`prompt-workflow` is deprecated; use
`promptmend` (removed in 1.0.0)". See
[From 0.18 or earlier](install.md#from-018-or-earlier-the-rename).

## See also

- [Usage](usage.md): the triggers that run `improve`
- [Configuration](configuration.md): every setting the commands read
- [Interface](interface.md): the same commands behind buttons
