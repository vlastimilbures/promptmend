"""prompts/default.md is the only source of the fixed wordings (#283): the -p- template is
written from it by scripts/render_prompt_template.py, and the bench scores against it."""

from __future__ import annotations

import dataclasses
import importlib.util
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from promptmend.prompt_builder import PROFILES
from promptmend.wordings import DOMAIN, Wordings, default_wordings, parse

REPO = Path(__file__).resolve().parents[1]

if TYPE_CHECKING:
    import render_prompt_template as rpt
else:
    _spec = importlib.util.spec_from_file_location(
        "render_prompt_template", REPO / "scripts" / "render_prompt_template.py"
    )
    assert _spec
    assert _spec.loader
    rpt = importlib.util.module_from_spec(_spec)
    sys.modules["render_prompt_template"] = rpt
    _spec.loader.exec_module(rpt)


def _template() -> str:
    return rpt.TEMPLATE.read_bytes().decode("utf-8")


def _replace() -> str:
    match = yaml.safe_load(_template())["matches"][0]
    assert match["trigger"] == "-p-"
    text: str = match["replace"]
    return text


# The checked-in -p- template is exactly what the generator writes from default.md, so a
# wording edited in one place only cannot be merged.
def test_template_is_generated_from_default_profile() -> None:
    assert rpt.TEMPLATE == REPO / "espanso" / "match" / "prompts-template.yml"
    assert rpt.render(_template(), default_wordings()) == _template(), (
        "prompts-template.yml is stale: run scripts/render_prompt_template.py, then "
        "scripts/update_match_history.py"
    )
    assert rpt.main(["--check"]) == 0


def test_parse_reads_each_wording_from_its_step() -> None:
    wordings = default_wordings()
    profile = PROFILES["default"]
    for name, wording in dataclasses.asdict(wordings).items():
        assert wording, name
        assert wording in profile, name
    assert wordings.plan.startswith("Plan ")
    assert wordings.execute.startswith("Execute")
    assert DOMAIN in wordings.independent
    assert "\n  - " in wordings.self_review
    assert wordings.inputs.startswith("Load and validate")
    assert wordings.judgment.startswith("Flag ")
    assert "\n" not in wordings.outputs


# What the snippet expands to: each wording, in its numbered step, [domain] as the fill-in.
def test_template_expands_to_the_wordings() -> None:
    wordings = default_wordings()
    replace = _replace()
    for label, wording in rpt.step_wordings(wordings).items():
        assert f"\n{label}/ {wording}\n" in replace, label
    assert rpt.FILL_IN in rpt.step_wordings(wordings)["4a"]
    assert DOMAIN not in replace
    assert f"<OUTPUTS>\n{wordings.outputs}\n</OUTPUTS>" in replace


def _changed() -> Wordings:
    w = default_wordings()
    return dataclasses.replace(
        w,
        plan="Plan it.",
        self_review="Check it:\n  - One.\n  - Two.\n  - Three.",
        independent=w.independent.replace("Use a separate agent", "Ask an agent"),
        outputs="plain text",
    )


# A wording edit reaches the template and nothing else changes; a longer or shorter
# multi-line wording replaces all of the old one's lines.
def test_render_follows_an_edited_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    changed = _changed()
    rendered = rpt.render(_template(), changed)
    replace = yaml.safe_load(rendered)["matches"][0]["replace"]
    assert "\n1a/ Plan it.\n1b/ " in replace
    assert "\n4b/ Check it:\n  - One.\n  - Two.\n  - Three.\n5/ " in replace
    assert "\n4a/ Ask an agent with [XXX] domain knowledge" in replace
    assert "<OUTPUTS>\nplain text\n</OUTPUTS>" in replace
    assert "Accuracy:" not in replace
    # Every line but the wording lines (a step, a self-review check, the OUTPUTS line) stays.
    wording_line = re.compile(r"^ +(?:1a|1b|2|4a|4b|5)/ |^ {8}- |^ +(?:structured \.md|plain text)")
    kept = [line for line in _template().split("\n") if not wording_line.search(line)]
    assert [line for line in rendered.split("\n") if not wording_line.search(line)] == kept
    assert rpt.render(rendered, default_wordings()) == _template()

    target = tmp_path / "espanso" / "match" / "prompts-template.yml"
    target.parent.mkdir(parents=True)
    target.write_bytes(rendered.encode("utf-8"))
    monkeypatch.setattr(rpt, "REPO", tmp_path)
    monkeypatch.setattr(rpt, "TEMPLATE", target)
    assert rpt.main(["--check"]) == 1
    assert "stale" in capsys.readouterr().err
    assert rpt.main([]) == 0
    assert "update_match_history" in capsys.readouterr().out
    assert target.read_bytes() == _template().encode("utf-8")
    assert rpt.main([]) == 0
    assert "is current" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["a\ttab", "an {{espanso}} variable"])
