"""providers/registry.py: every place that knows a provider reads PROVIDERS, so these hold
for any provider added there, the data-protection rules above all (CLAUDE.md "Data-protection
gate")."""

from __future__ import annotations

import ast
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import pytest

from promptmend import assets, setup_guide, smoke
from promptmend.config import Settings, env_names, secret_names
from promptmend.factory import (
    PROVIDER_KEYS,
    check_base_url,
    leaves_machine,
    make_provider,
)
from promptmend.gate import GateBlocked, GatedProvider
from promptmend.providers.base import ProviderError
from promptmend.providers.registry import PROVIDERS, ProviderSpec

CLOUD = [spec for spec in PROVIDERS.values() if spec.cloud]
LOCAL = [spec for spec in PROVIDERS.values() if not spec.cloud]
SETTINGS_FIELDS = {f.name for f in fields(Settings)}


def _keyed(cfg: Settings) -> Settings:
    changes: dict[str, Any] = {spec.key_field: "k" for spec in CLOUD if spec.key_field}
    return replace(cfg, **changes)


def _url(cfg: Settings, spec: ProviderSpec, url: str) -> Settings:
    changes: dict[str, Any] = {spec.url_field: url}
    return replace(cfg, **changes)


def _ids(specs: list[ProviderSpec]) -> list[str]:
    return [spec.name for spec in specs]


def test_both_kinds_exist() -> None:
    assert CLOUD
    assert LOCAL
    assert all(name == spec.name for name, spec in PROVIDERS.items())


@pytest.mark.parametrize("spec", PROVIDERS.values(), ids=list(PROVIDERS))
def test_spec_names_its_settings(spec: ProviderSpec) -> None:
    assert {spec.url_field, spec.model_field} == {f"{spec.name}_base_url", f"{spec.name}_model"}
    assert {spec.url_field, spec.model_field} <= SETTINGS_FIELDS
    prefix = spec.name.upper()
    assert {f"{prefix}_MODEL", f"{prefix}_BASE_URL"} <= set(env_names())
    assert spec.help
    if spec.cap:
        assert spec.cap in SETTINGS_FIELDS
    if spec.temperature:
        assert spec.temperature in SETTINGS_FIELDS
        assert spec.temperature.upper() in env_names()
    if spec.effort:
        assert spec.effort in SETTINGS_FIELDS
        assert spec.effort.upper() in env_names()
    # The cap's request field: OpenAI's reasoning models reject max_tokens (#274); every other
    # body keeps max_tokens. The other wire formats have no such choice.
    assert spec.cap_field in ("max_tokens", "max_completion_tokens")
    if spec.protocol != "openai":
        assert spec.cap_field == "max_tokens"
        assert spec.effort is None
        assert not spec.router
    # The Anthropic adapter needs a key and an output cap; make_provider() cannot check it.
    # It needs its own temperature too: current Claude models reject PROMPT_TEMPERATURE (#273).
    if spec.protocol == "anthropic":
        assert spec.key
        assert spec.cap
        assert spec.temperature


# A cloud API needs its key (a secret), has its own output cap, is never probed as a local
# server, and its key never goes over plain http to anything but loopback.
@pytest.mark.parametrize("spec", CLOUD, ids=_ids(CLOUD))
def test_cloud_spec_needs_a_key_and_https(spec: ProviderSpec) -> None:
    assert spec.key in secret_names()
    assert PROVIDER_KEYS[spec.name] == spec.key
    assert spec.key_field == f"{spec.name}_api_key"
    assert spec.key_field in SETTINGS_FIELDS
    assert spec.cap
    assert spec.probe_path is None
    # The conftest default is legacy mode (PROMPTMEND_ENV set), where `secrets set` refuses.
    legacy = f"{spec.key} is not set: add it to the .env that PROMPTMEND_ENV names"
    with pytest.raises(ProviderError) as caught:
        make_provider(spec.name, Settings())
    assert str(caught.value) == legacy
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("PROMPTMEND_ENV")
        with pytest.raises(ProviderError) as caught:
            make_provider(spec.name, Settings())
    assert str(caught.value) == (
        f"{spec.key} is not set: run `promptmend secrets set {spec.key}` in a terminal"
    )
    plain = _url(_keyed(Settings()), spec, "http://relay.example")
    with pytest.raises(GateBlocked, match=f"{spec.url_setting} must be an https:// URL"):
        check_base_url(spec.name, plain)
    with pytest.raises(GateBlocked, match=f"{spec.url_setting} must be an https:// URL"):
        make_provider(spec.name, plain)
    check_base_url(spec.name, _url(plain, spec, "http://127.0.0.1/"))


