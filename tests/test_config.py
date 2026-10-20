import os
from pathlib import Path

import pytest
from conftest import read_env_file

from promptmend import config
from promptmend.config import ConfigLayers, Finding, Settings, env_names, split_model_spec


# Settings() reads env vars at instantiation, not at import time.
def test_settings_read_at_instantiation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPT_PROVIDER", "lmstudio")
    monkeypatch.setenv("PROMPT_TIMEOUT_SECONDS", "12")
    settings = Settings()
    assert settings.provider == "lmstudio"
    assert settings.timeout == 12.0


# Default values apply when the relevant env vars are unset.
def test_defaults() -> None:
    settings = Settings()
    assert settings.provider == "openrouter"
    assert settings.profile == "default"
    assert settings.timeout == 30.0
    assert settings.temperature == 0.2
    assert settings.ollama_base_url == "http://localhost:11434"
    assert settings.ollama_model == "qwen3:8b"
    assert settings.ollama_think is False
    assert settings.openrouter_base_url == "https://openrouter.ai/api/v1"
    assert settings.openrouter_model == "google/gemini-3.5-flash-lite"
    assert settings.openrouter_api_key == ""
    assert settings.openrouter_max_tokens == 2400
    assert settings.openrouter_provider == "google-ai-studio/flex"
    assert settings.openrouter_allow_fallbacks is True
    assert settings.openrouter_reasoning_effort == "minimal"
    assert settings.openrouter_pro_model == "openai/gpt-6-luna"
    assert settings.openrouter_pro_provider == "openai"
    assert settings.openrouter_pro_reasoning_effort == "low"
    assert settings.pro_timeout == 60.0
    assert settings.openrouter_pro_max_tokens is None
    assert settings.lmstudio_base_url == "http://localhost:1234/v1"
    assert settings.lmstudio_model == "local-model"
    assert settings.anthropic_base_url == "https://api.anthropic.com"
    assert settings.anthropic_model == "claude-sonnet-5"
    assert settings.anthropic_api_key == ""
    assert settings.anthropic_max_tokens == 2400
    assert settings.allow_cloud_override is False


# for_tier("pro") swaps in the pro model, endpoint, effort, timeout and profile; standard is
# a no-op.
def test_for_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_PRO_MODEL", "x/pro")
    settings = Settings()
    assert settings.for_tier("standard") is settings
    pro = settings.for_tier("pro")
    assert (pro.openrouter_model, pro.openrouter_provider) == ("x/pro", "openai")
    assert (pro.openrouter_reasoning_effort, pro.timeout) == ("low", 60.0)
    assert pro.persona == settings.persona
    assert pro.profile == settings.profile == "default"  # empty by default: one prompt
    monkeypatch.setenv("PROMPT_PRO_PROFILE", "general")
    assert Settings().for_tier("pro").profile == "general"
    assert Settings().for_tier("standard").profile == settings.profile
    with pytest.raises(ValueError, match="Unknown tier"):
        settings.for_tier("ultra")


# for_call: the tier, then the overrides; a pro call on another model loses the pro profile.
def test_for_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPT_PROFILE", "general")
    settings = Settings()
    assert settings.for_call("standard") == settings
    assert settings.for_call("pro", model="default") == settings.for_tier("pro")
    assert settings.for_call("standard", model="x/m").profile == "general"
    assert settings.for_call("pro") == settings.for_tier("pro")
    monkeypatch.setenv("PROMPT_PRO_PROFILE", "default")
    settings = Settings()
    assert settings.for_call("pro", model=f"{settings.openrouter_pro_model}@auto").profile == (
        "default"
    )
    other = settings.for_call("pro", model="x/m@y", effort="high")
    assert (other.openrouter_model, other.openrouter_provider) == ("x/m", "y")
    assert (other.profile, other.timeout) == ("general", settings.pro_timeout)