def test_render_refuses_what_the_snippet_cannot_carry(bad: str) -> None:
    with pytest.raises(ValueError, match="tab or"):
        rpt.render(_template(), dataclasses.replace(default_wordings(), judgment=bad))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.replace("      5/ ", "      6/ ", 1),
        lambda t: t.replace("      1b/ ", "      1a/ ", 1),
        lambda t: t.replace("<OUTPUTS>", "<OUT>", 1),
    ],
)
def test_render_needs_each_wording_line_once(mutate: Callable[[str], str]) -> None:
    with pytest.raises(ValueError, match="once"):
        rpt.render(mutate(_template()), default_wordings())


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda t: t.replace('name="judgment"', 'name="verdict"'), "no judgment step"),
        (lambda t: t.replace("use exactly this line:", "use this line:"), "OUTPUTS rule"),
        (lambda t: t.replace('<variant id="b">Execute', '<variant id="c">Execute'), "planning"),
        (lambda t: t.replace("Always:\n", ""), "judgment step must be"),
        (lambda t: t.replace("Use a separate agent with [domain]", "Use an agent"), "placeholder"),
    ],
)
def test_parse_fails_loudly_on_a_restructured_profile(
    mutate: Callable[[str], str], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        parse(mutate(PROFILES["default"]))


@pytest.mark.parametrize("bad", ["Check it:\n\n  - One.", "Check it:\n- One."])
def test_render_refuses_an_unindented_continuation_line(bad: str) -> None:
    with pytest.raises(ValueError, match="empty or unindented"):
        rpt.render(_template(), dataclasses.replace(default_wordings(), self_review=bad))


def test_render_needs_outputs_closed_after_its_line() -> None:
    broken = _template().replace("      </OUTPUTS>", "      more\n      </OUTPUTS>", 1)
    with pytest.raises(ValueError, match="followed by </OUTPUTS>"):
        rpt.render(broken, default_wordings())


_REWRITE = re.compile(r"<rewrite>\n(.*?)\n</rewrite>", re.DOTALL)
_INSTRUCTIONS = re.compile(r"<INSTRUCTIONS>\n(.*?)\n</INSTRUCTIONS>", re.DOTALL)
_QUOTE = re.compile(r'even when the draft names its own activity: "([^"]+)", never')


def _example_steps(rewrite: str) -> list[str]:
    """The numbered steps of a few-shot example, each with its continuation lines."""
    found = _INSTRUCTIONS.search(rewrite)
    assert found, "an example rewrite has no INSTRUCTIONS"
    steps: list[str] = []
    for line in found.group(1).split("\n"):
        number = re.match(r"(\d+)/ ", line)
        if number:
            assert int(number.group(1)) == len(steps) + 1, line
            steps.append(line[number.end() :])
        else:
            assert steps, line
            assert line.startswith("  "), line
            steps[-1] += "\n" + line
    return steps


def _independent(wordings: Wordings) -> re.Pattern[str]:
    before, after = wordings.independent.split(DOMAIN)
    return re.compile(re.escape(before) + r"[^\n\[\]]+" + re.escape(after))


# default.md's own few-shot examples repeat the fixed wordings in full: each example's
# planning, inputs, review and judgment steps are the parsed wordings word for word (review
# variant (a) with a concrete domain for [domain]), so a variant edited without its examples
# fails here.
def test_profile_examples_use_the_wordings() -> None:
    wordings = default_wordings()
    rewrites = _REWRITE.findall(PROFILES["default"])
    assert len(rewrites) == 2
    reviews = []
    for rewrite in rewrites:
        steps = _example_steps(rewrite)
        assert len(steps) >= 5
        assert steps[0] in (wordings.plan, wordings.execute)
        assert steps[1] == wordings.inputs
        assert steps[-2] == wordings.self_review or _independent(wordings).fullmatch(steps[-2])
        assert steps[-1] == wordings.judgment
        reviews.append(steps[-2])
    assert wordings.self_review in reviews
    assert any(_independent(wordings).fullmatch(r) for r in reviews)


# The formatting rule quotes planning variant (a)'s opening as the wording to copy.
def test_formatting_rule_quotes_the_plan_wording() -> None:
    quoted = _QUOTE.findall(PROFILES["default"])
    assert len(quoted) == 1
    assert re.match(re.escape(quoted[0]) + r"\b", default_wordings().plan)
