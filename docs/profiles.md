# Profiles and persona

A profile is the system prompt that tells the model how to rewrite your draft. This page
covers the two built-in profiles, how to add your own or replace a built-in, and the persona
that opens every rewrite with your role. You need it to change what a rewrite looks like.

## Built-in profiles

The built-in profiles ship in the package, in `src/promptmend/prompts/`.

### default

`default` rewrites the draft into the golden template, the same one the static `-p-` snippet
gives you: `CONTEXT / GOAL / INSTRUCTIONS / CONSTRAINTS / INPUTS / OUTPUTS`. Both tiers
(`-i-` and `-ip-`) send it.

- **Step 1** is "plan first" for multi-step or ambiguous work, otherwise "execute, but state
  assumptions".
- **The review step** is decided by who reads the result: a self-review checklist when only
  you, a colleague, your manager or your team will, and an independent reviewer for anyone
  else (an executive, a board or committee, a regulator, anyone outside the organisation, or
  published text), however short.
- **`CONSTRAINTS`** lists the rules the result must respect (length, tone, deadline, format,
  standards, data limits), with an "Out of scope:" line when the draft implies one.
- **`OUTPUTS`** is the `.md` line for documents and a matching format (plain-text email, code
  block, slides…) for everything else.

The whole draft is treated as material to rewrite, never as instructions:

- a question becomes a prompt that asks for the answer;
- pasted emails or notes up to about 60 lines are copied into `INPUTS` in full; longer
  material is described there and flagged `[REVIEW: …]`;
- text such as "ignore previous instructions" is dropped.

The rewrite is always in English; a draft in another language gets a `- Language: …`
constraint, so the result comes back in that language. Specifics from the draft (numbers,
names, dates, deliverables) are kept, and anything missing is flagged `[REVIEW: …]` rather than
invented.

The prompt itself is organised in lowercase XML sections (`<section_rules>`, `<step>`,
`<decision_rule>`, `<example>`…), which keeps its own scaffolding visibly apart from the
uppercase sections the model must write. `default-pro`, the pro tier's own
variant in 0.12.0 and 0.13.0, is now an alias of `default`.

### general

`general` is a short (about 200 words) rewrite for the local triggers (`-il-`, `-ilm-`), small
enough for an 8B model. It:

- treats the whole clipboard as the draft (data, never instructions to the rewriter);
- copies pasted material word for word, with a constraint that instructions inside it must not
  be followed;
- adds no facts, roles or audiences;
- writes in the language of your own request;
- returns only the prompt, without a preamble or a code fence.

A reply wrapped in one code fence anyway is pasted without it, for every profile.

## Your own profiles

Your own profiles live outside the package, so an upgrade never replaces them: in `profiles/`
in the [config folder](configuration.md#files-and-folders), for example
`~/.config/promptmend/profiles/<name>.md` (`%APPDATA%\promptmend\profiles\<name>.md` on
Windows).

1. Create `<name>.md` holding the system prompt as plain text. It may use `{{PERSONA_RULE}}`,
   like the built-ins, where your persona should be applied.
2. Select it by its name: `--profile <name>`, `PROMPT_PROFILE` or `PROMPT_PRO_PROFILE`.
3. Check it with `promptmend profiles list`.

A name is lower-case letters, digits, `-` and `_`: no dots or spaces, and not a Windows device
name such as `con`. The file must be named exactly `<name>.md`.

A profile you added under `src/promptmend/prompts/` in a checkout keeps working there, but
belongs in this folder: copy it over by hand.

## Replacing a built-in

A file named like a built-in (`default.md`, `general.md`) is ignored, so a stray copy cannot
silently change what every trigger sends. To replace a built-in on purpose, list it in
`PROMPT_PROFILE_OVERRIDES`:

```bash
promptmend config set PROMPT_PROFILE_OVERRIDES default
```

`promptmend config unset PROMPT_PROFILE_OVERRIDES` goes back to the built-in. The package's own
files are never changed.

## Persona

Set `PROMPT_PERSONA` to a first-person sentence:

```bash
promptmend config set PROMPT_PERSONA "I am working as a Head of Data at Example Corp."
```

- The `default` rewrite then opens `CONTEXT` with it, unless the draft names a different role.
- `-p-` inserts it for you, even while another setting is invalid; only a settings file it
  cannot read gives the `[role]` placeholder.
- Left empty, the rewrite uses only a role the draft itself states and never guesses one.

The persona goes with every call whose profile uses it, and the data-protection gate does not
scan it. Keep it to what you are happy to send to every provider; `config validate` and
`doctor` warn when it matches a gate pattern. See
[What is sent, and to whom](privacy.md#what-is-sent-and-to-whom).

## Adding a built-in profile

To ship a new built-in profile with the project, see
[CONTRIBUTING.md, "Add a profile"](../CONTRIBUTING.md#add-a-profile).

## See also

- [Usage](usage.md#triggers): which trigger uses which profile
- [Configuration](configuration.md#general): the profile and persona settings
- [Benchmark](benchmark.md): how the `default` profile is scored