# Only OpenRouter has a pro tier: for any other provider for_call("pro") keeps the standard
# profile and timeout, even with PROMPT_PRO_PROFILE set (#31).
@pytest.mark.parametrize("provider", ["ollama", "lmstudio", "anthropic"])
def test_for_call_pro_tier_is_openrouter_only(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setenv("PROMPT_PRO_PROFILE", "general")
    settings = Settings()
    call = settings.for_call("pro", provider=provider)
    assert (call.profile, call.timeout) == (settings.profile, settings.timeout)
    assert settings.for_call("pro", provider=provider, timeout="7").timeout == 7
    pro = settings.for_call("pro", provider="openrouter")
    assert (pro.profile, pro.timeout) == ("general", settings.pro_timeout)
    with pytest.raises(ValueError, match="Unknown tier"):
        settings.for_call("ultra", provider=provider)


# --tier pro and --effort name OpenRouter-only settings; the neutral values pass anywhere.
def test_openrouter_only_options() -> None:
    config.openrouter_only("openrouter", "pro", "high")
    for neutral in ((("standard", None)), ("standard", "default")):
        config.openrouter_only("ollama", *neutral)
    with pytest.raises(
        ValueError, match=r"^--tier pro and --effort apply only to OpenRouter, not 'lmstudio'"
    ):
        config.openrouter_only("lmstudio", "pro", "low")
    with pytest.raises(ValueError, match=r"^--effort applies only to OpenRouter, not 'x y'"):
        config.openrouter_only("x y", "standard", "none")


# --max-tokens is kept for the local providers too, which have no cap setting of their own.
def test_with_overrides_call_max_tokens() -> None:
    settings = Settings()
    assert settings.call_max_tokens is None
    assert settings.with_overrides(max_tokens="300").call_max_tokens == 300
    assert "call_max_tokens" not in [f.metadata.get("env") for f in config.setting_fields()]


# OPENROUTER_PRO_MAX_TOKENS: empty (the default) inherits OPENROUTER_MAX_TOKENS for the pro
# tier; set, it caps the pro tier only (#31).
def test_pro_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_MAX_TOKENS", "1000")
    settings = Settings()
    assert settings.openrouter_pro_max_tokens is None
    assert settings.for_tier("pro").openrouter_max_tokens == 1000
    monkeypatch.setenv("OPENROUTER_PRO_MAX_TOKENS", "")
    assert Settings.load().for_tier("pro").openrouter_max_tokens == 1000
    monkeypatch.setenv("OPENROUTER_PRO_MAX_TOKENS", "4000")
    settings = Settings()
    assert settings.openrouter_pro_max_tokens == 4000
    assert settings.for_tier("pro").openrouter_max_tokens == 4000
    assert settings.for_tier("standard").openrouter_max_tokens == 1000
    assert settings.for_call("pro").openrouter_max_tokens == 4000
    assert settings.for_call("pro", max_tokens="default").openrouter_max_tokens == 4000
    assert settings.for_call("pro", max_tokens="8000").openrouter_max_tokens == 8000
    assert settings.for_call("standard").openrouter_max_tokens == 1000
    # A pro call on another model (the -if- popup) is still the pro tier: it keeps the cap.
    assert settings.for_call("pro", model="x/m@auto").openrouter_max_tokens == 4000
    for provider in ("ollama", "lmstudio", "anthropic"):
        call = settings.for_call("pro", provider=provider)
        assert (call.openrouter_max_tokens, call.anthropic_max_tokens) == (1000, 2400)
        assert call.call_max_tokens is None


@pytest.mark.parametrize("raw", ["0", "-1", "lots", "inf", " "])
def test_bad_pro_max_tokens(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("OPENROUTER_PRO_MAX_TOKENS", raw)
    with pytest.raises(
        ValueError,
        match=f"^OPENROUTER_PRO_MAX_TOKENS must be empty or a whole number above 0, got '{raw}'$",
    ):
        Settings()


# OLLAMA_NUM_CTX (#168): unset or empty keeps Ollama's own context size; the tiers and
# per-call overrides leave it alone.
def test_ollama_num_ctx(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().ollama_num_ctx is None
    monkeypatch.setenv("OLLAMA_NUM_CTX", "")
    assert Settings().ollama_num_ctx is None
    monkeypatch.setenv("OLLAMA_NUM_CTX", "8192")
    settings = Settings()
    assert settings.ollama_num_ctx == 8192
    assert settings.for_tier("pro").ollama_num_ctx == 8192
    assert settings.for_call("pro", provider="ollama").ollama_num_ctx == 8192
    assert settings.with_overrides(max_tokens="300", model="m").ollama_num_ctx == 8192


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "1.5", " "])
def test_bad_num_ctx(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("OLLAMA_NUM_CTX", raw)
    with pytest.raises(
        ValueError,
        match=f"^OLLAMA_NUM_CTX must be empty or a whole number above 0, got '{raw}'$",
    ):
        Settings()


# An empty PROMPT_TEMPERATURE means "send no temperature"; unset keeps the default.
def test_empty_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().temperature == 0.2
    monkeypatch.setenv("PROMPT_TEMPERATURE", "")
    assert Settings().temperature is None
    assert Settings.load().temperature is None
    monkeypatch.setenv("PROMPT_TEMPERATURE", " ")
    with pytest.raises(ValueError, match="PROMPT_TEMPERATURE must be empty or a number"):
        Settings()


# ANTHROPIC_TEMPERATURE is empty by default (Claude 4.7+ rejects any temperature below 1, so
# none is sent), and a set value is taken as is, below 1 included (#273).
def test_anthropic_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().anthropic_temperature is None
    for raw, value in (("1", 1.0), ("0", 0.0), ("0.3", 0.3), ("", None)):
        monkeypatch.setenv("ANTHROPIC_TEMPERATURE", raw)
        assert Settings.load().anthropic_temperature == value
    monkeypatch.setenv("PROMPT_TEMPERATURE", "0.7")
    assert Settings().anthropic_temperature is None


# split_model_spec: a bare slug has no pin, slug@endpoint pins it, @auto unpins.
def test_split_model_spec() -> None:
    assert split_model_spec(None) == (None, None)
    assert split_model_spec("x/m") == ("x/m", None)
    assert split_model_spec("x/m@openai/flex") == ("x/m", "openai/flex")
    assert split_model_spec("x/m@auto") == ("x/m", "")
    for bad in ("x/m@", "@openai"):
        with pytest.raises(ValueError, match="slug@endpoint"):
            split_model_spec(bad)


# with_overrides: None and `default` keep the settings; real values replace them.
def test_with_overrides() -> None:
    settings = Settings()
    assert settings.with_overrides() is settings
    assert settings.with_overrides(effort="default", max_tokens="default") is settings
    custom = settings.with_overrides(effort="high", max_tokens="8000", timeout="120")
    assert custom.openrouter_provider == settings.openrouter_provider
    assert custom.openrouter_reasoning_effort == "high"
    assert (custom.openrouter_max_tokens, custom.anthropic_max_tokens) == (8000, 8000)
    assert custom.timeout == 120.0
    assert custom.openrouter_model == settings.openrouter_model


# --model sets every provider's model, so it applies to whichever one runs; its @endpoint
# part pins OpenRouter (@auto: no pin), and a bare slug keeps the configured pin.
def test_with_overrides_model() -> None:
    settings = Settings()
    pinned = settings.with_overrides(model="x/m@auto")
    models = (
        pinned.ollama_model,
        pinned.lmstudio_model,
        pinned.openrouter_model,
        pinned.anthropic_model,
    )
    assert models == ("x/m",) * 4
    assert pinned.openrouter_provider == ""
    assert settings.with_overrides(model="x/m").openrouter_provider == settings.openrouter_provider
    with pytest.raises(ValueError, match="slug@endpoint"):
        settings.with_overrides(model="x/m@")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"effort": "extreme"}, "--effort must be one of"),
        ({"max_tokens": "lots"}, "--max-tokens must be a whole number above 0 or default"),
        ({"max_tokens": "0"}, "--max-tokens must be a whole number above 0"),
        ({"timeout": "soon"}, "--timeout must be a number above 0 or default"),
        ({"timeout": "-5"}, "--timeout must be a number above 0"),
    ],
)
def test_with_overrides_rejects_bad_values(kwargs: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Settings().with_overrides(**kwargs)


# A bad numeric env var raises a ValueError naming the setting and what it must be,
# instead of Python's opaque "could not convert string to float". Infinite, NaN, zero and
# negative values are refused too: an `inf` timeout would hang Espanso.
@pytest.mark.parametrize(
    ("env_name", "expected"),
    [
        ("PROMPT_TIMEOUT_SECONDS", "a number above 0"),
        ("PROMPT_PRO_TIMEOUT_SECONDS", "a number above 0"),
        ("PROMPT_TEMPERATURE", "empty or a number of 0 or more"),
        ("ANTHROPIC_TEMPERATURE", "empty or a number of 0 or more"),
        ("OPENROUTER_MAX_TOKENS", "a whole number above 0"),
        ("ANTHROPIC_MAX_TOKENS", "a whole number above 0"),
    ],
)
@pytest.mark.parametrize("raw", ["not-a-number", "inf", "nan", "-1"])
def test_bad_numeric_env_var_raises(
    monkeypatch: pytest.MonkeyPatch, env_name: str, expected: str, raw: str
) -> None:
    monkeypatch.setenv(env_name, raw)
    with pytest.raises(ValueError, match=f"^{env_name} must be {expected}, got '{raw}'$"):
        Settings()


# Zero is refused where it makes no sense, and allowed for temperature.
def test_zero_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPT_TEMPERATURE", "0")
    assert Settings().temperature == 0.0
    monkeypatch.setenv("OPENROUTER_MAX_TOKENS", "0")
    with pytest.raises(ValueError, match="OPENROUTER_MAX_TOKENS must be a whole number above 0"):
        Settings()


# API keys never appear in repr(), so a traceback or a failing assertion cannot print them.
def test_repr_hides_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-secret-value")
    text = repr(Settings())
    assert "secret-value" not in text
    assert "openrouter_model=" in text


# PROMPTMEND_ENV names the .env to load; real env vars still win over it.
def test_load_dotenv_explicit_path_does_not_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("PROMPT_PROVIDER=ollama\nOLLAMA_MODEL=from-file\n")
    monkeypatch.setenv("PROMPTMEND_ENV", str(env_file))
    monkeypatch.setenv("PROMPT_PROVIDER", "anthropic")

    settings = Settings.load()

    assert settings.ollama_model == "from-file"
    assert settings.provider == "anthropic"


# .env only sets the settings this package reads: a proxy or CA bundle variable there would
# change how httpx connects, so it is ignored, while real environment variables still apply.
def test_load_dotenv_exports_only_known_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    others = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "FOO")
    for name in others:
        monkeypatch.setenv(name, "x")  # so monkeypatch restores the original state afterwards
        monkeypatch.delenv(name)
    lines = [f"{name}=http://127.0.0.1:9" for name in others]
    (tmp_path / ".env").write_text("\n".join([*lines, "OLLAMA_MODEL=from-file", ""]))

    layers = ConfigLayers.resolve()

    assert layers.settings().ollama_model == "from-file"
    assert not [name for name in others if name in os.environ]
    assert not [name for layer in layers.layers for name in others if name in layer.values]
    assert set(layers.entries) == set(env_names())


