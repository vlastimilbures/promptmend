# Configuration

This page covers where PromptMend keeps its settings, keys and data, how to change them, and
what every setting does. You need it to change a model or provider, set your persona, switch
to clipboard output or local-only mode, or find a file. The settings reference at the end is
the one complete list.

## Contents

- [Where settings live](#where-settings-live)
- [Files and folders](#files-and-folders)
- [Change a setting](#change-a-setting)
- [API keys](#api-keys)
- [Settings in a .env](#settings-in-a-env)
- [Value syntax](#value-syntax)
- [Proxies](#proxies)
- [Settings reference](#settings-reference)
- [Ollama context window](#ollama-context-window)

## Where settings live

Every setting is named like an environment variable (`OPENROUTER_MODEL`, `PROMPT_PERSONA`).
Espanso starts the CLI without your shell's environment, so the CLI reads its settings from
files:

- `config.toml` in the [config folder](#files-and-folders), and the API keys in
  `secrets.toml` next to it.
- Or, when `PROMPTMEND_ENV` names a `.env`, that file alone: no `config.toml` or
  `secrets.toml` (legacy mode, see [Settings in a .env](#settings-in-a-env)).

No other `.env` is read: not one in the config folder, and not a checkout's.

Order of precedence, lowest first: built-in default < `config.toml` (or the `PROMPTMEND_ENV`
file) < `secrets.toml` < real environment variable < a trigger's own options.

The CLI never reads a settings file from the current directory, so running it inside some
other project cannot change its endpoint or switch off the gate. Only the settings in the
[reference](#settings-reference) are read; anything else in a file (such as `HTTPS_PROXY` or
`SSL_CERT_FILE`) is ignored.

## Files and folders

| What | macOS and Linux | Windows |
|------|-----------------|---------|
| Config folder | `~/.config/promptmend/` (`$XDG_CONFIG_HOME/promptmend/` when set) | `%APPDATA%\promptmend\` |
| Data folder | `~/.local/share/promptmend/` (`$XDG_DATA_HOME/promptmend/` when set) | `%LOCALAPPDATA%\promptmend\` |
| `config.toml` | config folder | config folder |
| `secrets.toml` | config folder, mode 600 | config folder, readable by your account alone |
| `profiles/<name>.md` | config folder | config folder |
| `prices.toml` (optional) | config folder | config folder |
| `history.sqlite3`, `history.lost` | data folder | data folder |
| `espanso-manifest.json` | data folder | data folder |
| `update-check.json` | data folder (the [update check](privacy.md#update-check)'s last answer) | data folder |

On Windows `XDG_CONFIG_HOME` and `XDG_DATA_HOME` are ignored. `config.toml` holds plain TOML
with the same names (`OPENROUTER_MODEL = "…"`, `OLLAMA_THINK = true`) and a `config_version`;
it never holds a key. A `config.toml` kept as a symlink (into a dotfiles repo, say) stays
one: saving writes the file it points to, and so does a symlinked `secrets.toml`. Keep
`secrets.toml` out of any repo you push.

## Change a setting

Use the commands, or the [Settings tab](interface.md#settings-tab) of the interface, a list
to switch, pick, edit and reset each setting:

```bash
promptmend config show                       # every setting, its value and its source
promptmend config get OPENROUTER_MODEL       # one value
promptmend config set OPENROUTER_MODEL google/gemini-3.5-flash-lite
promptmend config unset OPENROUTER_MODEL     # back to the default
promptmend config validate                   # check everything as a trigger reads it
```

`config set` checks the value as the CLI reads it before saving it in `config.toml`, and
refuses a key name or a value that looks like a key. A change applies to the next trigger,
with no redeploy.

In legacy mode, with `PROMPTMEND_ENV` set, that `.env` stays the only settings file:
`config set`, `config unset` and `secrets set|remove` refuse to write. Edit the file itself.

## API keys

Keys (`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`) live in `secrets.toml`, never in
`config.toml`. They are never an argument on the command line:

```bash
promptmend secrets set OPENROUTER_API_KEY            # hidden prompt
promptmend secrets set OPENROUTER_API_KEY --stdin < key.txt
promptmend secrets status                            # set or not set, and where from
promptmend secrets remove OPENROUTER_API_KEY
```

`secrets.toml` is private to your user: mode 600 on macOS and Linux, an access list for your
account alone on Windows. A keychain is not supported yet. Keys are never logged, never shown
in a traceback, and sent only as the authentication header of their own provider.

## Settings in a .env

A `.env` is read only when `PROMPTMEND_ENV` names it (for GUI apps, set it in Espanso's
environment, since Espanso does not inherit your shell). Earlier releases also read a `.env`
in the config folder, and an editable install the checkout's; those are no longer read
(#275). If you still have one:

- Save its settings again with `promptmend setup`, or `promptmend config set` and
  `promptmend secrets set` for each one, then remove the file. Until you do, `config validate`
  and `doctor` report a `.env` left in the config folder, or in the checkout while an
  editable install runs from it.
- Or keep using it as it is: set `PROMPTMEND_ENV` to its path.

The repository's `.env.example` sets only the key and the persona and shows every other
setting commented out with its default, so later default changes still reach you.

## Value syntax

- Values may be quoted, and an unquoted value may be followed by a ` # comment`.
- Quote a value that itself contains ` #`. For `PROMPT_EXTRA_PATTERNS` and `PROMPT_PERSONA`,
  where `#` may be part of the text, `config validate` and `doctor` report a value cut at
  ` #`. A cut `PROMPT_EXTRA_PATTERNS` also
  stops every rewrite trigger with a marker (`-p-` still pastes the persona), so the gate never
  runs on part of your patterns.
- A `.env` must be UTF-8 (a byte order mark is fine); any other encoding is an error.
- Booleans are `true` or `false`. Timeouts and token caps are numbers above 0. Temperature may
  be 0, or empty to send none.
- Anything else is reported inline rather than silently ignored. An error repeats the bad
  value only when it is short, does not look like a key and matches none of your
  `PROMPT_EXTRA_PATTERNS`.

## Proxies

Cloud calls use the system proxy (macOS System Settings, Windows Internet Options) or the
`HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY` environment variables. Since Espanso starts the CLI
without your shell's environment, set such variables for GUI apps (`launchctl setenv` on macOS,
user environment variables on Windows) rather than in a shell profile.

A base URL on this machine (`localhost`, `127.0.0.0/8`, `::1`) is always reached directly,
never through a proxy. A proxy that inspects TLS sees the whole request, the key header
included (see [Privacy](privacy.md#what-is-sent-and-to-whom)).

## Settings reference

### General

| Setting | Default | Purpose |
|---------|---------|---------|
| `PROMPT_PROVIDER` | `openrouter` | Provider when `--provider` is not given: a bare `promptmend improve` and the interface's Try tab. The triggers name their own provider, so it does not change them |
| `PROMPT_PROFILE` | `default` | Profile when `--profile` is not given (`-i-`, and `-if-` on a non-pro model) |
| `PROMPT_PERSONA` | *(empty)* | Your first-person role, see [Persona](profiles.md#persona) |
| `PROMPT_PROFILE_OVERRIDES` | *(empty)* | Comma-separated built-in profiles (`default`, `general`) your own same-named file replaces, see [Profiles](profiles.md#replacing-a-built-in) |
| `PROMPT_OUTPUT` | `paste` | `clipboard` puts the rewrite on the clipboard instead of pasting it, see [clipboard output](usage.md#output-paste-or-clipboard) |
| `PROMPT_TIMEOUT_SECONDS` | `30` | Time limit for one call, retry included |
| `PROMPT_TEMPERATURE` | `0.2` | For OpenRouter, Ollama and LM Studio; Anthropic and OpenAI use `ANTHROPIC_TEMPERATURE` and `OPENAI_TEMPERATURE` instead. Kept low so fixed template wording survives; empty (`PROMPT_TEMPERATURE=`, or `config set PROMPT_TEMPERATURE ""`) sends no temperature, for models that reject it, while unset keeps `0.2` |

### OpenRouter

| Setting | Default | Purpose |
|---------|---------|---------|
| `OPENROUTER_API_KEY` | — | Required for OpenRouter |
| `OPENROUTER_MODEL` | `google/gemini-3.5-flash-lite` | Model for `-i-`, `-iok-` and the bare CLI's standard tier (see the [benchmark](benchmark.md)) |
| `OPENROUTER_PROVIDER` | `google-ai-studio/flex` | Pin a serving endpoint; empty = OpenRouter's own routing |
| `OPENROUTER_REASONING_EFFORT` | `minimal` | `none`…`high`; empty omits it (models without the control) |
| `OPENROUTER_ALLOW_FALLBACKS` | `true` | `false` makes the pin binding |
| `OPENROUTER_DATA_COLLECTION` | *(empty)* | `deny` routes every OpenRouter call (both tiers) only to endpoints that do not store or train on requests; `allow` permits them; empty sends nothing, so your OpenRouter account setting applies. See [Privacy](privacy.md#data-collection-preference) |
| `OPENROUTER_MAX_TOKENS` | `2400` | Output cap (cost control), for both tiers unless `OPENROUTER_PRO_MAX_TOKENS` is set |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | API endpoint; must be `https://` |

### OpenRouter pro tier

Used by `-ip-`, `-if-` and `--tier pro`.

| Setting | Default | Purpose |
|---------|---------|---------|
| `OPENROUTER_PRO_MODEL` | `openai/gpt-6-luna` | Model for the pro tier |
| `OPENROUTER_PRO_PROVIDER` | `openai` | Endpoint pin for the pro tier |
| `OPENROUTER_PRO_REASONING_EFFORT` | `low` | Reasoning effort for the pro tier |
| `OPENROUTER_PRO_MAX_TOKENS` | *(empty)* | Output cap for the pro tier, whose reasoning counts against it; empty = `OPENROUTER_MAX_TOKENS` |
| `PROMPT_PRO_TIMEOUT_SECONDS` | `60` | Time limit for one pro-tier call, retry included |
| `PROMPT_PRO_PROFILE` | *(empty)* | Profile for the pro tier when it runs `OPENROUTER_PRO_MODEL`; empty = `PROMPT_PROFILE` |

### Anthropic

| Setting | Default | Purpose |
|---------|---------|---------|
| `ANTHROPIC_API_KEY` | — | Required for Anthropic |
| `ANTHROPIC_MODEL` | `claude-sonnet-5` | Model |
| `ANTHROPIC_MAX_TOKENS` | `2400` | Output cap |
| `ANTHROPIC_TEMPERATURE` | *(empty)* | Sampling temperature for Anthropic; empty sends none, since current Claude models (Opus 4.7, Sonnet 5 and later) return HTTP 400 for a temperature (most for any value, the rest for any but the default). A set value is sent as is, so set it only for a model that accepts one |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | API endpoint; must be `https://` |

### OpenAI

OpenAI's own API, called directly (the commented-out `-io-` trigger, `--provider openai`).

| Setting | Default | Purpose |
|---------|---------|---------|
| `OPENAI_API_KEY` | — | Required for OpenAI |
| `OPENAI_MODEL` | `gpt-6-luna` | Model; the pro tier's model, benchmarked at effort `low` (see the [benchmark](benchmark.md)) |
| `OPENAI_REASONING_EFFORT` | `low` | `none`…`high`, sent as `reasoning_effort` (each model accepts only some of them; OpenAI's `xhigh` and `max` are not offered, being too slow for a trigger); `--effort` sets it for one call. Empty omits it, for a model without a reasoning control, which rejects the field |
| `OPENAI_MAX_TOKENS` | `2400` | Output cap, sent as `max_completion_tokens` (OpenAI's reasoning models reject `max_tokens`); the reasoning counts against it |
| `OPENAI_TEMPERATURE` | *(empty)* | Sampling temperature for OpenAI; empty sends none, since OpenAI's reasoning models return HTTP 400 for any temperature but the default. A set value is sent as is, so set it only for a model that accepts one |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | API endpoint; must be `https://` |

### Ollama

| Setting | Default | Purpose |
|---------|---------|---------|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Server address |
| `OLLAMA_MODEL` | `qwen3:8b` | Model; a `cloud`-tagged model counts as a cloud call |
| `OLLAMA_THINK` | `false` | Keep reasoning off for thinking models |
| `OLLAMA_NUM_CTX` | *(empty)* | Context window in tokens, sent as `options.num_ctx`; empty sends nothing, so Ollama's own applies. See [below](#ollama-context-window) |

### LM Studio

| Setting | Default | Purpose |
|---------|---------|---------|
| `LMSTUDIO_BASE_URL` | `http://localhost:1234/v1` | Server address |
| `LMSTUDIO_MODEL` | `local-model` | Model |

### Privacy and gate

See [Privacy](privacy.md) for what each one changes.

| Setting | Default | Purpose |
|---------|---------|---------|
| `PROMPT_LOCAL_ONLY` | `false` | `true` refuses every provider that can send the draft off this machine |
| `PROMPT_GATE_LOCAL` | `false` | `true` runs the gate for Ollama / LM Studio on `localhost` too (a local relay to a cloud API, or an `ollama cp` alias of a cloud model) |
| `PROMPT_EXTRA_PATTERNS` | *(empty)* | Your own `;`-separated regexes for the gate |
| `ALLOW_CLOUD_OVERRIDE` | `false` | `true` lets flagged drafts reach cloud providers |

### Usage history

| Setting | Default | Purpose |
|---------|---------|---------|
| `PROMPT_HISTORY` | `true` | Keep a local [usage history](privacy.md#usage-history) (metadata only); `false` keeps none |
| `PROMPT_HISTORY_RETENTION_DAYS` | `365` | Days a record is kept before pruning (1 to 36500) |

### Interface

| Setting | Default | Purpose |
|---------|---------|---------|
| `PROMPT_UPDATE_CHECK` | `true` | `doctor` and the interface's Home ask pypi.org, once a day, whether a newer release exists ([privacy](privacy.md#update-check)); `false` makes no request |

### Files

Read from the real environment only, never from a file.

| Setting | Default | Purpose |
|---------|---------|---------|
| `PROMPTMEND_ENV` | *(unset)* | Path of the `.env` to load, alone: no `config.toml` or `secrets.toml`. Set it for GUI apps, since Espanso does not inherit your shell |

## Ollama context window

Ollama may shorten a prompt longer than its context window without reporting an error. The
`default` profile is much longer than `general`, and the window must hold the profile, the
draft and the reply. Ollama's default window depends on its version, the model and the
available memory.

If rewrites through `--provider ollama` ignore the template, set `OLLAMA_NUM_CTX` to a larger
value the model supports. A larger window uses more memory, and changing it reloads the model.
It applies to models Ollama runs on this machine; a cloud-tagged model uses its own context.
For LM Studio, set the context length when loading the model.

## See also

- [Profiles](profiles.md): your persona and your own profiles
- [Privacy](privacy.md): what the gate settings change
- [Commands](commands.md): every `config` and `secrets` option
