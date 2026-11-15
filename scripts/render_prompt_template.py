"""Write the default profile's fixed wordings into the -p- template (#283).

    uv run python scripts/render_prompt_template.py           # rewrite the wording lines
    uv run python scripts/render_prompt_template.py --check   # exit 1 when they are stale

prompts/default.md is the only source of the fixed wordings (promptmend.wordings). The -p-
template, espanso/match/prompts-template.yml, stays a checked-in match file: the script
rewrites only its wording lines (steps 1a/ 1b/ 2/ 4a/ 4b/ 5/ and the line after <OUTPUTS>)
and keeps every other line, so the template's own text (its header, CONTEXT, GOAL, step 3,
CONSTRAINTS, INPUTS, the [pick one ...] lines) is still edited in the file. The independent
review's [domain] becomes the template's fill-in [XXX]. Rerun it after editing a wording in
default.md, then scripts/update_match_history.py (the match file changed);
tests/test_wordings.py fails while the template is stale.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from promptmend.wordings import DOMAIN, Wordings, default_wordings

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "espanso" / "match" / "prompts-template.yml"
# What the template asks the user to fill in where a rewrite names the domain.
FILL_IN = "[XXX]"
_STEP = re.compile(r"^(?P<indent> +)(?P<label>1a|1b|2|4a|4b|5)/ ")


def step_wordings(wordings: Wordings) -> dict[str, str]:
    """Each wording step of the template, by its label."""
    return {
        "1a": wordings.plan,
        "1b": wordings.execute,
        "2": wordings.inputs,
        "4a": wordings.independent.replace(DOMAIN, FILL_IN),
        "4b": wordings.self_review,
        "5": wordings.judgment,
    }


def _block(indent: str, lead: str, wording: str) -> list[str]:
    # A wording sits in a YAML `|` block of an Espanso match: a tab or a `{{` (an Espanso
    # variable) would change what the snippet expands to, so neither may come through.
    if "\t" in wording or "{{" in wording:
        raise ValueError(f"wording {lead!r} has a tab or {{{{, which the template cannot carry")
    first, *rest = wording.split("\n")
    return [f"{indent}{lead}{first}", *(f"{indent}{line}" if line else "" for line in rest)]


def render(template: str, wordings: Wordings) -> str:
    """The template with each wording line, and the line after <OUTPUTS>, taken from
    `wordings`; every other line kept. A step's continuation lines (indented deeper, such as
    the self-review's checks) are replaced with it."""
    steps = step_wordings(wordings)
    seen: list[str] = []
    out: list[str] = []
    lines = template.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        step = _STEP.match(line)
        if step:
            indent, label = step["indent"], step["label"]
            seen.append(label)
            out.extend(_block(indent, f"{label}/ ", steps[label]))
            while i < len(lines) and lines[i].startswith(indent + " "):
                i += 1
            continue
        out.append(line)
        if line.strip() == "<OUTPUTS>" and i < len(lines):
            seen.append("<OUTPUTS>")
            indent = line[: len(line) - len(line.lstrip(" "))]
            out.extend(_block(indent, "", wordings.outputs))
            i += 1
    expected = [*steps, "<OUTPUTS>"]
    if sorted(seen) != sorted(expected):
        raise ValueError(f"the template must have each of {expected} once, found {seen}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the fixed wordings into the -p- template.")
    parser.add_argument("--check", action="store_true", help="only report a stale template")
    args = parser.parse_args(argv)
    current = TEMPLATE.read_bytes().decode("utf-8")
    rendered = render(current, default_wordings())
    name = TEMPLATE.relative_to(REPO).as_posix()
    if rendered == current:
        print(f"{name} is current")
        return 0
    if args.check:
        print(f"{name} is stale: run scripts/render_prompt_template.py", file=sys.stderr)
        return 1
    TEMPLATE.write_bytes(rendered.encode("utf-8"))
    print(f"wrote {name}; now run scripts/update_match_history.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