def _no_explicit_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project: Path, user: Path
) -> None:
    monkeypatch.delenv("PROMPTMEND_ENV")
    monkeypatch.setattr(config, "_PROJECT_ROOT", project)
    monkeypatch.setattr(config, "_user_config_dir", lambda _environ: user)


# A .env in the current directory or its parents is never loaded: a planted file in an
# untrusted checkout must not redirect the base URL or enable ALLOW_CLOUD_OVERRIDE.
def test_load_dotenv_ignores_cwd_and_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (tmp_path / ".env").write_text("ALLOW_CLOUD_OVERRIDE=true\n")
    (nested / ".env").write_text("ALLOW_CLOUD_OVERRIDE=true\n")
    monkeypatch.chdir(nested)
    _no_explicit_env(tmp_path, monkeypatch, tmp_path / "none", tmp_path / "none")

    layers = ConfigLayers.resolve()

    assert layers.settings().allow_cloud_override is False
    assert layers.entries["ALLOW_CLOUD_OVERRIDE"].source == "default"
    assert [layer.source for layer in layers.layers] == ["default", "env"]


# The repo .env (editable install) wins over the user config dir .env.
def test_load_dotenv_prefers_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, user = tmp_path / "repo", tmp_path / "cfg"
    project.mkdir()
    user.mkdir()
    (project / "pyproject.toml").write_text("")
    (project / ".env").write_text("OLLAMA_MODEL=from-repo\n")
    (user / ".env").write_text("OLLAMA_MODEL=from-user\n")
    _no_explicit_env(tmp_path, monkeypatch, project, user)

    layers = ConfigLayers.resolve()

    assert layers.settings().ollama_model == "from-repo"
    assert layers.entries["OLLAMA_MODEL"].source == f"file:{project / '.env'}"


