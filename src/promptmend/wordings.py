"""The default profile's fixed wordings, read from prompts/default.md, their only source (#283).

A rewrite copies them word for word: the planning step's two variants, the inputs step, the
review step's two variants, the judgment step, and the OUTPUTS line for a document. Two other
places use them and take them from here rather than carrying copies:

- the `-p-` template, espanso/match/prompts-template.yml: a checked-in match file (Espanso
  reads static YAML), whose wording lines scripts/render_prompt_template.py writes;
  tests/test_wordings.py fails when the file is not what the script writes;
- scripts/bench_models.py, which scores a rewrite by the opening words of each wording.

Contributor tooling: nothing on the trigger path imports this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .prompt_builder import PROFILES

# The placeholder default.md's independent-review variant carries for the domain.
DOMAIN = "[domain]"

_STEP = re.compile(
    r'^<step position="[^"]*" name="(\w+)">\n(.*?)\n</step>$', re.MULTILINE | re.DOTALL
)
_VARIANT = re.compile(r'^<variant id="([ab])">(.*?)</variant>$', re.MULTILINE | re.DOTALL)
_OUTPUTS = re.compile(r"^- For a document, .*, use exactly this line: (.+)$", re.MULTILINE)


@dataclass(frozen=True)
class Wordings:
    """The fixed wordings, each exactly as default.md words it (self_review spans lines)."""

    plan: str  # planning step, variant (a)
    execute: str  # planning step, variant (b)
    inputs: str
    independent: str  # review step, variant (a), with the DOMAIN placeholder
    self_review: str  # review step, variant (b)
    judgment: str
    outputs: str  # the OUTPUTS line for a document


def _variants(body: str, step: str) -> dict[str, str]:
    found = dict(_VARIANT.findall(body))
    if sorted(found) != ["a", "b"]:
        raise ValueError(f"the {step} step needs exactly the variants a and b")
    return found


def _last_line(body: str, step: str) -> str:
    """The wording of an always-present step: the line after its `Always...:` lead-in."""
    lines = body.splitlines()
    if len(lines) != 2 or not lines[0].startswith("Always"):
        raise ValueError(f"the {step} step must be an `Always...:` line and its wording")
    return lines[1]


def parse(profile: str) -> Wordings:
    """The fixed wordings of a profile laid out like default.md; ValueError when one is
    missing, so a restructured default.md fails loudly instead of yielding a wrong wording."""
    steps = dict(_STEP.findall(profile))
    missing = sorted({"planning", "inputs", "review", "judgment"} - set(steps))
    if missing:
        raise ValueError(f"no {', '.join(missing)} step")
    outputs = _OUTPUTS.findall(profile)
    if len(outputs) != 1:
        raise ValueError("no single `use exactly this line:` OUTPUTS rule")
    planning = _variants(steps["planning"], "planning")
    review = _variants(steps["review"], "review")
    if DOMAIN not in review["a"]:
        raise ValueError(f"the review step's variant a has no {DOMAIN} placeholder")
    return Wordings(
        plan=planning["a"],
        execute=planning["b"],
        inputs=_last_line(steps["inputs"], "inputs"),
        independent=review["a"],
        self_review=review["b"],
        judgment=_last_line(steps["judgment"], "judgment"),
        outputs=outputs[0],
    )


def default_wordings() -> Wordings:
    """The fixed wordings of the shipped default profile."""
    return parse(PROFILES["default"])
