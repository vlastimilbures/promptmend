import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import yaml

from promptmend.config import EFFORTS, KEEP, TIERS, split_model_spec
from promptmend.factory import PROVIDER_NAMES
from promptmend.prompt_builder import PROFILES
from promptmend.recorder import TRIGGER_IDS

ESPANSO_ROOT = Path(__file__).parents[1] / "espanso"
MATCH_FILES = sorted((ESPANSO_ROOT / "match").glob("*.yml"))
KNOWN_PROVIDERS = set(PROVIDER_NAMES)


# Every YAML file under espanso/ parses to something.
def test_espanso_yaml_parses() -> None:
    for path in ESPANSO_ROOT.rglob("*.yml"):
        assert yaml.safe_load(path.read_text(encoding="utf-8")) is not None


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def _commented_matches(path: Path) -> list[dict[str, Any]]:
    """Matches shipped commented out (such as -ic-): each `# - trigger:` block, uncommented."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        body = line.lstrip()
        if body.startswith("# - trigger:"):
            current = []
            blocks.append(current)
        elif current is None or not body.startswith("#"):
            current = None
            continue
        current.append(line.replace("# ", "", 1))
    try:
        return [match for block in blocks for match in yaml.safe_load("\n".join(block))]
    except yaml.YAMLError as exc:
        raise AssertionError(f"{path.name}: a commented-out match does not parse") from exc


# Every match file has a top-level matches: list.
def test_match_files_have_matches_list() -> None:
    for path in MATCH_FILES:
        data = _load(path)
        assert isinstance(data.get("matches"), list), f"{path.name} has no matches: list"


# Triggers are unique across every match file (the class of bug that let a
# duplicate or dead trigger silently shadow another one).
def test_triggers_are_unique_across_files() -> None:
    seen: dict[str, str] = {}
    for path in MATCH_FILES:
        for match in _load(path)["matches"]:
            trigger = match["trigger"]
            assert trigger not in seen, (
                f"{trigger!r} defined in both {seen[trigger]} and {path.name}"
            )
            seen[trigger] = path.name


def _all_matches() -> list[dict[str, Any]]:
    """Every match, the commented-out ones (-ic-) included."""
    matches = [m for path in MATCH_FILES for m in _load(path)["matches"]]
    return matches + [m for path in MATCH_FILES for m in _commented_matches(path)]


def _cli_vars(match: dict[str, Any]) -> list[dict[str, Any]]:
    """The vars of a match that name the CLI anywhere in their params."""
    return [v for v in match.get("vars", []) if "__PROMPT_WORKFLOW__" in str(v.get("params"))]


def _cli_args(match: dict[str, Any]) -> list[list[str]]:
    return [var["params"]["args"] for var in _cli_vars(match)]


def _calls_cli(match: dict[str, Any]) -> bool:
    """Whether a match runs the CLI."""
    return bool(_cli_vars(match))


def _script_args() -> Iterator[tuple[str, list[str]]]:
    for match in _all_matches():
        for args in _cli_args(match):
            yield match["trigger"], args


def _option(args: list[str], name: str) -> str | None:
    """The value after ``name`` in an argv list, or None."""
    return args[args.index(name) + 1] if name in args else None


# Every CLI call is a `type: script` var whose args start with the CLI placeholder, one argv
# item per string (#18). Espanso starts args[0] with no shell: a `type: shell` var runs
# through PowerShell on Windows, which cannot run a quoted path followed by arguments. Every
# item is a string (Espanso drops any other) and one token, and the placeholder appears only
# as args[0].
def test_cli_vars_are_scripts_starting_with_the_cli() -> None:
    for match in _all_matches():
        trigger = match["trigger"]
        for var in match.get("vars", []):
            assert var.get("type") != "shell", f"{trigger}: no shell vars (#18)"
        for var in _cli_vars(match):
            assert var.get("type") == "script", f"{trigger} must be a script var"
            assert set(var["params"]) == {"args"}, f"{trigger}: args only"
            args = var["params"]["args"]
            assert isinstance(args, list), trigger
            assert args[0] == "__PROMPT_WORKFLOW__", f"{trigger} must start with the CLI"
            assert args[1] in ("improve", "persona"), trigger
            assert "__PROMPT_WORKFLOW__" not in str(args[1:]), trigger
            for arg in args:
                assert isinstance(arg, str), f"{trigger}: {arg!r} is not a string"
                assert re.fullmatch(r"[^\s\"'`$%]+", arg), f"{trigger}: {arg!r} is not one token"


# Every match that runs the CLI, including the commented-out ones, pastes its output via
# the clipboard. Espanso's default backend would type output shorter than
# clipboard_threshold (100 chars) key by key instead.
def test_cli_matches_paste_via_clipboard() -> None:
    matches = [m for path in MATCH_FILES for m in _load(path)["matches"]]
    matches += [m for path in MATCH_FILES for m in _commented_matches(path)]
    cli_matches = [m for m in matches if _calls_cli(m)]
    expected = {"-i-", "-ip-", "-if-", "-iok-", "-il-", "-ilm-", "-ic-", "-p-"}
    assert {m["trigger"] for m in cli_matches} >= expected
    # Backstop: every CLI call in the raw text belongs to one of those matches, so none can
    # hide in global_vars or in a commented block the parser above does not recognise.
    calls = sum(
        len(re.findall(r'\["__PROMPT_WORKFLOW__"', p.read_text("utf-8"))) for p in MATCH_FILES
    )
    assert calls == len(cli_matches)
    assert not [p.name for p in MATCH_FILES if "global_vars" in _load(p)]
    for match in cli_matches:
        assert match.get("force_mode") == "clipboard", f"{match['trigger']} must set force_mode"


# No match, commented out or not, sets ignore_error: a crash of the CLI must still fail the
# expansion visibly. Raw text, so a var or a block the parsers miss cannot hide it.
def test_no_match_ignores_errors() -> None:
    for path in MATCH_FILES:
        assert "ignore_error" not in path.read_text("utf-8"), path.name


# Every match that runs the CLI, the commented-out -ic- included, names itself for the usage
# history with one literal --trigger-id: its own trigger without the dashes, from the CLI's
# allowlist. Never a {{form}} value or any other variable, and never shared, so two triggers
# cannot be counted as one.
def test_cli_matches_pass_their_own_trigger_id() -> None:
    matches = [m for path in MATCH_FILES for m in _load(path)["matches"]]
    matches += [m for path in MATCH_FILES for m in _commented_matches(path)]
    seen = set()
    for match in (m for m in matches if _calls_cli(m)):
        trigger = match["trigger"]
        (args,) = _cli_args(match)
        assert args[2] == "--trigger-id", f"{trigger}: --trigger-id right after the subcommand"
        assert not [a for a in args if a.startswith("--trigger-id=")], trigger
        ids = [args[i + 1] for i, arg in enumerate(args) if arg == "--trigger-id"]
        assert ids == [trigger.strip("-")], f"{trigger} must pass --trigger-id {trigger.strip('-')}"
        assert ids[0] in TRIGGER_IDS, f"{trigger}: {ids[0]!r} is not on the allowlist"
        seen.add(ids[0])
    assert seen == set(TRIGGER_IDS)


# Every --profile passed to the CLI is a profile prompt_builder.PROFILES actually defines,
# so deleting or renaming a profile cannot leave a trigger pointing at nothing.
def test_cli_args_use_known_profiles() -> None:
    for trigger, args in _script_args():
        profile = _option(args, "--profile")
        assert profile is None or profile in PROFILES, f"{trigger}: unknown profile {profile!r}"


# Every --provider passed to the CLI is a name make_provider() accepts.
def test_cli_args_use_known_providers() -> None:
    for trigger, args in _script_args():
        provider = _option(args, "--provider")
        assert provider is None or provider in KNOWN_PROVIDERS, f"{trigger}: {provider!r}"


# Every --tier passed to the CLI is one Settings.for_tier() accepts.
def test_cli_args_use_known_tiers() -> None:
    for trigger, args in _script_args():
        tier = _option(args, "--tier")
        assert tier is None or tier in TIERS, f"{trigger} uses unknown tier {tier!r}"


# form: blocks must interpolate at least one {{var}} — otherwise Espanso pops an
# empty dialog instead of expanding text.
def test_form_blocks_interpolate_a_variable() -> None:
    for path in MATCH_FILES:
        for match in _load(path)["matches"]:
            form = match.get("form")
            if form is not None:
                assert re.search(r"\{\{\w+\}\}", form), (
                    f"{match['trigger']} form: has no {{{{var}}}}"
                )


TRIGGER_SHAPE = re.compile(r"^-[a-z0-9]+(-[a-z0-9]+)*-$")


# Every enabled trigger uses the dash-delimited -name- form, not the old leading-colon form.
def test_triggers_use_dash_delimited_form() -> None:
    for path in MATCH_FILES:
        for match in _load(path)["matches"]:
            trigger = match["trigger"]
            assert TRIGGER_SHAPE.match(trigger), f"{trigger!r} in {path.name} is not -name- shaped"


# No enabled trigger occurs inside another. With left_word only a prefix could shadow another
# trigger; the stricter check also holds if a match ever loses left_word, when Espanso
# matches a trigger wherever it appears in the typed text.
def test_no_trigger_is_inside_another() -> None:
    triggers = [match["trigger"] for path in MATCH_FILES for match in _load(path)["matches"]]
    for a in triggers:
        for b in triggers:
            if a != b:
                assert a not in b, f"{a!r} occurs inside {b!r}, which is unreachable"


# Every trigger, including the commented-out ones, fires only at the start of a word. By
# default Espanso expands a trigger anywhere, even inside a word, and code such as
# `a[n-i-1]` or `only-if-cached` contains them. Espanso reads left_word first and falls back
# to word, so `left_word: false` with `word: true` does not count.
def test_triggers_fire_only_at_word_start() -> None:
    matches = [m for path in MATCH_FILES for m in _load(path)["matches"]]
    matches += [m for path in MATCH_FILES for m in _commented_matches(path)]
    assert "-ic-" in {m["trigger"] for m in matches}
    for match in matches:
        assert match.get("left_word", match.get("word")) is True, match["trigger"]


def _form_vars(path: Path) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    for match in _load(path)["matches"]:
        forms = {v["name"]: v["params"] for v in match.get("vars", []) if v.get("type") == "form"}
        if forms:
            yield match, forms


FORM_ARG = re.compile(r"\{\{(\w+)\.(\w+)\}\}")


# Every {{formN.field}} a CLI call uses is a field that form declares, so a renamed field
# cannot leave an unexpanded placeholder in the call, and it is a whole argv item: Espanso
# fills it in and passes it as one argument, never part of another.
def test_form_fields_used_in_cli_args_exist() -> None:
    for path in MATCH_FILES:
        for match, forms in _form_vars(path):
            for args in _cli_args(match):
                for arg in args:
                    if "{{" not in arg:
                        continue
                    found = FORM_ARG.fullmatch(arg)
                    assert found, f"{match['trigger']}: {arg!r} is not one whole form field"
                    form, name = found.groups()
                    assert name in forms[form]["fields"], f"{match['trigger']}: {form}.{name}"
                    assert f"[[{name}]]" in forms[form]["layout"], f"{match['trigger']}: {name}"


# Choice fields filled into a CLI call must be fixed lists whose default is one of the
# values, and each value must be one the CLI accepts, so the popup cannot produce an error
# marker (no free-text field ever reaches the CLI).
def test_form_choices_are_valid_cli_values() -> None:
    checks: dict[str, Callable[[str], object]] = {
        "--model": lambda v: (
            v == KEEP or (split_model_spec(v)[0] and re.fullmatch(r"[\w.\-/:@]+", v))
        ),
        "--effort": lambda v: v in (KEEP, *EFFORTS),
        "--max-tokens": lambda v: v == KEEP or v.isdigit(),
        "--timeout": lambda v: v == KEEP or v.isdigit(),
    }
    for path in MATCH_FILES:
        for match, forms in _form_vars(path):
            (args,) = _cli_args(match)
            pairs = [(args[i - 1], FORM_ARG.fullmatch(a)) for i, a in enumerate(args) if "{{" in a]
            assert pairs, match["trigger"]
            for option, found in pairs:
                assert found, f"{match['trigger']}: {option}"
                form, name = found.groups()
                field = forms[form]["fields"][name]
                assert field.get("type") == "choice", f"{match['trigger']}: {name} not a choice"
                values = [str(v) for v in field["values"]]
                assert str(field["default"]) in values, f"{match['trigger']}: {name} default"
                for value in values:
                    assert checks[option](value), f"{match['trigger']}: {option} {value!r}"


# Every -if- list starts with, and defaults to, `default` (keep the pro-tier setting), as the
# comment above the match and docs/usage.md say, so the popup changes nothing unless a value is
# picked.
# The timeout list stops at 120 s: Espanso blocks every other trigger while a call runs.
def test_if_form_lists_default_to_the_tier_setting() -> None:
    (forms,) = [f for path in MATCH_FILES for m, f in _form_vars(path) if m["trigger"] == "-if-"]
    fields = forms["form1"]["fields"]
    assert set(fields) == {"model", "effort", "maxtokens", "timeout"}
    for name, field in fields.items():
        values = [str(v) for v in field["values"]]
        assert values[0] == KEEP, f"-if-: {name} must list {KEEP} first"
        assert str(field["default"]) == KEEP, f"-if-: {name} must default to {KEEP}"
    assert max(int(v) for v in fields["timeout"]["values"] if str(v) != KEEP) <= 120


# Every match, the commented-out ones included, has its own label: Espanso's search bar shows
# it instead of the replacement text, which for the CLI triggers is only `{{output}}`. Each
# starts with the display name (#169), so a search for it lists them all.
def test_every_match_has_a_distinct_label() -> None:
    matches = [m for path in MATCH_FILES for m in _load(path)["matches"]]
    matches += [m for path in MATCH_FILES for m in _commented_matches(path)]
    labels = [match.get("label") for match in matches]
    for match, label in zip(matches, labels, strict=True):
        assert isinstance(label, str), f"{match['trigger']} has no label"
        assert label.strip(), f"{match['trigger']} has an empty label"
        assert label.startswith("PromptMend: "), f"{match['trigger']}: {label!r}"
    assert len(set(labels)) == len(labels), "two matches share a label"


# Match files are UTF-8 without a BOM: the installers read and write them as such, and
# Espanso would treat a BOM as part of the first key.
def test_match_files_are_utf8_without_bom() -> None:
    for path in ESPANSO_ROOT.rglob("*.yml"):
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{path.name} has a BOM"
        raw.decode("utf-8")