# Without a project checkout (no pyproject.toml), the user config dir .env is used.
def test_load_dotenv_falls_back_to_user_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, user = tmp_path / "site-packages", tmp_path / "cfg"
    project.mkdir()
    user.mkdir()
    (project / ".env").write_text("OLLAMA_MODEL=from-site-packages\n")
    (user / ".env").write_text("OLLAMA_MODEL=from-user\n")
    _no_explicit_env(tmp_path, monkeypatch, project, user)

    layers = ConfigLayers.resolve()

    assert layers.settings().ollama_model == "from-user"
    assert layers.entries["OLLAMA_MODEL"].source == f"file:{user / '.env'}"


# Comment lines, blank lines, and lines without '=' are skipped; '=' inside a
# value is preserved via partition(); surrounding quotes are stripped.
def test_load_dotenv_parses_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(
        "# a comment\n"
        "\n"
        "not a valid line\n"
        'OPENROUTER_API_KEY="sk-abc=def"\n'
        "LMSTUDIO_MODEL='local-model'\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LMSTUDIO_MODEL", raising=False)

    settings = Settings.load()

    assert settings.openrouter_api_key == "sk-abc=def"
    assert settings.lmstudio_model == "local-model"


# Booleans are "true" or "false" in any case.
@pytest.mark.parametrize(("raw", "expected"), [("TRUE", True), ("true", True), ("False", False)])
def test_bool_parsing(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv("ALLOW_CLOUD_OVERRIDE", raw)
    monkeypatch.setenv("OLLAMA_THINK", raw)
    monkeypatch.setenv("OPENROUTER_ALLOW_FALLBACKS", raw)
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", raw)
    monkeypatch.setenv("PROMPT_GATE_LOCAL", raw)
    settings = Settings()
    assert settings.gate_local is expected
    assert settings.allow_cloud_override is expected
    assert settings.ollama_think is expected
    assert settings.openrouter_allow_fallbacks is expected
    assert settings.local_only is expected


# OPENROUTER_DATA_COLLECTION: empty (the default) or allow/deny in any case, kept lower case.
@pytest.mark.parametrize(
    ("raw", "expected"), [("", ""), ("deny", "deny"), ("DENY", "deny"), ("Allow", "allow")]
)
def test_data_collection_parsing(monkeypatch: pytest.MonkeyPatch, raw: str, expected: str) -> None:
    monkeypatch.setenv("OPENROUTER_DATA_COLLECTION", raw)
    settings = Settings()
    assert settings.openrouter_data_collection == expected
    assert settings.for_tier("pro").openrouter_data_collection == expected


def test_data_collection_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_DATA_COLLECTION", raising=False)
    assert Settings().openrouter_data_collection == ""


# A typo is reported naming the variable, never silently sent (OpenRouter would 400).
@pytest.mark.parametrize("raw", ["no", "true", "denied"])
def test_data_collection_rejects_other_values(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("OPENROUTER_DATA_COLLECTION", raw)
    with pytest.raises(
        ValueError,
        match=f"^OPENROUTER_DATA_COLLECTION must be empty or one of allow, deny, got '{raw}'$",
    ):
        Settings()


# Anything else is an error, not a silent false: OLLAMA_THINK=1 must not mean "off".
@pytest.mark.parametrize("raw", ["1", "yes", ""])
@pytest.mark.parametrize("name", ["OLLAMA_THINK", "PROMPT_GATE_LOCAL"])
def test_bool_parsing_rejects_other_values(
    monkeypatch: pytest.MonkeyPatch, raw: str, name: str
) -> None:
    monkeypatch.setenv(name, raw)
    with pytest.raises(ValueError, match=f"^{name} must be true or false, got '{raw}'$"):
        Settings()


# Both reasoning-effort settings accept empty (omit the field) or a known effort, any case.
@pytest.mark.parametrize(
    "env_name", ["OPENROUTER_REASONING_EFFORT", "OPENROUTER_PRO_REASONING_EFFORT"]
)
@pytest.mark.parametrize(("raw", "expected"), [("", ""), ("low", "low"), ("HIGH", "high")])
def test_effort_parsing(
    monkeypatch: pytest.MonkeyPatch, env_name: str, raw: str, expected: str
) -> None:
    monkeypatch.setenv(env_name, raw)
    assert getattr(Settings(), env_name.lower()) == expected


# A typo is reported at load instead of reaching OpenRouter as an opaque HTTP 400.
@pytest.mark.parametrize(
    "env_name", ["OPENROUTER_REASONING_EFFORT", "OPENROUTER_PRO_REASONING_EFFORT"]
)
def test_effort_typo_is_rejected(monkeypatch: pytest.MonkeyPatch, env_name: str) -> None:
    monkeypatch.setenv(env_name, "hihg")
    with pytest.raises(
        ValueError,
        match=f"^{env_name} must be empty or one of none, minimal, low, medium, high, got 'hihg'$",
    ):
        Settings()


FAKE_KEY = "sk-or-v1-" + "cd" * 32


# A rejected value that may hold a secret is never repeated in the error, which the CLI
# pastes into the focused app. Short harmless values still are (see the tests above).
@pytest.mark.parametrize(
    "raw",
    [
        "2400OPENROUTER_API_KEY=" + FAKE_KEY,
        FAKE_KEY,
        "x" * 41,
    ],
    ids=["merged-line", "key", "long"],
)
def test_parse_error_never_echoes_secret_like_values(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("OPENROUTER_MAX_TOKENS", raw)
    with pytest.raises(ValueError, match=r"^OPENROUTER_MAX_TOKENS must be") as caught:
        Settings()
    message = str(caught.value)
    assert "sk-or-v1" not in message
    assert "cdcd" not in message
    assert "xxxx" not in message
    assert f"<redacted, {len(raw)} chars" in message


# Per-call overrides follow the same rule.
def test_override_error_never_echoes_secret_like_values() -> None:
    with pytest.raises(ValueError, match="redacted") as caught:
        Settings().with_overrides(max_tokens=FAKE_KEY)
    assert "cdcd" not in str(caught.value)


# A .env saved without the newline between two lines is refused by setting name, whatever
# the second line's name, case or spacing.
@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("OPENROUTER_MAX_TOKENS=2400", "OPENROUTER_API_KEY="),
        ("PROMPT_PERSONA=I am a tester.", "OPENROUTER_API_KEY="),
        ("PROMPT_PERSONA=I am a tester.", "OPENROUTER_API_KEY = "),
        ("PROMPT_PERSONA=I am a tester.", "openrouter_api_key="),
        ("PROMPT_PERSONA=I am a tester.", "OPENAI_API_KEY="),
    ],
    ids=["number", "persona", "spaced", "lowercase", "other-name"],
)
def test_load_rejects_merged_env_lines(tmp_path: Path, first: str, second: str) -> None:
    (tmp_path / ".env").write_text(f"{first}{second}{FAKE_KEY}\n")
    key = first.partition("=")[0]
    with pytest.raises(ValueError, match=f"^{key} in .env runs into the next line") as caught:
        Settings.load()
    assert "cdcd" not in str(caught.value)


# The error names an unknown key only through safe_repr: a key line merged in front of it
# would otherwise be repeated.
def test_load_merged_line_error_redacts_unknown_key(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"{FAKE_KEY}PROMPT_PERSONA=x OPENROUTER_MODEL=y\n")
    with pytest.raises(ValueError, match=r"^<redacted, ") as caught:
        Settings.load()
    assert "cdcd" not in str(caught.value)


# User regexes may match assignment-like text, so PROMPT_EXTRA_PATTERNS is not checked.
def test_load_allows_assignments_in_extra_patterns(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("PROMPT_EXTRA_PATTERNS=OPENROUTER_API_KEY=\\S+;DB_PASS\n")
    assert Settings.load().extra_patterns == "OPENROUTER_API_KEY=\\S+;DB_PASS"


# An invalid PROMPT_EXTRA_PATTERNS regex is rejected when settings load, so `config validate`,
# `config set`, doctor and the interface report it, not only a trigger. The message names the
# entry, never the pattern text, which may describe the data it guards.
# A repeat count too large for the engine (OverflowError) or nesting too deep to compile
# (RecursionError) is an invalid entry too, not an unexpected error.
_TOO_BIG = "a{99999999999}"
_TOO_DEEP = "(" * 2_000 + "a" + ")" * 2_000


@pytest.mark.parametrize(
    ("raw", "entry"),
    [("(", 1), ("falcon;secret[", 2), ("a;;(?P<x", 2), (f"ok;{_TOO_BIG}", 2), (_TOO_DEEP, 1)],
    ids=["paren", "bracket", "group", "overflow", "recursion"],
)
def test_invalid_extra_pattern_is_rejected(
    monkeypatch: pytest.MonkeyPatch, raw: str, entry: int
) -> None:
    monkeypatch.setenv("PROMPT_EXTRA_PATTERNS", raw)
    with pytest.raises(ValueError, match=r"^PROMPT_EXTRA_PATTERNS must be") as caught:
        Settings()
    message = str(caught.value)
    assert f"entry {entry} (custom_{entry}) is not" in message
    shown = message.replace(f"(custom_{entry})", "")  # the label's own parenthesis
    for text in (raw, "falcon", "secret"):
        assert text not in shown


@pytest.mark.parametrize(
    "raw", ["ok;(", f"ok;{_TOO_BIG}", _TOO_DEEP], ids=["paren", "overflow", "recursion"]
)
def test_invalid_extra_pattern_is_a_repair_finding(tmp_path: Path, raw: str) -> None:
    (tmp_path / ".env").write_text(f"PROMPT_EXTRA_PATTERNS={raw}\n")
    layers = ConfigLayers.resolve(strict=False)
    assert any("PROMPT_EXTRA_PATTERNS must be" in f.message for f in layers.findings)
    assert layers.settings().extra_patterns == ""


# A space pasted along with an API key is dropped.
def test_api_keys_are_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", f" {FAKE_KEY} ")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key\t")
    settings = Settings()
    assert (settings.openrouter_api_key, settings.anthropic_api_key) == (FAKE_KEY, "ant-key")


# An empty key in .env (as in .env.example) counts as unset for the provider check.
def test_empty_api_key_is_kept_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    assert not Settings().openrouter_api_key


# Shell-style `export KEY=value` lines are accepted, since .env files are often sourced too.
def test_load_dotenv_accepts_export_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("export OLLAMA_MODEL=exported\n")
    monkeypatch.chdir(tmp_path)
    assert Settings.load().ollama_model == "exported"


# Only a matching pair of surrounding quotes is removed; inner or lone quotes survive.
def test_load_dotenv_quote_handling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(
        'OLLAMA_MODEL="it\'s"\nLMSTUDIO_MODEL=\'a"\nPROMPT_PROFILE="general"  \n'
    )
    monkeypatch.chdir(tmp_path)
    settings = Settings.load()
    assert settings.ollama_model == "it's"
    assert settings.lmstudio_model == "'a\""
    assert settings.profile == "general"


# An inline ` # comment` after an unquoted value is not part of the value; a quoted value
# keeps its '#', and a '#' without whitespace before it is data.
def test_load_dotenv_strips_inline_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "OPENROUTER_PROVIDER=openai  # pinned for the pro tier\n"
        'PROMPT_PERSONA="I am #1 here" # note\n'
        "LMSTUDIO_MODEL=model#v2\n"
        "OLLAMA_MODEL=#\n"
    )
    monkeypatch.chdir(tmp_path)
    settings = Settings.load()
    assert settings.openrouter_provider == "openai"
    assert settings.persona == "I am #1 here"
    assert settings.lmstudio_model == "model#v2"
    assert settings.ollama_model == "#"


