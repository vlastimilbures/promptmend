# Install, update and uninstall

This page covers getting PromptMend running on macOS, Windows or Linux, keeping it up to date,
switching between install channels and removing it again. It also holds the notes for anyone
upgrading from an older release. The README's
[Install](../README.md#install) section has the short version.

## Contents

- [Requirements](#requirements)
- [Choose a channel](#choose-a-channel)
- [macOS](#macos)
- [Windows](#windows)
- [Linux](#linux)
- [Verify a download](#verify-a-download)
- [Set up](#set-up)
- [Update](#update)
- [Switch channel](#switch-channel)
- [Uninstall](#uninstall)
- [Upgrading from older versions](#upgrading-from-older-versions)
- [Install from a checkout](#install-from-a-checkout)

## Requirements

- [Espanso](https://espanso.org/install/), installed and running.
- macOS or Windows. Linux works too: the CLI is tested there, but the triggers are not used
  day to day, and the clipboard needs a helper tool (see [Linux](#linux)).
- For the default `-i-` trigger, an [OpenRouter API key](https://openrouter.ai/keys). For
  fully local use, [Ollama](https://ollama.com) or [LM Studio](https://lmstudio.ai) instead.
- Python 3.12 or later. You do not install it yourself: Homebrew brings its own, and uv
  downloads one when needed.

## Choose a channel

Every channel installs the same release files: the sdist or wheel that the release workflow
built and tested, with the exact dependency versions from `uv.lock`. Each installs the CLI
only; [Set up](#set-up) then writes the Espanso match files.

| Channel | macOS | Windows | Linux | Updates with |
|---------|-------|---------|-------|--------------|
| Homebrew tap | Recommended | — | Yes | `brew upgrade promptmend` |
| One-command install (`install.ps1`) | — | Recommended | — | The same command again |
| uv from PyPI | Yes | Yes | Recommended | `uv tool install --force …` |
| uv from a GitHub Release wheel | Yes | Yes | Yes | `uv tool install --force …` |

PyPI and the Homebrew tap carry releases from 0.19.0 on, the GitHub Release wheels from 0.16
on, and `install.ps1` from the release after 0.19.0 on. There is no Scoop or WinGet package
yet: a WinGet package is being prepared, see [Portable zip](#portable-zip-preview).
To run the code from a git checkout instead, see
[Install from a checkout](#install-from-a-checkout).

## macOS

### Homebrew (recommended)

```bash
brew tap vlastimilbures/tap
brew trust --formula vlastimilbures/tap/promptmend
brew install vlastimilbures/tap/promptmend
```

The formula lives in the project's own tap,
[vlastimilbures/homebrew-tap](https://github.com/vlastimilbures/homebrew-tap), not in
homebrew/core. It installs the sdist attached to the GitHub Release and every dependency from
PyPI, each pinned by SHA-256 to what the release's `uv.lock` records, into a virtual
environment on Homebrew's Python.

A tap is a third-party repository: Homebrew runs its formulas with your user's rights and
updates them on every `brew update`. Only add a tap you trust; `brew untap vlastimilbures/tap`
removes it.

Recent Homebrew also refuses to load a formula from a third-party tap until you trust it
(`Refusing to load formula … from untrusted tap`). `brew trust --formula` trusts this one
formula only; `brew trust vlastimilbures/tap` would trust everything the tap adds later too.
On a Homebrew without tap trust, skip that line. See
[Tap trust](https://docs.brew.sh/Tap-Trust).

### uv

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) (`brew install uv`
works), then:

```bash
uv tool install promptmend -c https://github.com/vlastimilbures/promptmend/releases/latest/download/constraints.txt
```

The package is [`promptmend` on PyPI](https://pypi.org/project/promptmend/), uploaded by the
release workflow through PyPI's trusted publishing (no stored token). The `-c` file is the
release's `constraints.txt`: the exact dependency versions from `uv.lock`. Without it, uv
would resolve the version ranges afresh and could pick a dependency version no release was
tested with.

To install one particular release, pin both the package and its constraints:

```bash
uv tool install promptmend==<version> \
  -c https://github.com/vlastimilbures/promptmend/releases/download/v<version>/constraints.txt
```

If a new terminal does not find `promptmend`, run `uv tool update-shell` once.

## Windows

### Espanso on Windows

Install Espanso before PromptMend, either from [espanso.org](https://espanso.org/install/) or
with `winget install -e --id Espanso.Espanso`. Either way, it is not ready yet:

1. Open Espanso once from the Start menu and accept its first-run wizard, which adds Espanso to
   PATH and starts it with Windows. Until then, `espanso` is not on PATH and Espanso is not
   running.
2. Close every Windows Terminal window, or sign out and in again. A new tab or window keeps
   the environment Windows Terminal started with, so it does not see a PATH that an install
   (Espanso, uv, or uv's `update-shell`) changed since. Do the same after installing
   PromptMend, before the first `promptmend` command.

A portable Espanso (the zip, unpacked to a folder of your choice) keeps its configuration in
the `.espanso` folder next to `espansod.exe`; an installed one uses `%APPDATA%\espanso`.
PromptMend deploys to the portable folder while that Espanso is the one running, so start it
before `promptmend setup` or `promptmend espanso deploy`. `promptmend espanso status` names
the folder PromptMend uses; `espanso path config`, run from the portable folder, shows
Espanso's own answer.

Keep only one Espanso. A portable copy usually starts from its own `espanso.lnk` in your
Startup folder (`shell:startup`), and the installed one's wizard adds autostart of its own, so
two Espanso instances, each with its own configuration folder, can start at sign-in. Delete
one of the two shortcuts, or remove the portable folder once the installed Espanso works.

### One command (recommended)

Paste this into PowerShell or a Command Prompt. It needs no Python or uv first:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://github.com/vlastimilbures/promptmend/releases/latest/download/install.ps1 | iex"
```

`install.ps1` is attached to each GitHub Release from the release after 0.19.0 on (0.19.0
does not have it), with that release's version filled in, and attested like the other release
files. You can
[read it](https://github.com/vlastimilbures/promptmend/blob/main/scripts/install.ps1) first.
It prints each command before running it, and:

1. installs uv if it is missing (`winget install --id astral-sh.uv -e`, or uv's official
   installer where there is no winget);
2. installs the wheel attached to that same Release with its `constraints.txt`, with
   `--force` (PyPI is not used);
3. runs `uv tool update-shell`, so a new terminal finds `promptmend` (in Windows Terminal,
   only once every window was closed);
4. runs `promptmend doctor --no-clipboard`, which lists what [Set up](#set-up) still has to do.

It never runs `promptmend setup` or `promptmend espanso deploy`: it only names them as the
next step. It says so when it replaces an editable install from a checkout.

To install another release, set the version in PowerShell first and run the `irm … | iex`
part in that same window. 0.19.0 is the oldest release it accepts.

```powershell
$env:PROMPTMEND_VERSION = "<version>"
```

### uv

```powershell
winget install --id astral-sh.uv -e
uv tool install promptmend -c https://github.com/vlastimilbures/promptmend/releases/latest/download/constraints.txt
uv tool update-shell
```

Then close every Windows Terminal window and open a new one, so it finds `promptmend` (see
[Espanso on Windows](#espanso-on-windows)).

### Portable zip (preview)

From the release after 0.21.0 on, each GitHub Release also carries
`promptmend-<version>-windows-x64.zip`, attested like the other release files: the CLI built
with PyInstaller as a folder that brings its own Python, so it needs neither Python nor uv.
It is what the coming WinGet package (`vlastimilbures.PromptMend`) will install; until that
package is published, the zip is for trying out. It is not signed, so SmartScreen or
Defender may warn the first time it runs.

1. Unzip it to `%LOCALAPPDATA%\Programs`, so the exe is
   `%LOCALAPPDATA%\Programs\promptmend\promptmend.exe`; it needs the `_internal` folder next
   to it. Deploy refuses an exe still in a temporary folder (opened straight from the zip) or
   in a folder named for its version (`promptmend-<version>-windows-x64`), since the next
   release would move it.
2. Run its `setup` from that folder (`.\promptmend.exe setup` in PowerShell), so the match
   files call that exe.

To update, delete the old `%LOCALAPPDATA%\Programs\promptmend` folder, then unzip the new
release to the same place: the path stays the same, so the match files keep working. Once the
WinGet package is published,
`winget upgrade vlastimilbures.PromptMend` updates it instead, and `doctor` names that command.

## Linux

### uv (recommended)

```bash
uv tool install promptmend -c https://github.com/vlastimilbures/promptmend/releases/latest/download/constraints.txt
```

The clipboard needs `xclip` or `xsel` (X11) or `wl-clipboard` (Wayland). Without one, a
trigger pastes a `[promptmend: Clipboard unavailable: …]` marker instead of a rewrite.

### Homebrew

`brew install vlastimilbures/tap/promptmend` works on Linux too, as on [macOS](#macos).

## Verify a download

Each [GitHub Release](https://github.com/vlastimilbures/promptmend/releases) carries the
sdist, the wheel and `constraints.txt` (the wheel and constraints from 0.16 on),
`install.ps1` from the release after 0.19.0 and the
[portable Windows zip](#portable-zip-preview) from the release after 0.21.0, each with a build
provenance attestation. To check the files before installing them:

1. Download them: `gh release download v<version> -R vlastimilbures/promptmend`.
2. Verify each one: `gh attestation verify <file> -R vlastimilbures/promptmend`.
3. Install the local wheel with its constraints:

   ```bash
   uv tool install ./promptmend-<version>-py3-none-any.whl -c constraints.txt
   ```

The same wheel installs straight from the Release too:

```bash
uv tool install \
  https://github.com/vlastimilbures/promptmend/releases/download/v<version>/promptmend-<version>-py3-none-any.whl \
  -c https://github.com/vlastimilbures/promptmend/releases/download/v<version>/constraints.txt
```

## Set up

After any install, run one of these in a terminal:

- `promptmend setup` walks you through it one step at a time on a full screen
  ([Setup](interface.md#setup)): pick the provider and default profile from a list (saved in
  `config.toml`), paste each API key the triggers need (hidden, saved in `secrets.toml`),
  install the match files after seeing what changes, and run a smoke test of `improve`
  against a stub on `127.0.0.1` (never a paid call, never your real key). It ends with what
  is left to do and how to try `-i-`. `promptmend setup --non-interactive` prints the same steps and asks nothing.
- `promptmend` opens the [full-screen interface](interface.md), which does the same through
  its tabs.

`setup` also names a `.env` left in the config folder, which is no longer read (see
[From 0.25 or earlier](#from-025-or-earlier-settings-in-a-env)). In a script, pass the key on
stdin and nothing is asked:

```bash
promptmend setup --non-interactive --api-key-stdin --deploy < key.txt
```

Without `--deploy`, the deploy stays a preview. Then copy a rough draft, type `-i-` in any
text field and wait without typing or switching windows (see [Usage](usage.md), and the
[list of triggers](usage.md#triggers) for what each one does and sends).

## Update

Update through the channel you installed with, then check the result with
`promptmend doctor`. `doctor` and the [interface](interface.md#home)'s About screen give the
command below for your channel when a newer release exists.

| Channel | Update command |
|---------|----------------|
| Homebrew | `brew update && brew upgrade promptmend` |
| One-command install | The same `irm … \| iex` command again, with `PROMPTMEND_VERSION` unset |
| uv from PyPI | `uv tool install --force promptmend -c <latest constraints URL>` (below) |
| uv from a Release wheel | The install command with the new version and `--force` |
| WinGet (once published) | `winget upgrade vlastimilbures.PromptMend` |
| Portable zip | Unzip the new release over the same folder |
| A checkout ([below](#install-from-a-checkout)) | `git pull`, then rerun `./scripts/install_macos.sh` (`.\scripts\install_windows.ps1` on Windows) |

With uv, install the new release over the old one with its own `constraints.txt`; `--force`
makes uv replace the tool already there (`install.ps1` and the checkout install scripts pass it
too):

```bash
uv tool install --force promptmend -c https://github.com/vlastimilbures/promptmend/releases/latest/download/constraints.txt
promptmend doctor
```

The one-command install puts the newest release over the old one with the same launcher, so
the match files keep working. `uv tool upgrade promptmend` does not move such an install,
since uv pins the wheel's URL: run the command again instead.

The tap's formula is updated by hand after each release, so Homebrew can trail the GitHub
Release and PyPI by a while: `doctor` may name a release that `brew upgrade` does not offer
yet.

Upgrading the CLI does not touch the match files Espanso holds. If `doctor` reports one as
`stale` (for example `prompts-template.yml: stale`), run `promptmend espanso deploy` to bring
it up to date.

On Windows, a deploy is needed after upgrading from 0.20.0 or earlier: those match files ran
the CLI through PowerShell, which fails on every trigger (#18).

## Switch channel

To move from one channel to another (say from uv to Homebrew):

1. Install through the new channel.
2. Run `promptmend espanso deploy` from the new install, so the match files call its launcher.
3. Uninstall the old one (step 4 of [Uninstall](#uninstall)).

Run from the new install, `doctor` warns while the matches still call the old launcher, or
while the old `promptmend` comes first on `PATH` (`path_shadow`).

## Uninstall

Detach before you uninstall: once the CLI is gone, every trigger that calls it fails with
Espanso's rendering error.

1. `promptmend espanso detach` removes the matches that call the CLI (`prompts-llm.yml`,
   `prompts-template.yml`) and keeps `-risk-` as a static snippet (`--keep-static`, the
   default); `--remove-all` removes every file it deployed. The `.bak-…` backups are never
   deleted.
2. Check with `promptmend espanso status` that neither `prompts-llm.yml` nor
   `prompts-template.yml` is left (both should be `missing`). Detach removes only files on
   record and unedited, so:
   - if it said `Nothing to do` (the files came from an install script before 0.16, so there
     is no record), run `promptmend espanso deploy` first: it adopts unedited files any
     release wrote, after which `detach` removes them;
   - a file you edited (`modified` or `foreign`) is kept: delete it from Espanso's `match/`
     folder by hand, or remove its CLI-calling matches.

   Do not uninstall while one of these files is left.
3. Optionally, and only if you want them gone: `promptmend history reset` deletes the usage
   history, and `promptmend secrets remove OPENROUTER_API_KEY` (or `ANTHROPIC_API_KEY`)
   deletes a saved key. The config folder (`config.toml`, `profiles/`) and the
   data folder stay until you delete them (see
   [Files and folders](configuration.md#files-and-folders)).
4. Uninstall through the channel you installed with:
   - uv, including the Windows one-command install: `uv tool uninstall promptmend`. uv stays;
     `winget uninstall --id astral-sh.uv -e` removes it if winget installed it.
   - Homebrew: `brew uninstall promptmend`, and `brew untap vlastimilbures/tap` if nothing
     else from the tap is installed.

If the CLI is already broken or gone, install it again, then detach. Or clean up by hand:
`espanso-manifest.json` in the data folder lists each deployed file as `target` with its
`backups`. Delete the targets in Espanso's `match/` folder, restore a backup if you want your
earlier version back, and restart Espanso.

## Upgrading from older versions

### From 0.25 or earlier: settings in a .env

Releases up to 0.25.0 also read a `.env` in the config folder (and an editable install the
checkout's `.env`), and had `config migrate`, `config rollback`, `profiles migrate` and the
`prompt-workflow` alias. All of that is gone (#275): only the `.env` that `PROMPTMEND_ENV`
names is read.

Before you upgrade, while 0.25.0 is still installed:

1. Its `config migrate` moves the `.env` in use into `config.toml` and the secret store,
   after a preview.
2. From a checkout install (one made with the install scripts), `profiles migrate --checkout
   PATH` copies the profiles you added or edited there into your profile folder.

Already upgraded? Your settings are not lost: the `.env` is still there, and `doctor` and
`config validate` report it. Set `PROMPTMEND_ENV` to it to keep using it, or save its settings
again with `promptmend setup` (see [Settings in a .env](configuration.md#settings-in-a-env)).
Copy a profile you edited in a checkout into the profile folder by hand
([Profiles](profiles.md)).

Leaving a checkout install for a release: run `promptmend espanso deploy` from the release, so
the triggers call it, and if `promptmend` on `PATH` is still the checkout's (`doctor` warns in
`path_shadow`), run `deactivate` (its `.venv` is active) or take it off `PATH`.

### From 0.18 or earlier: the rename

Up to 0.18.0 the package was `espanso-prompt-rewriter` and the command `prompt-workflow`; both
are now `promptmend`. Switch over once:

```bash
uv tool uninstall espanso-prompt-rewriter   # the triggers stop working until the deploy
uv tool install promptmend -c https://github.com/vlastimilbures/promptmend/releases/latest/download/constraints.txt
promptmend espanso deploy                   # the matches now call promptmend
promptmend doctor
```

The deploy counts the match files an earlier release wrote (stamped `# prompt-workflow …`) as
its own and `stale`, so it replaces them without asking. Other changes a script may notice:

- the error and note marker is `[promptmend: …]` (was `[prompt-workflow: …]`);
- the match labels in Espanso's search bar start with `PromptMend:`;
- the Python package is `promptmend`;
- OpenRouter sees the `X-Title: promptmend` header.

Those releases kept your settings in `~/.config/prompt-workflow/` and the usage history and
deploy manifest in `~/.local/share/prompt-workflow/` (`%APPDATA%\prompt-workflow\` and
`%LOCALAPPDATA%\prompt-workflow\` on Windows). Releases 0.19 to 0.25 renamed them to
`promptmend` on the first management command; this one no longer looks for them, so rename
them by hand if they are still there.

Coming from a version before 0.9: if Espanso's `match/base.yml` still holds the `-p-`
template those versions installed there, delete it by hand (deploy no longer does), or `-p-`
is defined twice.

## Install from a checkout

To run your own checkout from Espanso (for development), see
[CONTRIBUTING.md, "Set up"](../CONTRIBUTING.md#set-up). It installs the checkout as an
editable tool, so a `git pull` changes the triggers at once.

## See also

- [Usage](usage.md): the triggers and what they paste
- [Configuration](configuration.md): settings, keys and folders
- [Troubleshooting](troubleshooting.md): when `doctor` reports a problem
