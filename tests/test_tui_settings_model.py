"""The Settings tab's model (#199), without the interface: every setting listed once, every
choice one the CLI accepts, the dot rules, and the persona never shown."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from promptmend import config, config_store
from promptmend.config import ConfigLayers, env_names, secret_names
from promptmend.prompt_builder import PROFILES, UserProfile
from promptmend.tui import settings_model as model
from promptmend.tui.state import State

PERSONA = "I am the head of a secret project"


def _state(environ: dict[str, str], profiles: list[UserProfile] | None = None) -> State:
    layers = ConfigLayers.resolve(environ, strict=False)
    return State(
        layers=layers,
        settings=layers.settings(),
        report=None,  # type: ignore[arg-type]  # rows() reads only the layers and profiles
        triggers=[],
        plan=None,
        plan_error=None,
        profiles=profiles or [],
        group_by="trigger",
        stats=[],
        stats_error=None,
    )


def test_every_setting_is_listed_exactly_once() -> None:
    names = [meta.name for meta in model.SETTINGS]
    assert sorted(names) == sorted(env_names())
    assert len(names) == len(set(names))
    assert {m.name for m in model.SETTINGS if m.kind == model.KEY} == set(secret_names())
    # Every group has a row; the list shows the basic rows by group, then those behind the
    # Advanced fold by group (#389), the groups in GROUPS' order each time.
    assert {m.group for m in model.SETTINGS} == set(model.GROUPS)
    order = [(m.advanced, model.GROUPS.index(m.group)) for m in model.SETTINGS]
    assert sorted(order) == order
    assert model.ADVANCED_COUNT == 29
    assert all(m.help and "\n" not in m.help for m in model.SETTINGS)


# The rows the list shows with the Advanced fold closed (#389), in order: a setting moved
# into or out of the fold must change this on purpose.
BASIC = (
    ("Triggers", "OPENROUTER_MODEL"),
    ("Triggers", "OPENROUTER_PRO_MODEL"),
    ("Triggers", "PROMPT_PRO_PROVIDER"),
    ("Triggers", "OPENROUTER_API_KEY"),
    ("Output", "PROMPT_OUTPUT"),
    ("Output", "PROMPT_PROFILE"),
    ("Output", "PROMPT_PERSONA"),
    ("Output", "PROMPT_TIMEOUT_SECONDS"),
    ("Privacy", "PROMPT_LOCAL_ONLY"),
    ("Privacy", "PROMPT_GATE_LOCAL"),
    ("Privacy", "ALLOW_CLOUD_OVERRIDE"),
    ("Privacy", "PROMPT_UPDATE_CHECK"),
    ("History", "PROMPT_HISTORY"),
    ("Anthropic", "ANTHROPIC_API_KEY"),
    ("Anthropic", "ANTHROPIC_MODEL"),
    ("OpenAI", "OPENAI_API_KEY"),
    ("OpenAI", "OPENAI_MODEL"),
    ("Ollama", "OLLAMA_MODEL"),
    ("LM Studio", "LMSTUDIO_MODEL"),
)


# The rows behind the fold, in order.
ADVANCED = (
    *(("Output", n) for n in ("PROMPT_TEMPERATURE", "PROMPT_PROVIDER", "PROMPT_PRO_PROFILE")),
    ("Output", "PROMPT_PROFILE_OVERRIDES"),
    ("Output", "PROMPT_PRO_TIMEOUT_SECONDS"),
    ("Privacy", "PROMPT_EXTRA_PATTERNS"),
    ("Privacy", "PROMPT_GATE_PACKS"),
    ("Privacy", "OPENROUTER_DATA_COLLECTION"),
    ("History", "PROMPT_HISTORY_RETENTION_DAYS"),
    ("OpenRouter", "OPENROUTER_BASE_URL"),
    ("OpenRouter", "OPENROUTER_PROVIDER"),
    ("OpenRouter", "OPENROUTER_REASONING_EFFORT"),
    ("OpenRouter", "OPENROUTER_MAX_TOKENS"),
    ("OpenRouter", "OPENROUTER_ALLOW_FALLBACKS"),
    ("OpenRouter", "OPENROUTER_PRO_PROVIDER"),
    ("OpenRouter", "OPENROUTER_PRO_REASONING_EFFORT"),
    ("OpenRouter", "OPENROUTER_PRO_MAX_TOKENS"),
    ("Anthropic", "ANTHROPIC_BASE_URL"),
    ("Anthropic", "ANTHROPIC_MAX_TOKENS"),
    ("Anthropic", "ANTHROPIC_EFFORT"),
    ("Anthropic", "ANTHROPIC_TEMPERATURE"),
    ("OpenAI", "OPENAI_BASE_URL"),
    ("OpenAI", "OPENAI_REASONING_EFFORT"),
    ("OpenAI", "OPENAI_MAX_TOKENS"),
    ("OpenAI", "OPENAI_TEMPERATURE"),
    ("Ollama", "OLLAMA_BASE_URL"),
    ("Ollama", "OLLAMA_THINK"),
    ("Ollama", "OLLAMA_NUM_CTX"),
    ("LM Studio", "LMSTUDIO_BASE_URL"),
)


def test_the_basic_and_advanced_settings_are_pinned() -> None:
    basic = tuple((m.group, m.name) for m in model.SETTINGS if not m.advanced)
    assert basic == BASIC
    assert tuple((m.group, m.name) for m in model.SETTINGS if m.advanced) == ADVANCED
    # Every key is a basic row: a key is never behind the fold.
    assert all(not m.advanced for m in model.SETTINGS if m.kind == model.KEY)
    assert model.GROUPS == (
        "Triggers",
        "Output",
        "Privacy",
        "History",
        "OpenRouter",
        "Anthropic",
        "OpenAI",
        "Ollama",
        "LM Studio",
    )


@pytest.mark.parametrize("meta", model.SETTINGS, ids=lambda m: m.name.lower())
def test_every_choice_and_default_parses(meta: model.Meta) -> None:
    profiles = [UserProfile("mine", Path("mine.md"), "added")]
    for choice in model.choices(meta, _state({}, profiles)):
        config_store.toml_value(meta.name, choice)
    if meta.kind == model.BOOL:
        assert model.default(meta.name) in ("true", "false")
    if meta.kind == model.CHOICE:
        assert model.choices(meta, None)
    config_store.toml_value(meta.name, model.default(meta.name))


def test_profile_choices_are_the_builtins_and_your_added_ones() -> None:
    profiles = [
        UserProfile("mine", Path("mine.md"), "added"),
        UserProfile("default", Path("default.md"), "shadowed"),
    ]
    state = _state({}, profiles)
    assert model.choices(model.BY_NAME["PROMPT_PROFILE"], state) == (*PROFILES, "mine")
    assert model.choices(model.BY_NAME["PROMPT_PRO_PROFILE"], state) == ("", *PROFILES, "mine")
    assert model.choices(model.BY_NAME["PROMPT_PROFILE"], None) == tuple(PROFILES)
    assert model.choices(model.BY_NAME["PROMPT_OUTPUT"], state) == config.OUTPUTS


def test_a_bad_value_is_refused_naming_the_setting() -> None:
    with pytest.raises(ValueError, match="PROMPT_HISTORY_RETENTION_DAYS must be"):
        config_store.toml_value("PROMPT_HISTORY_RETENTION_DAYS", "0")


def test_dot_rules() -> None:
    environ = {
        "PROMPT_OUTPUT": "clipboard",
        "PROMPT_LOCAL_ONLY": "false",
        "PROMPT_HISTORY": "false",
        "OPENROUTER_API_KEY": "-".join(("some", "key")),
        "PROMPT_PERSONA": PERSONA,
    }
    rows = {r.name: r for r in model.rows(_state(environ))}

    def seen(name: str) -> tuple[str, bool, str]:
        return rows[name].value, rows[name].custom, rows[name].source

    # The dot says only "set by you", for every kind of row (#388): a switch on by default
    # is hollow; one set to off, or to its own default, is filled.
    assert seen("PROMPT_UPDATE_CHECK") == ("on", False, "default")
    assert seen("ALLOW_CLOUD_OVERRIDE") == ("off", False, "default")
    assert seen("PROMPT_HISTORY") == ("off", True, "environment")
    assert seen("PROMPT_LOCAL_ONLY") == ("off", True, "environment")
    # A value: filled when set somewhere, hollow at its built-in default.
    assert seen("PROMPT_OUTPUT") == ("clipboard", True, "environment")
    assert seen("PROMPT_PROVIDER") == ("openrouter", False, "default")
    assert seen("PROMPT_EXTRA_PATTERNS") == ("none", False, "default")
    # Keys: set or not set, never the value; an unset key is hollow and has no source.
    assert seen("OPENROUTER_API_KEY") == ("set", True, "environment")
    assert seen("ANTHROPIC_API_KEY") == ("not set", False, "")
    assert rows["ANTHROPIC_API_KEY"].where == ""
    # The persona: set, hidden.
    assert seen("PROMPT_PERSONA") == ("<set, hidden>", True, "environment")
    assert all(PERSONA not in r.value for r in rows.values())


def test_a_switch_set_off_in_a_file_is_custom(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PROMPT_HISTORY=false\nOPENAI_API_KEY=\n", encoding="utf-8")
    rows = {r.name: r for r in model.rows(_state({"PROMPTMEND_ENV": str(env_file)}))}
    assert (rows["PROMPT_HISTORY"].value, rows["PROMPT_HISTORY"].custom) == ("off", True)
    assert rows["PROMPT_HISTORY"].source == ".env"
    # A key set empty is not set: hollow, with no source.
    key = rows["OPENAI_API_KEY"]
    assert (key.value, key.custom, key.source, key.where) == ("not set", False, "", "")


def test_a_value_from_a_file_shows_its_file_name(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PROMPT_PROFILE=general\n", encoding="utf-8")
    state = _state({"PROMPTMEND_ENV": str(env_file)})
    row = next(r for r in model.rows(state) if r.name == "PROMPT_PROFILE")
    assert (row.source, row.custom) == (".env", True)
    assert row.where == str(env_file)
    assert model.source_name("file:/x/config.toml") == "config.toml"


def test_a_value_set_to_its_default_is_still_custom() -> None:
    environ = {"PROMPT_PROVIDER": "openrouter", "PROMPT_UPDATE_CHECK": "true"}
    rows = {r.name: r for r in model.rows(_state(environ))}
    assert rows["PROMPT_PROVIDER"].custom
    assert rows["PROMPT_UPDATE_CHECK"].custom


def test_the_switch_state_is_the_value_not_the_dot() -> None:
    environ = {"PROMPT_HISTORY": "false", "PROMPT_LOCAL_ONLY": "TRUE"}
    rows = {r.name: r for r in model.rows(_state(environ))}
    assert rows["PROMPT_UPDATE_CHECK"].switched_on  # on, at its default: hollow
    assert not rows["PROMPT_HISTORY"].switched_on  # off, set by you: filled
    assert rows["PROMPT_LOCAL_ONLY"].switched_on
    assert not rows["ALLOW_CLOUD_OVERRIDE"].switched_on
    assert not any(r.switched_on for r in rows.values() if r.meta.kind != model.BOOL)


@pytest.mark.parametrize("meta", model.SETTINGS, ids=lambda m: m.name.lower())
def test_an_empty_value_shows_its_empty_word(meta: model.Meta) -> None:
    """#388: an empty value says what it does, never "(empty)"; on a choice list the word is
    never a real choice (EFFORTS holds the value `none`)."""
    if meta.kind == model.CHOICE:
        assert meta.empty not in model.choices(meta, None)
    assert meta.empty
    assert meta.empty != "(empty)"
    try:
        config_store.toml_value(meta.name, "")
    except ValueError:
        return  # an empty value is refused (a switch, a number)
    row = next(r for r in model.rows(_state({meta.name: ""})) if r.name == meta.name)
    assert row.value == ("not set" if meta.kind == model.KEY else meta.empty)
    if not model.default(meta.name) and meta.kind != model.KEY:
        at_default = next(r for r in model.rows(_state({})) if r.name == meta.name)
        assert at_default.value == meta.empty


def test_the_empty_words() -> None:
    words = {m.name: m.empty for m in model.SETTINGS if m.empty != "none"}
    efforts = ("OPENROUTER", "OPENROUTER_PRO", "OPENAI")
    assert words == {
        **{f"{p}_REASONING_EFFORT": "not sent" for p in efforts},
        "ANTHROPIC_EFFORT": "not sent",
        **{f"{p}_TEMPERATURE": "not sent" for p in ("PROMPT", "ANTHROPIC", "OPENAI")},
        "OPENROUTER_DATA_COLLECTION": "not sent",
        "OPENROUTER_PROVIDER": "any endpoint",
        "OPENROUTER_PRO_PROVIDER": "any endpoint",
        "OPENROUTER_PRO_MAX_TOKENS": "as standard",
        "PROMPT_PRO_PROFILE": "as PROMPT_PROFILE",
        "PROMPT_PRO_PROVIDER": "as openrouter",
        "OLLAMA_NUM_CTX": "Ollama's own",
        "PROMPT_GATE_PACKS": "off",
        # Empty is accepted but the next call fails: the row must not look valid.
        **dict.fromkeys(("PROMPT_PROVIDER", "PROMPT_PROFILE"), "empty"),
        **{f"{p}_MODEL": "empty" for p in ("OPENROUTER", "OPENROUTER_PRO", "OLLAMA")},
        **{f"{p}_MODEL": "empty" for p in ("LMSTUDIO", "ANTHROPIC", "OPENAI")},
    }


def test_the_filter_matches_name_and_group_in_any_case() -> None:
    state = _state({})
    assert [r.name for r in model.rows(state, "HiSt")] == [
        "PROMPT_HISTORY",
        "PROMPT_HISTORY_RETENTION_DAYS",
    ]
    privacy = model.rows(state, "privacy")
    assert {r.meta.group for r in privacy} == {"Privacy"}
    # Basic and advanced rows alike, in the list's order (#389).
    assert [r.meta.advanced for r in privacy] == [False] * 4 + [True] * 3
    assert {r.meta.group for r in model.rows(state, "OpenRouter")} >= {"Triggers", "OpenRouter"}
    # The old Keys group is gone: "keys" matches no group and no name.
    assert model.rows(state, "keys") == []
    assert model.rows(state, "  ") == model.rows(state)
    assert model.rows(state, "nothing-like-this") == []


def test_rows_are_frozen() -> None:
    row = model.rows(_state({}))[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.value = "x"  # type: ignore[misc]


def test_the_docs_counts_match_the_model() -> None:
    """docs/interface.md names the fold's and the count's numbers: they follow the model."""
    text = (Path(__file__).resolve().parents[1] / "docs" / "interface.md").read_text("utf-8")
    total = len(model.SETTINGS)
    basic = total - model.ADVANCED_COUNT
    assert f"{model.ADVANCED_COUNT} more settings" in text
    assert f"{basic} of {total} settings" in text
    assert f"{total} settings" in text