# PROMPT_PERSONA is empty unless configured.
def test_persona_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().persona == ""
    monkeypatch.setenv("PROMPT_PERSONA", "I am a tester.")
    assert Settings().persona == "I am a tester."


# Loading reads the .env again each time: an edit shows up in the same process, which the
# old os.environ.setdefault() loader hid behind the first load's values.
def test_load_sees_an_edited_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OLLAMA_MODEL=first\n")
    assert Settings.load().ollama_model == "first"
    env_file.write_text("OLLAMA_MODEL=second\nPROMPT_PROVIDER=ollama\n")
    settings = Settings.load()
    assert (settings.ollama_model, settings.provider) == ("second", "ollama")


# Loading never writes the process environment, so child processes inherit nothing from .env.
def test_load_leaves_os_environ_unchanged(tmp_path: Path) -> None:
    lines = [f"{name}=x" for name in ("OLLAMA_MODEL", "LMSTUDIO_MODEL", "PROMPT_PERSONA", "FOO")]
    (tmp_path / ".env").write_text("\n".join([*lines, "OLLAMA_THINK=true", ""]))
    before = dict(os.environ)
    assert Settings.load().ollama_think is True
    ConfigLayers.resolve(strict=False)
    assert dict(os.environ) == before


# Each key reports where its value came from; a real env var shadowing a .env value says so.
def test_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OLLAMA_MODEL=from-file\nPROMPT_PROVIDER=ollama\n")
    monkeypatch.setenv("PROMPT_PROVIDER", "lmstudio")
    monkeypatch.setenv("LMSTUDIO_MODEL", "from-env")

    layers = ConfigLayers.resolve()

    file = f"file:{env_file}"
    assert [layer.source for layer in layers.layers] == ["default", file, "env"]
    got = {
        name: (
            layers.entries[name].value,
            layers.entries[name].source,
            layers.entries[name].shadows,
        )
        for name in ("OLLAMA_MODEL", "PROMPT_PROVIDER", "LMSTUDIO_MODEL", "OPENROUTER_MODEL")
    }
    assert got == {
        "OLLAMA_MODEL": ("from-file", file, ()),
        "PROMPT_PROVIDER": ("lmstudio", "env", (file,)),
        "LMSTUDIO_MODEL": ("from-env", "env", ()),
        "OPENROUTER_MODEL": ("google/gemini-3.5-flash-lite", "default", ()),
    }
    assert layers.findings == ()
    assert layers.settings() == Settings.load()


