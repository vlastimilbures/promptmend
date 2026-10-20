# Privacy and data protection

This page covers what a trigger sends and to whom, the data-protection gate that checks a
draft before it leaves your machine, how to stay fully local, how the clipboard and the output
are handled, and what the local usage history keeps. Read it before you use a cloud trigger
with work material.

## Contents

- [In short](#in-short)
- [What is sent, and to whom](#what-is-sent-and-to-whom)
- [Data collection preference](#data-collection-preference)
- [The data-protection gate](#the-data-protection-gate)
- [Local only](#local-only)
- [Clipboard safety](#clipboard-safety)
- [Output clean-up](#output-clean-up)
- [Usage history](#usage-history)
- [Update check](#update-check)

## In short

- Cloud triggers (`-i-`, `-ip-`, `-if-`, `-iok-`) send your clipboard to a third-party API.
  Use them only where your organisation's policy allows.
- Before a draft can leave your machine, the gate scans it and blocks keys, tokens, passwords,
  payment cards, IDs, confidentiality labels and your own patterns.
- For sensitive work, use `-il-` or `-ilm-` with a model that runs on your machine, and nothing
  leaves it. `PROMPT_LOCAL_ONLY=true` rules out cloud calls altogether.
- Items a password manager marks as concealed are refused, and invisible characters are
  removed from what is sent and what is pasted.
- The usage history is metadata only and stays on this device. Your settings files and keys
  never leave it, except a key as the authentication header of its own provider.
- `doctor` and the interface ask pypi.org once a day whether a newer release exists
  (`PROMPT_UPDATE_CHECK=false` turns that off).

> [!WARNING]
> The gate is a heuristic safety net, not a compliance control. It misses things (names,
> addresses, phone numbers, IP addresses, most countries' ID formats, a password in a sentence
> that does not call it one, look-alike letters from other alphabets) and sometimes flags
> harmless text.

## What is sent, and to whom

Each call is one HTTP request (two when a rate limit or an unavailable upstream is retried)
carrying:

- **the draft**, after the gate and the clean-up of invisible characters;
- **the system prompt**: the profile's instructions and, with the `default` profile (or your
  own profile using `{{PERSONA_RULE}}`), your `PROMPT_PERSONA` sentence, word for word;
- **the model name and request settings**, such as the temperature and maximum tokens (for
  OpenRouter also the reasoning effort and the endpoint preference; for OpenAI the reasoning
  effort; for Ollama its `think` setting and `num_ctx` when set; the Anthropic API version
  header);
- **the API key**, only as the authentication header (`Authorization: Bearer …` for
  OpenRouter and OpenAI, `x-api-key` for Anthropic; none for Ollama or LM Studio).

The persona goes with every such call and is **not** scanned by the gate: an email, a company
name or one of your `PROMPT_EXTRA_PATTERNS` in the persona is sent even where the same text in
the draft would be blocked. Keep the persona to what you are happy to send to every recipient
below.

`config validate` and `doctor` (and the interface's Home and Diagnostics tabs) run the
gate's scan over the persona when your profiles send it (not with `PROMPT_LOCAL_ONLY=true`
unless `PROMPT_GATE_LOCAL=true`), and name what it matches, never the text.

| Provider | Recipients |
|----------|------------|
| OpenRouter (`-i-`, `-iok-`, `-ip-`, `-if-`) | OpenRouter (`OPENROUTER_BASE_URL`), and the upstream endpoint that serves the model: the one pinned by `OPENROUTER_PROVIDER` (`OPENROUTER_PRO_PROVIDER` for `-ip-`, the form's pick for `-if-`), or another endpoint serving the same model when OpenRouter falls back, which `OPENROUTER_ALLOW_FALLBACKS` allows by default (`false` makes the pin binding). An empty pin leaves the choice to OpenRouter. The request carries an `X-Title: promptmend` header, which attributes the calls to this app in OpenRouter's dashboard (the benchmark script sends `promptmend-bench`) |
| Anthropic (`-ic-`, commented out) | Anthropic (`ANTHROPIC_BASE_URL`) |
| OpenAI (`-io-`, commented out) | OpenAI (`OPENAI_BASE_URL`), called directly, not through OpenRouter |
| Ollama / LM Studio on `localhost` (`-il-`, `-ilm-`) | Nobody else, as long as the server on this machine runs the model itself (a `localhost` relay that forwards to a cloud API is not detected; a `cloud`-tagged Ollama model is the next row). These triggers use the `general` profile, which has no persona |
| Ollama / LM Studio at another address, or an Ollama `cloud` model | The configured server (`OLLAMA_BASE_URL`/`api/chat` or `LMSTUDIO_BASE_URL`); for a cloud model, that Ollama server also forwards the request to ollama.com (this tool sends no key there). With an `http://` URL anyone on the network path can read the draft too. The gate applies as for the cloud providers |

A request to a server that is not on this machine also passes through any proxy in between
(see [Proxies](configuration.md#proxies)); a proxy that inspects TLS sees the whole request,
the key header included.

Cloud base URLs must be `https://` (plain `http` only to `localhost`), so a key is never sent
in clear text. Ollama and LM Studio send no key and accept any scheme: an `http://` base URL on
another machine sends the draft and the rewrite in clear text across your network, so use
`https://` (or an SSH tunnel) for a server you do not reach over `localhost`.

What never leaves this machine: the usage history, your settings files and keys (a key only as
the authentication header of its own provider), and anything else on the clipboard before or
after the trigger.

## Data collection preference

How long each recipient keeps the request, and whether it may train on it, is set by them, not
by this tool. See OpenRouter's privacy and data settings for your account (they cover which
upstream endpoints it may route to) and the privacy terms of the upstream provider, of
Anthropic or of OpenAI.

To make the request itself carry that choice, set `OPENROUTER_DATA_COLLECTION=deny` (empty,
the default, sends no such field). The request then asks OpenRouter, through its
`provider.data_collection` routing field, to skip every endpoint whose data policy allows
storing or training on requests, for both tiers. That can mean fewer endpoints.

The default pin, `google-ai-studio/flex`, served a `deny` call when this was checked
(2026-10-06), but OpenRouter's endpoint policies can change.

If the pinned endpoint (or the `-ip-`/`-if-` one) does not qualify, OpenRouter falls back to
another endpoint that does while `OPENROUTER_ALLOW_FALLBACKS=true`.

Otherwise the trigger pastes an error marker instead of a rewrite, typically
`[promptmend: OpenRouter returned HTTP 404: not found…]` followed by OpenRouter's reason. Pick another endpoint (`OPENROUTER_PROVIDER`, `OPENROUTER_PRO_PROVIDER`)
or clear the setting.

## The data-protection gate

Every call that can send the draft off your machine first runs through a regex gate
([`redaction.py`](../src/promptmend/redaction.py)):

- OpenRouter, Anthropic and OpenAI, always;
- Ollama or LM Studio when their base URL is not loopback (`localhost`, `127.0.0.0/8` or `::1`);
- Ollama when the model is a cloud model: a `:cloud` or `-cloud` tag in any letter case, also
  with an `@sha256:…` digest, which the local daemon forwards to ollama.com;
- with `PROMPT_GATE_LOCAL=true`, Ollama and LM Studio on `localhost` too.

No code path builds a provider that can reach another machine without the gate.

### What it blocks

- Payment card numbers (Luhn-checked, also when split by double spaces, tabs, dashes or one
  line break), IBANs (checksum-validated) and email addresses.
- API keys (OpenRouter, Anthropic, OpenAI, Stripe, GitHub, GitLab, Hugging Face, Slack, Google,
  xAI, npm, AWS including temporary `ASIA…` keys), Azure storage keys and SAS signatures, JWTs,
  bearer and `Basic` credentials, `curl -u user:password`, PEM/OpenSSH/PGP private keys and
  `user:password@` URLs.
- Secrets assigned to a name: `DB_PASSWORD=…`, JSON `"client_secret": "…"`, camelCase
  `clientSecret`/`dbPassword`, `PGPASSWORD`, `SECRET_KEY`, `PRIVATE_KEY`, `_authToken`, PHP
  `=>` and Go `:=` (the value must contain a digit), and passwords in prose (`the password is
  …`, `mật khẩu là …`).
- A draft that is a single password- or token-like word, such as a vault password left on the
  clipboard.
- An email address next to a password (`jane@example.com:…`, `login jane@… / …`).
- Confidentiality labels written as labels: upper case (`CONFIDENTIAL`, `RESTRICTED`, `MẬT`,
  `NỘI BỘ`), alone on a line or opening one (`# Confidential`, `**Confidential**:`,
  `Restricted - …`), in brackets (`[restricted]`), a classification field (`Classification:
  Restricted`, `Độ mật: Mật`), *highly/company/strictly confidential*, *internal only*, *do not
  distribute*, and Vietnamese *tài liệu/văn bản/thông tin mật*, *tối mật*, *lưu hành nội bộ*.
  The words in prose ("output restricted to 5 bullets", "confidential information", *bảo mật*,
  *mật độ*, *mật khẩu*) are not flagged.
- National ID numbers, in Vietnamese formats only today: a 12-digit CCCD with a valid province and century code (not digits
  inside an AWS ARN), and a 9-digit number next to CMND, CCCD, CMT, *chứng minh nhân dân/thư*,
  *căn cước*, *hộ chiếu*, *passport*, *national ID* or *ID card*.
- Your own patterns from `PROMPT_EXTRA_PATTERNS` (below).

The draft is also scanned in a normalised form, so no-break or zero-width spaces, soft
hyphens, variation selectors, Hangul fillers and fullwidth digits cannot split a card number or
key.

### Your own patterns

Add your own `;`-separated, case-insensitive regexes:

```bash
promptmend config set PROMPT_EXTRA_PATTERNS "project[- ]falcon;CUST-\d{6}"
```

A match is reported as `custom_1`, `custom_2`, … so the pattern itself never appears in the
message. An entry that is not a valid regex is rejected when settings load: `config validate`,
`config set` and `doctor` report it by position (`entry 2 (custom_2)`), and every rewrite
trigger prints a marker until it is fixed (`-p-` still pastes the persona).

Keep each pattern simple. It runs on every draft and on each value written to the usage
history, and Python's regex engine can take exponential time on a pattern with nested
quantifiers such as `(\w+\s?)+` or `(a|aa)+`. Prefer a literal word, a character class with a
fixed count (`CUST-\d{6}`) or a bounded repeat (`\w{1,20}`).

### Overriding a block

A blocked draft pastes `[promptmend: Blocked cloud call. Sensitive content detected: …]`
instead of calling the API.

- When every finding is a label, a national ID number, an email address or an IBAN, you can send
  that one draft with `-iok-` (`--allow-flagged`). The paste then starts with
  `[promptmend: sent despite: …]`, and the next draft is checked as usual.
- Keys, tokens, passwords, cards, private keys, a bare token and your own
  `PROMPT_EXTRA_PATTERNS` are never sent this way.
- `ALLOW_CLOUD_OVERRIDE=true` turns the gate off for every finding and every later call; prefer
  `-iok-` for a one-off.

### Cloud model aliases

Only the Ollama model name is checked, so a local alias of a cloud model escapes it. After
`ollama cp gpt-oss:120b-cloud my-model`, `OLLAMA_MODEL=my-model` has no `cloud` tag and counts
as local, although Ollama still forwards the draft to ollama.com. A model built from a
Modelfile whose `FROM` names a cloud model may do the same. The gate then does not run for
`-il-`, and `PROMPT_LOCAL_ONLY=true` does not refuse it.

If you use such an alias, set `PROMPT_GATE_LOCAL=true`, which gates every Ollama call whatever
the model name, or use the model under its `cloud`-tagged name.

## Local only

Each trigger names its own provider, so `PROMPT_PROVIDER` does not make `-i-` local. To rule
out cloud calls, set `PROMPT_LOCAL_ONLY=true`:

```bash
promptmend config set PROMPT_LOCAL_ONLY true
```

Every trigger or command that would send the draft off your machine then pastes
`[promptmend: PROMPT_LOCAL_ONLY=true: … would send the draft off this machine]` instead.

It judges by the base URL and the Ollama model tag, so a relay on `localhost` that forwards to
a cloud API (LiteLLM, an SSH tunnel) still counts as local. If your `localhost` server is such
a relay, set `PROMPT_GATE_LOCAL=true`: the gate then scans `-il-` and `-ilm-` drafts too, with
the same override rules. It does not make `PROMPT_LOCAL_ONLY` refuse them.

## Clipboard safety

The trigger sends whatever is on the clipboard, unseen. On macOS and Windows the CLI first asks
the clipboard which formats it holds, without reading the item, and refuses one that a password
manager marked as concealed or as not for clipboard history, for every trigger, local ones
included:

- macOS: `org.nspasteboard.ConcealedType` or 1Password's own type;
- Windows: `ExcludeClipboardContentFromMonitorProcessing`, `Clipboard Viewer Ignore` or
  `CanIncludeInClipboardHistory` = 0.

The refused item is then cleared from the clipboard. Espanso pastes the refusal through the
clipboard and restores the previous content as plain text, without the marker, so the next
trigger would otherwise send it.

Not covered: browser extensions of password managers (they copy through the web clipboard and
set no marker), apps that set none of these markers, a probe that fails, and Linux. There the
item is sent like any other text.

## Output clean-up

The rewrite comes from a model that read your clipboard, so text copied from a web page can
steer it. Before anything is pasted, the CLI removes:

- a leading `<think>…</think>` reasoning block;
- control characters (an escape sequence could end a terminal's bracketed paste and run the
  lines after it);
- every character Unicode marks as default-ignorable, which renders as nothing and can carry
  hidden instructions for the next AI: zero-width spaces, bidi marks and overrides, Unicode
  tag characters, Hangul fillers and variation selectors.

Other line breaks become newlines. An emoji keeps the one presentation selector and joiner it
needs, a keycap (`1️⃣`) keeps its selector, and the joiners Persian and Indic scripts use
survive. One selector or joiner can still follow each non-ASCII character, so a hidden channel
of a few bits per character remains in non-Latin text, but none in English prose.

The same characters are removed from the draft before it is sent. Still read a rewrite before
running anything it contains.

## Usage history

The CLI keeps a local usage history, so you can see what your triggers cost and how fast they
are (`promptmend stats`). Every `improve` and `persona` run is recorded: one entry per run,
plus one per HTTP request it made (a retried request counts twice, in the same run). It is on
by default (`PROMPT_HISTORY=true`) and stays on this device.

### What is stored

Metadata only, from a fixed list of columns:

- when a trigger ran, which trigger, profile and outcome, and how long it took until the
  output was printed;
- per request: the provider, model, HTTP status, token counts and the cost the provider
  reported, with its unit (OpenRouter credits are never converted to USD).

| Outcome | Meaning |
|---------|---------|
| `ok` | Rewritten |
| `error_marker` | A `[promptmend: …]` error was printed |
| `gate_blocked` | Any data-protection refusal: a flagged draft, `PROMPT_LOCAL_ONLY`, a cloud URL that is not https |
| `validation_failed` | The reply's content was rejected |
| `clipboard_failed` | The clipboard could not be read or written |
| `concealed_refused` | A concealed clipboard item was refused |
| `unexpected_error` | Anything else |

Each managed match passes its own fixed `--trigger-id` (`i`, `iok`, `ip`, `if`, `il`, `ilm`,
`ic`, `p`), stored as the trigger (`-i-`). A run from the terminal, or from a match of your own
without `--trigger-id`, is recorded as `direct`; a match passing an id that is not on that list
is recorded as a managed run with no trigger (unattributed). The trigger is never guessed from the other
options. `-p-` counts once, through its `persona` call.

### What is never stored

Your draft or clipboard, the rewrite, your persona, API keys, form picks or any raw response:
no column is meant for them, and a record's other fields are never read.

Each text column also has a fixed shape. The outcome, endpoint, cost state and error kind are
words from fixed lists; a trigger looks like `-i-`; a profile, provider or version is a short
lower-case or slug name; a response id must start with `gen-`, `msg_` or `chatcmpl-`. Only the
two model columns take `/` and `:` (`openai/gpt-x:free`).

No column takes a space, `@` or `\`, and a value the gate or your `PROMPT_EXTRA_PATTERNS` would flag, or that holds a token-like
word, is not stored. So a sentence, a path, an email, a `user:password@host` or a key cannot
get in. A single harmless-looking word in a name column (say, a model called `hunter2`) can,
which is why only your settings and the provider's reply fill those columns, never the draft.

### Where and how long

- **Where:** `history.sqlite3` in the [data folder](configuration.md#files-and-folders). It is
  never synced and never sent anywhere.
- **How long:** 365 days by default (`PROMPT_HISTORY_RETENTION_DAYS`, at most 36500). Each
  recorded run also deletes up to 100 of the oldest records past that age, so the history
  stays within it without a separate clean-up (after lowering the setting, a large backlog goes
  over the next few runs).
- **Export, prune, reset:** `promptmend history export [--format json|csv] [-o FILE]` writes
  every record (the same metadata, nothing more), `promptmend history prune [--older-than DAYS]`
  deletes older records now, and `promptmend history reset` deletes them all. Uninstalling does
  not delete the history.
- **Off:** `promptmend config set PROMPT_HISTORY false` (or `PROMPT_HISTORY=false` in your
  `.env`), then `promptmend history reset` to
  delete what is there. `setup` and `stats` say that the history is on, what it stores and
  where. Nothing is recorded either when the settings fail to load, since whether you turned
  it off is then unknown.

### Timing and costs

The run is written after its output is printed, so it never changes what is pasted, and the
usage of a reply that then failed (rejected content, a clipboard error) is still kept. Writing
never breaks a trigger and delays it by about 0.25 s at most (1 s once, for the write that
creates the file).

A write that cannot finish in that time (a slow disk, antivirus or endpoint scanning, a
locked, read-only or corrupt file) is kept in a small `history.spool` file next to the
database, in the same [data folder](configuration.md#files-and-folders). It holds exactly the
columns above, nothing else, and each record is checked again before it is stored. The next
recorded run, `promptmend stats`, `promptmend history export` and `promptmend doctor` move its
records into the database. It holds at most 100 records; a record that does not fit (or a
spool that cannot be written) is dropped, and a small `history.lost` file counts the dropped
writes. `promptmend history reset` deletes the spool too. Only a disk the operating system
itself stalls (a hung network drive) can hold a write up longer.

Costs are kept only as the provider reported them; an unknown cost is shown as unknown, never
as 0. For an estimate where a provider reports no cost (Anthropic, OpenAI), create `prices.toml` in the
[config folder](configuration.md#files-and-folders) with your prices per million tokens:

```toml
version = "2026-10-01"
unit = "USD"
[models."claude-sonnet-5"]
input_uncached = 3.00
cache_read = 0.30
cache_write = 3.75
output = 15.00
```

An estimate is stored with the table's `version` and always shown apart from reported costs.

## Update check

`promptmend doctor` and the [interface](interface.md#home)'s Home tab say whether a newer
release exists, and so does the `doctor` that `install.ps1` runs at its end. To find out,
they ask pypi.org for the list of PromptMend releases (`https://pypi.org/pypi/promptmend/json`)
once a day, and keep the answer (the newest version and when it was asked) in
`update-check.json` in the [data folder](configuration.md#files-and-folders). When pypi.org
cannot be reached, that is kept too, and they ask again at most once an hour. A trigger never
asks.

pypi.org sees what any download from it shows: your IP address and a User-Agent naming
PromptMend and its version. Nothing else is sent: no setting, key, draft or history. A proxy
set in `HTTPS_PROXY` is used.

To turn it off:

```bash
promptmend config set PROMPT_UPDATE_CHECK false
```

Then nothing asks, and Home shows only the installed version. A value that is not `true`
or `false`, or a settings file that cannot be read, also makes no request, since it may have
meant `false`.

## See also

- [Configuration](configuration.md#privacy-and-gate): the gate and history settings
- [Troubleshooting](troubleshooting.md#gate-and-blocked-calls): blocked calls
- [SECURITY.md](../SECURITY.md): report a way around the gate privately