# Always gated, and refused by PROMPT_LOCAL_ONLY, even pointed at a loopback relay.
@pytest.mark.parametrize("url", ["", "http://127.0.0.1:8080"], ids=["default", "loopback"])
@pytest.mark.parametrize("spec", CLOUD, ids=_ids(CLOUD))
def test_cloud_spec_always_leaves_the_machine(spec: ProviderSpec, url: str) -> None:
    cfg = _keyed(Settings())
    if url:
        cfg = _url(cfg, spec, url)
    assert leaves_machine(spec.name, cfg)
    assert isinstance(make_provider(spec.name, cfg), GatedProvider)
    with pytest.raises(GateBlocked, match="PROMPT_LOCAL_ONLY"):
        make_provider(spec.name, replace(cfg, local_only=True))


# A local provider at its default loopback URL stays on this machine, ungated and allowed by
# PROMPT_LOCAL_ONLY; pointed elsewhere it leaves the machine and is gated.
@pytest.mark.parametrize("spec", LOCAL, ids=_ids(LOCAL))
def test_local_spec_stays_local_on_loopback(spec: ProviderSpec) -> None:
    assert spec.key is None
    assert spec.cap is None
    assert spec.probe_path
    cfg = Settings()
    assert not leaves_machine(spec.name, cfg)
    assert not isinstance(make_provider(spec.name, cfg), GatedProvider)
    assert not isinstance(make_provider(spec.name, replace(cfg, local_only=True)), GatedProvider)
    if spec.protocol == "ollama":  # an Ollama cloud model goes to ollama.com
        model: dict[str, Any] = {spec.model_field: "gpt-oss:120b-cloud"}
        assert leaves_machine(spec.name, replace(cfg, **model))
    remote = _url(cfg, spec, "https://llm.example.com/v1")
    assert leaves_machine(spec.name, remote)
    assert isinstance(make_provider(spec.name, remote), GatedProvider)


# OpenRouter alone has the router extras (the pro tier, routing, X-Title, its cost, -iok-).
def test_only_openrouter_is_a_router() -> None:
    assert [spec.name for spec in PROVIDERS.values() if spec.router] == ["openrouter"]
    openai = PROVIDERS["openai"]
    assert (openai.cap_field, openai.effort, openai.router) == (
        "max_completion_tokens",
        "openai_reasoning_effort",
        False,
    )
    assert (openai.temperature, openai.cap) == ("openai_temperature", "openai_max_tokens")


# --model, --effort and --max-tokens reach whichever provider runs (--effort those with one).
def test_overrides_reach_every_provider() -> None:
    cfg = Settings().with_overrides(model="m", effort="medium", max_tokens="300")
    for spec in PROVIDERS.values():
        assert spec.model(cfg) == "m"
        assert spec.max_tokens(cfg) == 300
        if spec.effort:
            assert spec.reasoning_effort(cfg) == "medium"
    assert cfg.call_max_tokens == 300
    assert [s.name for s in PROVIDERS.values() if s.effort] == ["openrouter", "openai"]


def test_setup_lists_every_provider_cloud_first() -> None:
    choices = setup_guide.provider_choices(Settings(), assets.triggers())
    assert [c.value for c in choices] == _ids(CLOUD) + _ids(LOCAL)
    assert [c.title for c in choices] == [s.label for s in CLOUD + LOCAL]
    assert all(c.detail.startswith(s.help) for c, s in zip(choices, CLOUD + LOCAL, strict=True))


# The stub runs point every provider at 127.0.0.1 with the placeholder key, never the real one.
def test_stub_points_every_provider_at_the_stub() -> None:
    env = smoke.stub_env(4242, {"OPENROUTER_API_KEY": "real"})
    cfg = smoke.stub_settings(_keyed(Settings()), 4242)
    # Each keeps its default's path, so the stub sees the paths the real API would.
    assert {spec.url_setting: env[spec.url_setting] for spec in PROVIDERS.values()} == {
        "OLLAMA_BASE_URL": "http://127.0.0.1:4242",
        "LMSTUDIO_BASE_URL": "http://127.0.0.1:4242/v1",
        "OPENROUTER_BASE_URL": "http://127.0.0.1:4242/api/v1",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:4242",
        "OPENAI_BASE_URL": "http://127.0.0.1:4242/v1",
    }
    for spec in PROVIDERS.values():
        assert spec.url(cfg) == env[spec.url_setting]
        if spec.key:
            assert env[spec.key] == smoke.PLACEHOLDER_KEY
            assert spec.api_key(cfg) == smoke.PLACEHOLDER_KEY
    assert env["PROMPT_HISTORY"] == "false"
    assert not cfg.history


def _marked(spec: ProviderSpec) -> dict[str, Any]:
    """A value for each setting ``spec`` names that no default and no other spec has."""
    marks: dict[str, Any] = {spec.url_field: "https://marked.example", spec.model_field: "marked"}
    if spec.key_field:
        marks[spec.key_field] = "marked-key"
    if spec.cap:
        marks[spec.cap] = 777
    if spec.temperature:
        marks[spec.temperature] = 0.77
    if spec.effort:
        marks[spec.effort] = "high"
    return marks