# Entries and layers never print a value, which may be an API key.
def test_provenance_repr_hides_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    layers = ConfigLayers.resolve()
    assert "cdcd" not in repr(layers)
    assert "cdcd" not in repr(layers.entries["OPENROUTER_API_KEY"])


# A broken .env: strict mode raises today's exact error; repair mode returns the same text as
# findings and falls back to the next lower layer, so management commands can still run.
def test_repair_mode_returns_findings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"PROMPT_PERSONA=I am a tester.OPENROUTER_API_KEY={FAKE_KEY}\n"
        "OPENROUTER_MAX_TOKENS=lots\n"
        "OLLAMA_MODEL=from-file\n"
        "OLLAMA_THINK=1\n"
    )
    monkeypatch.setenv("PROMPT_TIMEOUT_SECONDS", "inf")
    merged = "PROMPT_PERSONA in .env runs into the next line; add the missing newline"
    with pytest.raises(ValueError, match=f"^{merged}$"):
        Settings.load()

    layers = ConfigLayers.resolve(strict=False)

    file = f"file:{env_file}"
    assert layers.findings == (
        Finding(file, merged),
        Finding("env", "PROMPT_TIMEOUT_SECONDS must be a number above 0, got 'inf'"),
        Finding(file, "OLLAMA_THINK must be true or false, got '1'"),
        Finding(file, "OPENROUTER_MAX_TOKENS must be a whole number above 0, got 'lots'"),
    )
    assert "cdcd" not in repr(layers.findings)
    settings = layers.settings()
    assert (settings.persona, settings.timeout, settings.ollama_think) == ("", 30.0, False)
    assert (settings.openrouter_max_tokens, settings.ollama_model) == (2400, "from-file")
    assert layers.entries["PROMPT_TIMEOUT_SECONDS"].source == "default"


