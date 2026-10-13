from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from .config import _user_config_dir
from .redaction import safe_repr

_PROMPT_DIR = files(__package__) / "prompts"


def _load_profiles() -> dict[str, str]:
    # Sorted: a directory lists in the file system's order (ext4 hashes names), and the order
    # shows in --help texts, errors and the interface.
    return {
        p.name.removesuffix(".md"): p.read_text(encoding="utf-8").strip()
        for p in sorted(_PROMPT_DIR.iterdir(), key=lambda p: p.name)
        if p.name.endswith(".md")
    }


# The built-in profiles shipped in the package. A user's own live in user_profiles_dir().
PROFILES: dict[str, str] = _load_profiles()

# Retired profile names that still resolve, so an existing .env or --profile keeps working.
# `default-pro` was `default` minus one clause; one prompt now serves both tiers (#44).
ALIASES = {"default-pro": "default"}

# Replaced in a profile by the rule for how CONTEXT opens (see PROMPT_PERSONA).
PERSONA_TOKEN = "{{PERSONA_RULE}}"  # noqa: S105 - a template placeholder, not a secret
# Replaced by the persona and a space, or by nothing without one: the few-shot examples open
# CONTEXT with it, so they show the persona opening the rule asks for (#49).
PERSONA_OPENING_TOKEN = "{{PERSONA_OPENING}}"  # noqa: S105 - a template placeholder

# A user profile's name is its file name without `.md`: no dots, separators or spaces, so a
# --profile value cannot reach outside the profile directory. Lower case only, so `Default`
# can never reach a user default.md through a case-insensitive file system (macOS, Windows),
# and never a Windows device name (`con.md` opens the console there).
PROFILE_NAME = re.compile(r"(?!(?:con|prn|aux|nul|com[1-9]|lpt[1-9])$)[a-z0-9][a-z0-9_-]{0,63}")


def user_profiles_dir() -> Path:
    """The user's own profiles, `<name>.md` in the config dir that also holds .env. An
    upgrade never touches it, unlike the package's prompts/."""
    return _user_config_dir() / "profiles"


def _read_user_profile(name: str) -> str | None:
    """A user profile's text, or None when there is no such file. Only looked up for a name
    that is not built in or is listed in PROMPT_PROFILE_OVERRIDES, so the usual trigger
    touches no file."""
    if not PROFILE_NAME.fullmatch(name):
        return None
    folder = user_profiles_dir()
    try:
        # The exact file name must be listed: a case-insensitive file system would otherwise
        # open `Mine.MD` for `mine`, which Linux would not, and user_profiles() reports invalid.
        if f"{name}.md" not in {p.name for p in folder.iterdir()}:
            return None
        text = (folder / f"{name}.md").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        # Named, not the path: the marker is pasted into the focused app.
        raise ValueError(f"Cannot read user profile {name}.md ({type(exc).__name__})") from None
    if not text:
        raise ValueError(f"User profile {name}.md is empty")
    return text


def persona_rule(persona: str) -> str:
    if persona:
        return (
            f'open with "{persona}" If the draft states a different role, company, or '
            "persona, use that one instead."
        )
    return (
        "if the draft explicitly states the user's role, company, or persona, open with it; "
        "otherwise do not state or guess any role and open directly with what is wanted."
    )


def persona_opening(persona: str) -> str:
    return f"{persona} " if persona else ""


def render(template: str, persona: str = "") -> str:
    """Fill a profile's tokens in one pass, so a persona that holds a token's text is never
    filled again. Not str.format: templates contain braces."""
    values = {PERSONA_TOKEN: persona_rule(persona), PERSONA_OPENING_TOKEN: persona_opening(persona)}
    return re.sub("|".join(map(re.escape, values)), lambda m: values[m.group()], template)


def resolve_profile_name(profile: str) -> str:
    """The profile name ``profile`` stands for: itself, or the built-in a retired alias names."""
    return profile if profile in PROFILES else ALIASES.get(profile, profile)


def system_prompt(profile: str, persona: str = "", overrides: Collection[str] = ()) -> str:
    """The rendered profile: a built-in, or `<name>.md` in user_profiles_dir() for a name
    that is not built in. A user file named like a built-in replaces it only when the name is
    in ``overrides`` (PROMPT_PROFILE_OVERRIDES)."""
    return render(template(profile, overrides), persona)


