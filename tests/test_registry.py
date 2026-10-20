"""providers/registry.py: every place that knows a provider reads PROVIDERS, so these hold
for any provider added there, the data-protection rules above all (CLAUDE.md "Data-protection
gate")."""

from __future__ import annotations

from dataclasses import fields, replace
from typing import Any

import pytest

from promptmend import assets, setup_guide, smoke
from promptmend.config import Settings, env_names, secret_names
from promptmend.factory import (
    PROVIDER_KEYS,
    PROVIDER_LABELS,
    check_base_url,
    leaves_machine,
    make_provider,
    routes,
)
from promptmend.gate import GateBlocked, GatedProvider
from promptmend.providers.base import ProviderError
from promptmend.providers.registry import PROVIDERS, ProviderSpec

CLOUD = [spec for spec in PROVIDERS.values() if spec.cloud]
LOCAL = [spec for spec in PROVIDERS.values() if not spec.cloud]
SETTINGS_FIELDS = {f.name for f in fields(Settings)}


def _keyed(cfg: Settings) -> Settings:
    changes: dict[str, Any] = {f"{spec.name}_api_key": "k" for spec in CLOUD}
    return replace(cfg, **changes)


def _url(cfg: Settings, spec: ProviderSpec, url: str) -> Settings:
    changes: dict[str, Any] = {f"{spec.name}_base_url": url}
    return replace(cfg, **changes)


def _ids(specs: list[ProviderSpec]) -> list[str]:
    return [spec.name for spec in specs]


def test_both_kinds_exist() -> None:
    assert CLOUD
    assert LOCAL
    assert all(name == spec.name for name, spec in PROVIDERS.items())


@pytest.mark.parametrize("spec", PROVIDERS.values(), ids=list(PROVIDERS))
def test_spec_names_its_settings(spec: ProviderSpec) -> None:
    assert {f"{spec.name}_base_url", f"{spec.name}_model"} <= SETTINGS_FIELDS
    assert {spec.model_setting, spec.url_setting} <= set(env_names())
    assert PROVIDER_LABELS[spec.name] == spec.label
    assert spec.help
    if spec.cap:
        assert spec.cap in SETTINGS_FIELDS
    route = next(r for r in routes(Settings()) if r.name == spec.name)
    assert (route.model_setting, route.url_setting, route.key) == (
        spec.model_setting,
        spec.url_setting,
        spec.key,
    )


# A cloud API needs its key (a secret), has its own output cap, is never probed as a local
# server, and its key never goes over plain http to anything but loopback.
@pytest.mark.parametrize("spec", CLOUD, ids=_ids(CLOUD))
def test_cloud_spec_needs_a_key_and_https(spec: ProviderSpec) -> None:
    assert spec.key in secret_names()
    assert PROVIDER_KEYS[spec.name] == spec.key
    assert f"{spec.name}_api_key" in SETTINGS_FIELDS
    assert spec.cap
    assert spec.probe_path is None
    with pytest.raises(ProviderError, match=f"^{spec.key} is not configured$"):
        make_provider(spec.name, Settings())
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
    remote = _url(cfg, spec, "https://llm.example.com/v1")
    assert leaves_machine(spec.name, remote)
    assert isinstance(make_provider(spec.name, remote), GatedProvider)


# --model and --max-tokens reach whichever provider runs.
def test_overrides_reach_every_provider() -> None:
    cfg = Settings().with_overrides(model="m", max_tokens="300")
    for spec in PROVIDERS.values():
        assert getattr(cfg, f"{spec.name}_model") == "m"
        if spec.cap:
            assert getattr(cfg, spec.cap) == 300
    assert cfg.call_max_tokens == 300


def test_setup_lists_every_provider_cloud_first() -> None:
    choices = setup_guide.provider_choices(Settings(), assets.triggers())
    assert [c.value for c in choices] == _ids(CLOUD) + _ids(LOCAL)
    assert [c.title for c in choices] == [s.label for s in CLOUD + LOCAL]
    assert all(c.detail.startswith(s.help) for c, s in zip(choices, CLOUD + LOCAL, strict=True))


# The stub runs point every provider at 127.0.0.1 with the placeholder key, never the real one.
def test_stub_points_every_provider_at_the_stub() -> None:
    env = smoke.stub_env(4242, {"OPENROUTER_API_KEY": "real"})
    cfg = smoke.stub_settings(_keyed(Settings()), 4242)
    for spec in PROVIDERS.values():
        assert env[spec.url_setting].startswith("http://127.0.0.1:4242")
        assert getattr(cfg, f"{spec.name}_base_url") == env[spec.url_setting]
        if spec.key:
            assert env[spec.key] == smoke.PLACEHOLDER_KEY
            assert getattr(cfg, f"{spec.name}_api_key") == smoke.PLACEHOLDER_KEY
    assert env["PROMPT_HISTORY"] == "false"
    assert not cfg.history