# Strict mode reports a bad number with today's message, from the .env as from the env.
def test_strict_bad_number_in_env_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("OPENROUTER_MAX_TOKENS=lots\n")
    message = "^OPENROUTER_MAX_TOKENS must be a whole number above 0, got 'lots'$"
    with pytest.raises(ValueError, match=message):
        Settings.load()


# Repair mode falls back past a bad env value to a valid .env value, not to the default.
def test_repair_mode_falls_back_to_next_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PROMPT_TIMEOUT_SECONDS=12\n")
    monkeypatch.setenv("PROMPT_TIMEOUT_SECONDS", "soon")
    layers = ConfigLayers.resolve(strict=False)
    assert layers.settings().timeout == 12.0
    entry = layers.entries["PROMPT_TIMEOUT_SECONDS"]
    assert (entry.source, entry.shadows, entry.rejected) == (f"file:{env_file}", (), ("env",))
    assert [finding.source for finding in layers.findings] == ["env"]
    assert layers.entries["OLLAMA_MODEL"].rejected == ()


# resolve() takes the environment as a mapping, so a caller can resolve another one.
def test_resolve_takes_an_environment_mapping(tmp_path: Path) -> None:
    env_file = tmp_path / "other.env"
    env_file.write_text("OLLAMA_MODEL=from-file\n")
    environ = {"PROMPTMEND_ENV": str(env_file), "PROMPT_PROVIDER": "ollama"}
    settings = ConfigLayers.resolve(environ).settings()
    assert (settings.provider, settings.ollama_model) == ("ollama", "from-file")


# Repair mode reports a line without '=' by number (never its text); strict mode skips it.
def test_repair_mode_reports_lines_without_equals(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"# note\n\nOLLAMA_MODEL=m\n{FAKE_KEY}\nexport\n")
    assert Settings.load().ollama_model == "m"
    layers = ConfigLayers.resolve(strict=False)
    file = f"file:{env_file}"
    assert layers.findings == (
        Finding(file, "line 4 of .env has no '=' and was ignored"),
        Finding(file, "line 5 of .env has no '=' and was ignored"),
    )
    assert "cdcd" not in repr(layers.findings)
    assert layers.settings().ollama_model == "m"


# A .env candidate that exists but is not UTF-8 is a finding in repair mode, which goes on to
# the next one (strict mode refuses it, see test_strict_mode_refuses_undecodable_env); a
# missing candidate is no finding.
def test_repair_mode_reports_undecodable_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, user = tmp_path / "repo", tmp_path / "cfg"
    project.mkdir()
    user.mkdir()
    (project / "pyproject.toml").write_text("")
    (project / ".env").write_bytes(b"OLLAMA_MODEL=\xff\xfe\n")
    (user / ".env").write_text("OLLAMA_MODEL=from-user\n")
    _no_explicit_env(tmp_path, monkeypatch, project, user)

    layers = ConfigLayers.resolve(strict=False)
    assert layers.findings == (
        Finding(
            f"file:{project / '.env'}",
            "the checkout's .env is not UTF-8 text; save it as UTF-8",
        ),
    )
    assert layers.entries["OLLAMA_MODEL"].source == f"file:{user / '.env'}"

    (user / ".env").unlink()
    assert ConfigLayers.resolve(strict=False).findings == layers.findings