# Each typed reader reads exactly the Settings field named beside it (and only that one), and a
# setting the spec lacks falls back as the factory always did: no key is None, no cap is
# --max-tokens, no temperature is PROMPT_TEMPERATURE, no effort is empty. So a typo in a name
# or a reader cannot hide behind the other.
@pytest.mark.parametrize("spec", PROVIDERS.values(), ids=list(PROVIDERS))
def test_accessors_read_the_settings_they_name(spec: ProviderSpec) -> None:
    marks = _marked(spec)
    assert set(marks) <= SETTINGS_FIELDS
    # The shared fallbacks get values of their own, so a reader that fell back to them by
    # mistake would show it.
    cfg = replace(Settings(), **marks, call_max_tokens=55, temperature=0.11)
    assert spec.url(cfg) == "https://marked.example"
    assert spec.model(cfg) == "marked"
    assert (spec.read_key is None) == (spec.key is None) == (spec.key_field is None)
    assert spec.api_key(cfg) == ("marked-key" if spec.key else None)
    assert (spec.read_cap is None) == (spec.cap is None)
    assert spec.max_tokens(cfg) == (777 if spec.cap else 55)
    assert (spec.read_temperature is None) == (spec.temperature is None)
    assert spec.request_temperature(cfg) == (0.77 if spec.temperature else 0.11)
    assert (spec.read_effort is None) == (spec.effort is None)
    assert spec.reasoning_effort(cfg) == ("high" if spec.effort else "")
    # Every other spec still reads its own defaults from these settings.
    for other in PROVIDERS.values():
        if other is not spec:
            assert other.url(cfg) == other.url(Settings())
            assert other.model(cfg) == other.model(Settings())
            assert other.api_key(cfg) == other.api_key(Settings())
            if other.cap:
                assert other.max_tokens(cfg) == other.max_tokens(Settings())
            if other.temperature:
                assert other.request_temperature(cfg) == other.request_temperature(Settings())
            if other.effort:
                assert other.reasoning_effort(cfg) == other.reasoning_effort(Settings())


@pytest.mark.parametrize("spec", PROVIDERS.values(), ids=list(PROVIDERS))
def test_accessors_without_overrides_follow_the_fallbacks(spec: ProviderSpec) -> None:
    cfg = Settings()
    assert spec.max_tokens(cfg) == (getattr(cfg, spec.cap) if spec.cap else None)
    assert spec.request_temperature(cfg) == getattr(cfg, spec.temperature or "temperature")
    assert spec.api_key(cfg) == ("" if spec.key else None)


# The provider a spec builds is named and labelled by that spec, whatever its wire format.
@pytest.mark.parametrize("spec", PROVIDERS.values(), ids=list(PROVIDERS))
def test_provider_takes_name_and_label_from_its_spec(spec: ProviderSpec) -> None:
    built = make_provider(spec.name, _keyed(Settings()))
    inner: Any = built._inner if isinstance(built, GatedProvider) else built
    assert (inner.name, inner.label) == (spec.name, spec.label)


# The getattr calls in src/ whose attribute name is not a string literal, each with why it
# cannot be a typed access: (path below src/, the call as ast.unparse() prints it).
DYNAMIC_GETATTR = {
    # A lazily imported management command: the attribute comes from cli._LAZY_COMMANDS.
    ("promptmend/cli.py", "getattr(module, attr)"),
    # pyperclip's paste or copy, imported only once a run touches the clipboard (#223).
    ("promptmend/pipeline.py", "getattr(pyperclip, op)"),
    # A pane class imported on first use: (module, class) come from the static tui/app._PANES.
    ("promptmend/tui/app.py", "getattr(importlib.import_module(f'.{module}', __package__), name)"),
}


def _dynamic_getattrs(src: Path) -> set[tuple[str, str]]:
    found = set()
    for path in sorted(src.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            literal = (
                isinstance(node, ast.Call)
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and not literal
            ):
                found.add((path.relative_to(src).as_posix(), ast.unparse(node)))
    return found


# A provider's settings are read through its spec's typed accessors, never by building a field
# name for getattr, which mypy cannot check (#319): every getattr whose name is not a string
# literal is one of the allowlisted ones above, and each of those still exists.
def test_no_settings_field_name_is_built_for_getattr() -> None:
    assert _dynamic_getattrs(Path(__file__).resolve().parents[1] / "src") == DYNAMIC_GETATTR


# The Messages API requires max_tokens, so an anthropic spec without its own cap is refused
# rather than sent without one (the real spec has one: test_spec_names_its_settings).
def test_anthropic_spec_without_a_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    capless = replace(PROVIDERS["anthropic"], cap=None, read_cap=None)
    monkeypatch.setitem(PROVIDERS, "anthropic", capless)
    with pytest.raises(ProviderError, match=r"^Anthropic has no output cap setting$"):
        make_provider("anthropic", _keyed(Settings()))
