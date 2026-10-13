# The interface

`promptmend` opens a full-screen terminal interface for setting up and managing PromptMend:
settings and keys, profiles, the Espanso match files, the usage history, diagnostics, and a
tab to try a rewrite before a trigger pastes one. This page covers its tabs, the command line
on Home, the Settings and Try tabs and the keys. Everything it does, a headless command does too (see
[Commands](commands.md)).

## Contents

- [Open it](#open-it)
- [Setup](#setup)
- [Tabs](#tabs)
- [Home](#home)
- [Command line](#command-line)
- [Settings tab](#settings-tab)
- [Recipes and Copy last](#recipes-and-copy-last)
- [Try tab](#try-tab)
- [Keys](#keys)
- [Intro](#intro)
- [Accessibility](#accessibility)

## Open it

Run `promptmend` (or `promptmend ui`) in a terminal. Without a terminal, a bare `promptmend`
prints the help and `ui` exits 3. For the same command line without the full screen, with
each command run in your terminal, use `promptmend shell` (see
[Commands](commands.md#shell)).

The interface does what the management commands do, through the same code. Keys are shown
only as set or not set. Removing a key, deploying, detaching, migrating or deleting history
asks first, in a dialog whose Cancel has the focus. No provider is called except by the Test
call button, which runs `improve` against a stub on `127.0.0.1` with a placeholder key, and by
a Try tab run you confirm.

When it finds an earlier checkout install, it opens a "Previous install" checklist once per
session (also on Home's "Previous install…", always there, whose "Enter a path…" finds a
checkout it did not, or one you skipped): copy the settings, copy the profiles, deploy,
retire the old `.env`. See [From a checkout install](install.md#from-a-checkout-install).

## Setup

`promptmend setup` in a terminal opens the interface on its setup, one step at a time, with
the steps listed on the left. The first time you open `promptmend` with nothing set up yet (no
`config.toml`, no key, no match file), it opens by itself, once; Home's "Setup…" opens it any
time.

| Step | What you do |
|------|-------------|
| Welcome | Read what setup does and what the usage history keeps |
| Earlier settings | Shown only when there is one: move a `.env` into `config.toml` and the secret store, or open the Previous install checklist |
| Provider | Pick the default provider from a list; each line says whether it is cloud or local, whether it needs a key, and which triggers use it |
| Profile | Pick the default profile; each line says how it rewrites |
| API keys | Paste each key the triggers need (hidden); the step names the triggers each key is for |
| Espanso | See the match folder and each file in words (not installed yet, up to date, you edited it), then install them; a file you edited is kept |
| Test | Run `improve` against a stub on `127.0.0.1`; no provider is called |
| Done | A checklist of what is set and what is left, with the command for each, and how to try `-i-` |

Next and Back (or Escape) move between the steps; Enter on a list picks and moves on. Each
change is made through the same code as its command and shows that command. With
`PROMPTMEND_ENV` set, settings stay in that file: Next moves on without saving a provider or
profile and says so. With `--plain`,
or any of `--provider`, `--profile`, `--migrate-from`, `--deploy`/`--no-deploy` or
`--no-smoke-test`, `setup` asks line by line instead (see [Commands](commands.md#management-commands)).

![The setup's provider step](../tests/snapshots/setup-provider.svg)

## Tabs

| Key | Tab | What it shows and does |
|-----|-----|------------------------|
| `1` | Home | Whether you are ready, the most urgent problem, the session log and the command line |
| `2` | Settings | Every setting and key in one list: switch, pick, edit or reset a setting, set or remove a key, migrate a `.env`, run the Test call (see [Settings tab](#settings-tab)) |
| `3` | Profiles | Built-in and your own profiles; pick the default profile, edit a profile in `$VISUAL` or `$EDITOR`, copy a checkout's profiles (it asks for the checkout's folder when none was found) |
| `4` | Triggers | The match files and their states (refreshed as soon as a deploy or detach has written the files; `needs Ollama` or `needs LM Studio` when a local trigger's server does not answer), the diff, deploy and detach |
| `5` | History | Usage statistics by trigger, provider, model or day; export, prune, reset |
| `6` | Diagnostics | Each provider's model, base URL, key and whether the draft leaves this machine; every setting's source; the `doctor` checks, and an import-time check of the CLI |
| `7` | Try | Rewrite a typed draft against a stub or a confirmed provider |

Every button's tooltip shows the command that does the same in a terminal, and every result
starts with that command (`$ promptmend config set …`).

![The Home tab](interface.svg)

## Home

Home says in one line whether you are ready, or names the most urgent problem and the tab that
fixes it. Below, each row has a status word (ok, warn, FAIL) and the tab that fixes it:

- the installed version and whether it is the latest release (`0.21.0 · latest`); a newer
  one shows as `new` (`0.22.0 available  a: how to update`), and the About screen (`a`)
  gives the update command for your install channel. It is no problem: it never changes the
  headline or the header's status. It asks pypi.org once a day
  ([privacy](privacy.md#update-check)); `PROMPT_UPDATE_CHECK=false` turns it off;
- what `-i-` and `-ip-` run (every trigger is listed in [Usage](usage.md#triggers));
- the match files and Espanso;
- the usage history;
- the output mode (`PROMPT_OUTPUT`: paste or clipboard), which the Settings tab switches;
- the `doctor` checks.

The header shows the same status (`ok`, or `1 problem, 2 warnings`). Home also lists this
session's commands, or a few useful ones before you have done anything.

## Command line

Press `c` (from any tab) and type a command, such as `espanso status --diff`.

- It completes the word you are typing; Tab or the Right arrow takes the suggestion. It
  suggests commands, options, setting names after `config set`, key names only after
  `secrets set` or `secrets remove`, and profile names.
- It shows the command's usage and help below, or the error the CLI would print.
- Up and Down go back through what you entered. Escape leaves the line, so the tab keys work
  again.

Enter acts on the command by what it does:

| Kind | Commands | What Enter does |
|------|----------|-----------------|
| Reads, previews, changes a setting or writes an export | `doctor`, `config show\|get\|set\|unset\|validate`, `secrets status`, `profiles list`, `stats`, `espanso status`, `history export`, any `--dry-run` preview, any `--help` and `--version` | Runs it and shows `$ promptmend …`, its output and `exit N` below the line and in the session log. One runs at a time, with no input and at most 120 seconds. `doctor` skips the clipboard check unless you pass `--clipboard` |
| Asks first | `espanso deploy`, `espanso detach`, `config migrate`, `secrets set NAME`, `secrets remove`, `history prune`, `history reset` | Opens the dialog of the tab that does it (`history prune` with `--older-than` filled in; a key is typed hidden in the dialog). `--yes`, `--on-conflict`, `--no-restart` and `--preview-token` are ignored there: the dialog decides |
| Asks as it goes | `setup`, `shell`, `config rollback`, `config retire`, `profiles migrate`, and a dialog's command given `--espanso-dir`, `--launcher` or `--from` | Says to quit and run it in a terminal |
| Refused | `improve`, `persona`, `ui` | Says why: the triggers run `improve` and `persona`, and `improve` reads the clipboard; the Try tab rewrites a typed draft instead |

A line that looks like it holds a key, `secrets set` with anything after the key's name, or
`config set|get|unset` with a key's name and a value, is never run, shown back or kept: it is cleared, and keys go in Settings. A key in a command's
output is shown as `<redacted, N chars>`.

## Settings tab

The Settings tab (`2`) lists every setting and both API keys, grouped (Output, Keys, Privacy,
Models, History, Interface). Each row has a dot, the name, the value and where the value comes
from (`config.toml`, `secrets.toml`, `.env`, `environment` or `default`):

- a green `●` is a switch that is on, a key that is set, or a value that is not the default;
- a grey `○` and a dimmed row is a switch that is off, a key that is not set, or a default.

The dot is a glyph as well as a colour, so it reads in the high-contrast theme and with
`NO_COLOR` too. Keys show only `set` or `not set`, and `PROMPT_PERSONA` only `<set, hidden>`.
The line under the list explains the setting under the cursor, says where its value comes
from and gives the command that changes it in a terminal.

The list has the focus when the tab opens. Its keys:

| Key | Action |
|-----|--------|
| Up, Down, `j`, `k` | Move |
| Space | Switch a true/false setting, saved at once |
| Enter | Switch a true/false setting; pick from a list (the current value marked); type a value (prefilled, the persona too), checked as the CLI reads it: a rejected value or one that looks like a key stays in the dialog with the reason, and nothing is saved. On a key's row, the Set key dialog |
| `u` | Back to the default (`config unset`). On a key's row, the Remove key dialog. Only while the list has the focus |
| `/` | Filter by name or group; Escape clears it and goes back to the list |

Every change is saved in `config.toml` as `promptmend config set` saves it (keys in the secret
store), and shows and logs its command (`$ promptmend config set PROMPT_OUTPUT clipboard`,
the persona as `<value withheld>`). An environment variable of the same name still wins, and
the result says so.

## Recipes and Copy last

- **Recipes…** lists the useful commands Home suggests. Picking one puts it on the command
  line; nothing runs until you press Enter there, and Escape or Cancel leaves the line as it
  was.
- **Copy last** sends the session's latest command, exactly as the session log shows it
  (withheld values such as `<value withheld>` and placeholders included), to the terminal's
  clipboard (OSC 52). Some terminals, tmux or SSH sessions need it allowed, or drop it. The
  interface never reads the clipboard.

## Try tab

The Try tab (`7`) rewrites a draft you type into it, the way `-i-` would, so you can see the
result before a trigger pastes one.

![The Try tab](try.svg)

1. Type the draft.
2. Pick where it runs (Local stub or Real provider), the provider, the profile and the tier.
   The profile starts at "as configured", which uses `PROMPT_PROFILE` (or `PROMPT_PRO_PROFILE`,
   if set, on the pro tier) exactly as a trigger does.
3. Press Run. One run at a time.

- **Local stub** (the default) answers on `127.0.0.1` with a placeholder key: no provider is
  called and nothing is recorded.
- **Real provider** first asks, naming the provider, model and base URL. A confirmed call may
  be charged and is recorded in the [usage history](privacy.md#usage-history) as a direct
  call with the trigger `-try-`, so `stats` tells it from `promptmend improve` typed in a
  terminal. Home's session log shows it
  with the draft withheld.

The clipboard is never read or written, and the draft never goes into a command line. The
settings are read as a trigger reads them, so an invalid one stops the run with its marker.

Every run goes through the [data-protection gate](privacy.md#the-data-protection-gate): a
draft the gate blocks shows the same `[promptmend: …]` marker a trigger would paste, and
nothing is sent. A stub run is gated (and refused under `PROMPT_LOCAL_ONLY`) exactly as the
real call would be.

Under the result, a line gives the time, the requests, the tokens in and out and the cost: as
reported, `unknown` when the provider did not say, or `not applicable` for the stub and local
models, never a made-up 0.

## Keys

| Key | Action |
|-----|--------|
| `1`-`7` | Switch tabs |
| `c` | Open Home's command line |
| `a` | About: version, whether a newer release exists and the command to update, install channel, folders and licence |
| `r` | Reload |
| `u` | In the Settings list: back to the default (`config unset`) |
| `t` | Switch to the high-contrast theme |
| `q` | Quit |
| Escape | Leave the command line or the Try tab's draft, so the tab keys work again; clear the Settings filter |
| Up, Down, `j`, `k`, Space, Enter, `/` | Move and change in the [Settings list](#settings-tab) |

## Intro

The interface opens with a short intro that stays until you press Enter (or any other key, or
click); that key does nothing else. Its last line says how to turn it off for good:
`promptmend config set PROMPT_UI_INTRO false`. `promptmend ui --no-intro` skips it once.

## Accessibility

- `t` switches to a high-contrast theme.
- `NO_COLOR` turns colour off.
- Scripts and screen readers can use the headless [commands](commands.md) instead: each tab's
  action names its command.

## See also

- [Commands](commands.md): the headless commands behind every button
- [Install](install.md#set-up): `setup`, the question-and-answer alternative
- [Troubleshooting](troubleshooting.md): what the Diagnostics checks mean