# A .env saved with a UTF-8 byte order mark (Windows Notepad) keeps its first key (#32).
def test_env_with_byte_order_mark(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"\xef\xbb\xbfPROMPT_EXTRA_PATTERNS=CUST-\\d{6}\nOLLAMA_MODEL=m\n")
    assert Settings.load().extra_patterns == r"CUST-\d{6}"
    assert ConfigLayers.resolve(strict=False).findings == ()
    assert read_env_file(path) == {
        "PROMPT_EXTRA_PATTERNS": r"CUST-\d{6}",
        "OLLAMA_MODEL": "m",
    }


# A .env that is not UTF-8 stops strict mode with an error naming the problem, not a value,
# instead of falling through to the next candidate (#32).
def test_strict_mode_refuses_undecodable_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, user = tmp_path / "repo", tmp_path / "cfg"
    project.mkdir()
    user.mkdir()
    (project / "pyproject.toml").write_text("")
    (project / ".env").write_bytes("OLLAMA_MODEL=secret-ish\n".encode("utf-16"))
    (user / ".env").write_text("OLLAMA_MODEL=from-user\n")
    _no_explicit_env(tmp_path, monkeypatch, project, user)
    with pytest.raises(
        ValueError, match=r"^the checkout's \.env is not UTF-8 text; save it as UTF-8$"
    ):
        Settings.load()
    (project / ".env").unlink()
    (user / ".env").write_bytes("OLLAMA_MODEL=x\n".encode("utf-16"))
    with pytest.raises(ValueError, match=r"^the \.env in the user config folder is not UTF-8"):
        Settings.load()


# An unquoted value cut at ` #` keeps dotenv's comment meaning. Repair mode reports the cut
# only for the free-text settings where '#' may be data (a regex, the persona), naming the
# setting only; elsewhere ` # note` is an ordinary comment (#32).
def test_repair_mode_reports_a_value_cut_at_a_comment(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "PROMPT_EXTRA_PATTERNS=ticket #\\d{5};CUST-\\d{6}\n"
        'OPENROUTER_PROVIDER="openai" # quoted, so not cut\n'
        "LMSTUDIO_MODEL=model#v2\n"
        "OLLAMA_MODEL=qwen3:8b  # a note\n"
        "PROMPT_TIMEOUT_SECONDS=45 # seconds\n"
        "PROMPT_PERSONA=I am #1 here\n"
        "NOT_A_SETTING=x # y\n"
    )
    # A cut gate pattern fails closed; repair mode falls back to the default (no patterns).
    with pytest.raises(ValueError, match=r"^the value of PROMPT_EXTRA_PATTERNS was cut"):
        Settings.load()
    layers = ConfigLayers.resolve(strict=False)
    settings = layers.settings()
    assert settings.extra_patterns == ""
    assert settings.persona == "I am"
    assert (settings.ollama_model, settings.timeout) == ("qwen3:8b", 45)
    source = f"file:{tmp_path / '.env'}"
    assert layers.findings == tuple(
        Finding(source, f"the value of {key} was cut at ' #' (a comment); quote the value")
        for key in ("PROMPT_EXTRA_PATTERNS", "PROMPT_PERSONA")
    )


# A rejected value that matches one of the user's PROMPT_EXTRA_PATTERNS is never quoted, even
# in the error raised while the settings load (#32).
def test_rejected_value_matching_a_user_pattern_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROMPT_EXTRA_PATTERNS", "falcon")
    monkeypatch.setenv("PROMPT_TIMEOUT_SECONDS", "falcon")
    with pytest.raises(ValueError, match="PROMPT_TIMEOUT_SECONDS must be") as exc:
        Settings.load()
    assert "falcon" not in str(exc.value)
    assert "<redacted, 6 chars>" in str(exc.value)
    findings = ConfigLayers.resolve(strict=False).findings
    assert findings
    assert all("falcon" not in f.message for f in findings)


# PROMPT_OUTPUT (#134): paste by default, paste or clipboard in any case, anything else names
# the variable.
@pytest.mark.parametrize(("raw", "expected"), [("paste", "paste"), ("CLIPBOARD", "clipboard")])
def test_output_parsing(monkeypatch: pytest.MonkeyPatch, raw: str, expected: str) -> None:
    assert Settings().output == "paste"
    monkeypatch.setenv("PROMPT_OUTPUT", raw)
    assert Settings().output == expected


@pytest.mark.parametrize("raw", ["", "copy", "both"])
def test_output_parsing_rejects_other_values(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("PROMPT_OUTPUT", raw)
    with pytest.raises(
        ValueError, match=f"^PROMPT_OUTPUT must be paste or clipboard, got '{raw}'$"
    ):
        Settings()


# --output beats PROMPT_OUTPUT either way; None and `default` keep it; a bad value is refused.
def test_output_override(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().for_call("standard", output="clipboard").output == "clipboard"
    monkeypatch.setenv("PROMPT_OUTPUT", "clipboard")
    settings = Settings()
    assert settings.for_call("pro", output="paste").output == "paste"
    assert settings.with_overrides(output="default").output == "clipboard"
    assert settings.with_overrides().output == "clipboard"
    with pytest.raises(ValueError, match=r"^--output must be paste or clipboard or default"):
        settings.with_overrides(output="copy")
