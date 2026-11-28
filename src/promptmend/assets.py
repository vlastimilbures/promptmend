"""The Espanso match files, shipped in the wheel as package data.

The repo's `espanso/match/` stays the source of truth; Hatch's force-include copies it to
`promptmend/espanso/match/` when the wheel is built. An editable install maps only
`src/promptmend`, so there the files are read from the checkout instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from importlib.resources import files
from importlib.resources.abc import Traversable

from .paths import _PROJECT_ROOT
from .redaction import safe_repr


def match_dir() -> Traversable:
    """The folder holding the match files: the packaged copy, else the editable checkout's."""
    packaged = files(__package__) / "espanso" / "match"
    if packaged.is_dir():
        return packaged
    checkout = _PROJECT_ROOT / "espanso" / "match"
    if checkout.is_dir():
        return checkout
    raise FileNotFoundError("The Espanso match files are missing from this installation")


def match_names() -> list[str]:
    """File names of the match files (`prompts-llm.yml`, ...), sorted."""
    return sorted(p.name for p in match_dir().iterdir() if p.name.endswith(".yml"))


def read_match(name: str) -> str:
    """One match file's text, as shipped (the CLI path still a placeholder)."""
    if name not in match_names():
        raise ValueError(f"No match file named {safe_repr(name)}")
    return (match_dir() / name).read_text(encoding="utf-8")


# A match's trigger line, commented out or not, and the CLI call in its script var's args.
TRIGGER_LINE = re.compile(r'^\s*(#\s*)?- trigger: "([^"]+)"')
# The first item is the placeholder as shipped, or the launcher a deploy put there.
_CMD = re.compile(r'^\s*(?:#\s*)?args: \["[^"]+", "(\w+)"(.*)\]')
_OPTION = re.compile(r'"--(provider|profile|tier)", "([^"]+)"')
# The `type: shell` form that releases up to 0.20.0 deployed is not read (#414): in such a
# file each trigger reads as one with no command line (command None), until a deploy
# replaces the file, which deploy recognises as stale.


@dataclass(frozen=True)
class Trigger:
    """One trigger in a shipped match file. ``command`` is None for a static snippet; the
    options are the ones its command line fixes, None when it leaves them to the settings."""

    trigger: str
    file: str
    active: bool  # False: commented out, as -ic- ships
    command: str | None = None
    provider: str | None = None
    profile: str | None = None
    tier: str | None = None


def triggers() -> list[Trigger]:
    """Every trigger the match files ship, in file order, with what its CLI call fixes."""
    return [t for name in match_names() for t in triggers_in(read_match(name), name)]


def triggers_in(text: str, name: str) -> list[Trigger]:
    """The triggers of one match file's text, shipped or deployed, in file order."""
    found: list[Trigger] = []
    current: Trigger | None = None
    for line in text.splitlines():
        if head := TRIGGER_LINE.match(line):
            if current is not None:
                found.append(current)
            current = Trigger(head.group(2), name, active=not head.group(1))
        elif current is not None and (found_cmd := _CMD.match(line)):
            options = dict(_OPTION.findall(found_cmd.group(2)))
            current = replace(
                current,
                command=found_cmd.group(1),
                provider=options.get("provider"),
                profile=options.get("profile"),
                tier=options.get("tier"),
            )
    if current is not None:
        found.append(current)
    return found