def template(profile: str, overrides: Collection[str] = ()) -> str:
    """system_prompt()'s profile before render(), its tokens unfilled."""
    name = resolve_profile_name(profile)
    text = None
    if name not in PROFILES or name in overrides:
        text = _read_user_profile(name)
    if text is None:
        text = PROFILES.get(name)
    if text is None:
        # Built-ins first, in the order they always had; the user's own after them, sorted.
        own = sorted(p.name for p in user_profiles(overrides) if p.status == ADDED)
        known = ", ".join([*PROFILES, *own])
        raise ValueError(f"Unknown profile: {safe_repr(profile)}. Choose from: {known}")
    return text


# user_profiles() statuses: a new profile, a built-in replaced by explicit opt-in, a file
# ignored because it is named like a built-in (or an alias) without the opt-in, a file whose
# name no --profile can select, and an opted-in built-in that has no file.
ADDED = "added"
OVERRIDES = "overrides"
SHADOWED = "shadowed"
INVALID_NAME = "invalid name"
MISSING = "missing"


@dataclass(frozen=True)
class UserProfile:
    name: str
    path: Path
    status: str


def user_profiles(overrides: Collection[str] = ()) -> list[UserProfile]:
    """What user_profiles_dir() holds and how system_prompt() treats each file, for a
    doctor or `profiles` command to report. Overrides without a file come last as MISSING."""
    folder = user_profiles_dir()
    try:
        paths = sorted(p for p in folder.iterdir() if p.suffix.lower() == ".md" and p.is_file())
    except OSError:
        paths = []
    found = []
    for path in paths:
        name = path.stem
        # `x.MD` is never selected either: lookups need the exact `<name>.md`.
        if path.suffix != ".md" or not PROFILE_NAME.fullmatch(name):
            status = INVALID_NAME
        elif name in PROFILES:
            status = OVERRIDES if name in overrides else SHADOWED
        else:
            status = SHADOWED if name in ALIASES else ADDED
        found.append(UserProfile(name, path, status))
    listed = {p.name for p in found}
    missing = [n for n in overrides if n not in listed]
    return found + [UserProfile(n, folder / f"{n}.md", MISSING) for n in missing]


# Present in every profile that emits the golden template (CONTEXT ... OUTPUTS).
TEMPLATE_MARKER = "<output_template>"

# flash-lite sometimes closes CONTEXT with the next section's tag, "<CONTEXT>...</GOAL>" right
# before "<GOAL>", a decoding slip that comes and goes with unrelated wording changes. Only
# that exact pattern is repaired, and only in the rewrite's first section, with each tag on its
# own line: any other malformed output, and tags inside pasted material, stay as they are.
_CONTEXT_CLOSED_AS_GOAL = re.compile(
    r"(<CONTEXT>(?:(?!</CONTEXT>|</?GOAL>).)*?\n[ \t]*)</GOAL>([ \t]*\r?\n\s*<GOAL>[ \t]*\r?\n)",
    re.DOTALL,
)


def repair_template_tags(text: str) -> str:
    """Close a CONTEXT section that the model closed with </GOAL> just before <GOAL>."""
    start = text.find("<CONTEXT>")
    slip = _CONTEXT_CLOSED_AS_GOAL.match(text, start) if start >= 0 else None
    if slip is None:
        return text
    return text[: slip.end(1)] + "</CONTEXT>" + text[slip.start(2) :]


# A fence line: three or more backticks or tildes, optionally followed by an info string
# (a language tag, maybe with attributes).
_FENCE = re.compile(r"(`{3,}|~{3,})[ \t]*([^`\n]{0,80})")


def strip_outer_fence(text: str) -> str:
    """Remove a code fence wrapped around the whole reply, which Espanso would otherwise paste
    as-is. Only when the first and last lines open and close one block: fences inside it
    (tagged, or longer than the outer one) are kept, and a reply with text outside the fence,
    two blocks, an untagged inner block, an unclosed fence or nothing inside is returned
    unchanged."""
    lines = text.strip().splitlines()
    opening = _FENCE.fullmatch(lines[0].strip()) if len(lines) > 2 else None
    if opening is None:
        return text
    fence = opening.group(1)
    closing = lines[-1].strip()
    if closing[:1] != fence[0] or closing.strip(fence[0]) or len(closing) < len(fence):
        return text
    body = lines[1:-1]
    depth = 0
    for line in body:
        inner = _FENCE.fullmatch(line.strip())
        if inner is None or inner.group(1)[0] != fence[0]:
            continue
        if inner.group(2):  # a tagged fence opens a nested block
            depth += 1
        elif depth:
            depth -= 1
        elif len(inner.group(1)) >= len(fence):  # closes the outer block early: two blocks
            return text
    unwrapped = "\n".join(body)
    # An empty block would paste nothing: keep the reply, so the user sees what came back.
    return unwrapped if unwrapped.strip() else text
