# Usage

This page covers what you do every day: the triggers, when they fire, what they send, how the
result arrives, and how to add triggers of your own. It ends with how PromptMend manages the
match files it writes into Espanso. Read it after [installing](install.md) and running
`promptmend setup`.

## Contents

- [Triggers](#triggers)
- [When a trigger fires](#when-a-trigger-fires)
- [What gets sent](#what-gets-sent)
- [While it runs](#while-it-runs)
- [Output: paste or clipboard](#output-paste-or-clipboard)
- [The -if- form](#the--if--form)
- [Your own triggers](#your-own-triggers)
- [Managing the match files](#managing-the-match-files)

## Triggers

| Trigger | What it does | Provider | Key | Leaves the machine | Profile |
|---------|--------------|----------|-----|--------------------|---------|
| `-i-` | Rewrites the clipboard into the golden template | OpenRouter | `OPENROUTER_API_KEY` | Yes, [gated](privacy.md#the-data-protection-gate) | `PROMPT_PROFILE` (`default`) |
| `-ip-` | The same rewrite on the pro tier (a reasoning model, slower) | OpenRouter | `OPENROUTER_API_KEY` | Yes, [gated](privacy.md#the-data-protection-gate) | `PROMPT_PRO_PROFILE`, else `PROMPT_PROFILE` |
| `-if-` | The same rewrite; a [form](#the--if--form) picks model, effort, tokens and timeout | OpenRouter | `OPENROUTER_API_KEY` | Yes, [gated](privacy.md#the-data-protection-gate) | as `-ip-` |
| `-iok-` | `-i-`, sent once despite a flagged label, ID, email or IBAN ([overriding a block](privacy.md#overriding-a-block)) | OpenRouter | `OPENROUTER_API_KEY` | Yes, gated, flagged soft findings let through | `PROMPT_PROFILE` |
| `-il-` | General prompt improvement, fully local | Ollama | — | No (see below) | `general` |
| `-ilm-` | General prompt improvement, fully local | LM Studio | — | No (see below) | `general` |
| `-ic-` | General improvement through Claude (commented out by default) | Anthropic | `ANTHROPIC_API_KEY` | Yes, [gated](privacy.md#the-data-protection-gate) | `general` |
| `-io-` | The golden-template rewrite through OpenAI directly (commented out by default) | OpenAI | `OPENAI_API_KEY` | Yes, [gated](privacy.md#the-data-protection-gate) | `PROMPT_PROFILE` (`default`) |
| `-p-` | An empty golden template to fill in, opening with your persona | — | — | No | — |
| `-risk-` | A static risk-analysis prompt scaffold to fill in | — | — | No | — |

`PROMPT_PRO_PROFILE` is empty by default, so `-ip-` and `-if-` use `PROMPT_PROFILE` too. In
`-if-`, a model other than `OPENROUTER_PRO_MODEL` always gets `PROMPT_PROFILE`. The local
triggers always use `general`. See [Profiles](profiles.md) for what each profile does.

"Gated" means the [data-protection gate](privacy.md#the-data-protection-gate) scans the draft
first and blocks keys, tokens, passwords, cards, IDs, confidentiality labels and your own
patterns. `-iok-` lets one draft through when every finding is a label, an ID, an email
address or an IBAN; keys, tokens, passwords, cards and your own patterns stay blocked.
`ALLOW_CLOUD_OVERRIDE=true` turns the gate off for every call, so prefer `-iok-`. `-il-` and
`-ilm-` keep the draft on your machine only while their base URL is loopback and the Ollama
model has no `cloud` tag; otherwise the draft leaves it and the gate runs as for a cloud call
(see [Local only](privacy.md#local-only)).

One rule decides which triggers a deploy turns on: a trigger is active unless it needs a key
you opt into. So `-il-` and `-ilm-` are always active, even with no Ollama or LM Studio
installed, and `-ic-` and `-io-` ship commented out (uncomment one once its key,
`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, is set). A local trigger whose server does not answer
pastes an error marker. `promptmend doctor` (`local_servers`) and the interface's Triggers tab
(State `needs Ollama`, `needs LM Studio`) say so; they ask only a server on this machine, with one short request, never a trigger.
`doctor`'s `keys` check names the deployed triggers that need each missing key.

For a draft that pastes an email, a thread or a document to work on, use `-ip-`. The pro
tier copies the pasted material into `INPUTS` word for word (up to about 60 lines), while
`-i-`'s faster model often summarises a short pasted email instead (CONTRIBUTING, "Known gaps
in the default prompt", #42).

Each match has a label starting with `PromptMend:`, which Espanso's search bar
(Alt+Space / Option+Space by default) shows instead of the `{{output}}` placeholder.

## When a trigger fires

Triggers expand only at the start of a word: after a space, tab, newline, punctuation
(`. , ? ! : ; ' "`) or a bracket, or as the first thing typed after clicking into a field.

- Text such as `a[n-i-1]` or `only-if-cached` does not fire them.
- `s[-i-1]` or `x = -i-1` still does.
- If a trigger follows anything else (a letter, digit, `-`, `=`, `/` …), type a space first.

## What gets sent

The improve triggers (`-i-`, `-ip-`, `-if-`, `-iok-`, `-il-`, `-ilm-`) send your current
clipboard as it is, whatever it holds, so copy the draft first. If you forgot, the last thing
you copied goes instead.

- On macOS and Windows, an item a password manager marked as concealed is refused and cleared
  from the clipboard (`[promptmend: The clipboard held a password-manager item …]`) when the
  check can tell. See [Clipboard safety](privacy.md#clipboard-safety).
- Cloud triggers pass through the [data-protection gate](privacy.md#the-data-protection-gate)
  first.
- Drafts over 50,000 characters are refused, so an accidental copy of a log or document is
  not sent.

To rewrite text in place, select it, copy it (Cmd+C / Ctrl+C) and type `-i-`. As in any
editor, the first character you type replaces the selection, and Espanso then replaces the
trigger with the rewrite.

## While it runs

A rewrite takes a few seconds on `-i-` and longer on `-ip-` (measured times are in the
[benchmark](benchmark.md#at-a-glance)), at most the call's time limit
(`PROMPT_TIMEOUT_SECONDS`, `PROMPT_PRO_TIMEOUT_SECONDS` for the pro tier).

Do not type or switch windows while it runs. Espanso pastes the result wherever the focus is
when the answer arrives, and other triggers do not expand until it finishes. Writing the
[usage history](privacy.md#usage-history) adds up to 0.25 s after the output (1 s once, for
the write that creates the file).

[Clipboard output](#output-paste-or-clipboard) removes the misplaced paste: nothing is pasted
on success, so switching windows during a long wait is safe. Espanso still erases the trigger
where the focus is, so do not type.

## Output: paste or clipboard

By default every improve trigger pastes the rewrite in place of the trigger. With
`PROMPT_OUTPUT=clipboard` the rewrite goes on the clipboard instead:

```bash
promptmend config set PROMPT_OUTPUT clipboard
promptmend config set PROMPT_OUTPUT paste       # back to pasting
```

In the [interface](interface.md#settings-tab), press `2` for Settings: `PROMPT_OUTPUT` is the
first row; Enter picks paste or clipboard. Home's Output row shows the current mode.

It applies to every improve trigger at once, with no redeploy. Type the trigger as usual:
after the wait the trigger text vanishes, nothing is pasted, and the rewrite is on the
clipboard. Press Cmd+V / Ctrl+V where you want it.

Everything that is not the rewrite still pastes, so you see it where you typed:

- every error marker;
- the `[promptmend: sent despite: …]` note of `-iok-` (the clipboard then holds only the
  rewrite, without the note);
- the cut-off note of a reply that hit its output limit (the clipboard holds the partial
  rewrite).

If the clipboard cannot be written, the rewrite is pasted after a
`[promptmend: Clipboard unavailable: …]` marker, so it is never lost. `-p-` and the other
static snippets are unaffected. For one call, `improve --output paste|clipboard` overrides the
setting.

Three things to know:

- **Privacy:** the rewrite replaces your draft on the clipboard, so a clipboard-history
  manager, Universal Clipboard / Handoff and the Windows cloud clipboard see it, as they saw
  the draft you copied.
- **Knowing when it is ready:** a clipboard manager that notifies you of a new item tells you
  the rewrite has arrived, for example [Vorssaint](https://github.com/vorssaint/vorssaint-utils)
  (free, open source, macOS 14 or later).
- **Espanso setting:** it relies on Espanso restoring your clipboard after it inserts the
  (empty) output, which is the default. `preserve_clipboard: false` in Espanso's
  `config/default.yml` can leave what Espanso inserted on the clipboard in place of the
  rewrite: leave that setting out or set it to `true`.

## The -if- form

`-if-` opens an Espanso form with four dropdowns before the rewrite runs:

| Field | Choices |
|-------|---------|
| Model | `model@endpoint`: the OpenRouter slug plus its endpoint pin; `@auto` leaves routing to OpenRouter. Shipped: `openai/gpt-6-luna@openai`, `openai/gpt-6-luna@auto`, `google/gemini-3.8-flash@google-ai-studio`, `google/gemini-3.5-flash-lite@google-ai-studio/flex` |
| Effort | `none`, `minimal`, `low`, `medium`, `high` |
| Max tokens | `2400`, `4000`, `8000`, `16000` |
| Timeout (s) | `30`, `60`, `120` |

Every list starts with, and defaults to, `default`, which keeps the pro-tier setting:
`OPENROUTER_PRO_MODEL` with its `OPENROUTER_PRO_PROVIDER` pin, `OPENROUTER_PRO_REASONING_EFFORT`,
`OPENROUTER_PRO_MAX_TOKENS` (or `OPENROUTER_MAX_TOKENS` while that is empty, as it is by
default) and `PROMPT_PRO_TIMEOUT_SECONDS`.

The four picks reach the CLI as `--model`, `--effort`, `--max-tokens` and `--timeout`, each
one whole argument, on top of `--tier pro`. In `--model`, the part before `@` is the model and
the part after it replaces the `OPENROUTER_PRO_PROVIDER` pin for this call, so
`google/gemini-3.5-flash-lite@google-ai-studio/flex` runs that model on Google AI Studio's
flex endpoint.

- Many reasoning models count thinking tokens against the max-tokens cap, so pair `high`
  effort with `8000` or more, or the rewrite can come back cut short.
- Espanso waits for the command, and other triggers do not expand until it finishes. A `high`
  effort rewrite on `openai/gpt-6-luna` took about 23 s.

To change the lists, make your own variant (next section). In a checkout, edit them in
`espanso/match/prompts-llm.yml` and deploy again; the tests reject any value the CLI would not
accept.

## Your own triggers

Put your own variants (an `-ic-` for Anthropic, an `-io-` for OpenAI, an `-if-` with other
lists, a trigger with another profile) in a file of your own in Espanso's `match/` folder,
such as `my-prompts.yml`:

1. Copy the match from the deployed `prompts-llm.yml`, which already holds the CLI's absolute
   path.
2. Give it a trigger no other match uses.
3. Keep its `--trigger-id` only if its runs should count as that trigger in the
   [usage history](privacy.md#usage-history); without one, a run is recorded as `direct`.
4. Keep the CLI call a `type: script` var whose `args` list holds the CLI path first and then
   each argument as its own quoted item, as the deployed file has it:

   ```yaml
   vars:
     - name: output
       type: script
       params:
         args: ["/absolute/path/to/promptmend", "improve", "--provider", "ollama", "--source", "clipboard"]
   ```

   Espanso starts a script var directly, with no shell. Do not turn it into a `type: shell`
   `cmd:` line: on Windows Espanso runs those through PowerShell, which cannot run a quoted
   path followed by arguments, so the trigger pastes `[Espanso]: An error occurred during
   rendering`. A trigger you copied from a file deployed by 0.20.0 or earlier (#18) has that shell
   shape; rewrite it this way.

`promptmend espanso deploy` and `detach` handle only the files they deployed
(`prompts-core.yml`, `prompts-llm.yml`, `prompts-template.yml`), so they never change yours;
`status` lists yours as `yours`. Editing a deployed file instead marks it `modified`: deploy
then keeps your copy (or replaces it, with a backup, if you choose ours), so it no longer
receives updates.

In a checkout, you can enable `-ic-` or `-io-` by uncommenting it in
`espanso/match/prompts-llm.yml` and running `promptmend espanso deploy` (or the install script) again.

## Managing the match files

Espanso starts the CLI as a GUI subprocess, without your shell's `PATH` or environment. So
`promptmend espanso deploy` writes the CLI's absolute path into the match files it puts in
Espanso's `match/` folder (found with `espanso path config`, else Espanso's default folder).

It records them in `espanso-manifest.json` in the
[data folder](configuration.md#files-and-folders), and each deployed file starts with
`# promptmend <version> (managed; edit at your own risk)`.

| Command | What it does |
|---------|--------------|
| `promptmend espanso status [--diff]` | Shows each file's state (table below), then each other `.yml`/`.yaml` file in `match/` as `yours` |
| `promptmend espanso deploy` | Shows the plan and a diff, asks, then writes and restarts Espanso. `--yes` skips the question, `--dry-run` stops after the diff; a run with nothing to change does nothing |
| `promptmend espanso detach` | Removes the matches that call the CLI and keeps `-risk-` as a static snippet (`--keep-static`, the default); `--remove-all` removes every file it deployed |

| State | Meaning |
|-------|---------|
| `missing` | Not deployed |
| `in sync` | Deployed by this version, unchanged |
| `stale` | An older deploy of ours, such as after an upgrade; `deploy` updates it |
| `modified` | Ours, but you edited it |
| `foreign` | Not ours |
| `yours` | Another file in `match/` (Espanso's own `base.yml`, your overlays); never touched by `deploy` or `detach` |

A file you edited is never overwritten silently. `deploy` asks whether to:

- keep yours;
- take ours (yours is saved as `<file>.bak-<timestamp>`; only the last 2 of these backups
  are kept);
- write ours side by side as `<file>.promptmend-new`, which Espanso does not load.

With `--yes` it keeps yours unless you pass `--on-conflict ours|side`, and ends with a
`WARNING` naming each file it kept. A file an older release's installer wrote, unedited, is
recognised as ours and updated. `detach` likewise removes only files whose content is still
what was written, and reports any you edited.

The launcher written into the matches is the install channel's stable entry point (uv's tool
bin, Homebrew's `bin/`), never a versioned path an upgrade would remove; `--launcher PATH`
overrides it. A manifest entry for a file that no longer exists (its folder was deleted, or
Espanso now uses another config folder) is forgotten by the next `deploy`; until then `doctor`
mentions it but does not judge its launcher.

## See also

- [Commands](commands.md): `improve` and every management command
- [Privacy](privacy.md): what a trigger sends, and the gate
- [Troubleshooting](troubleshooting.md): a trigger that does not fire or pastes an error
