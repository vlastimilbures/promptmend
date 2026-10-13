"""Saved settings: config.toml, the secret store, and the .env migration (#84).

Every file lives under tmp_path: conftest points HOME, XDG_CONFIG_HOME, APPDATA and the
editable-install root there, and these tests unset PROMPTMEND_ENV only through
``saved_mode``.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from promptmend import config, config_files, config_store
from promptmend.cli import PERSONA_PLACEHOLDER, app
from promptmend.config import ConfigLayers, Settings, env_names, secret_names
from promptmend.config_files import FileSecretStore, SecretStoreError
from promptmend.config_store import (
    ConfigStoreError,
    MigrationError,
    ReadOnlyConfigError,
    StaleEditError,
)

if TYPE_CHECKING:
    from conftest import StubProvider

REPO = Path(__file__).resolve().parents[1]
runner = CliRunner()

# Built at runtime so secret scanners never see a key-shaped literal in the source, and named
# without "secret" so CodeQL does not read the fixture files written below as clear-text storage.
OPENROUTER_VALUE = "-".join(["sk", "or", "v1", "5e17e1" * 10])
ANTHROPIC_VALUE = "-".join(["sk", "ant", "api03", "c0ffee" * 10])


@pytest.fixture
def saved_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Leave legacy mode (PROMPTMEND_ENV unset); returns the per-test user config dir."""
    monkeypatch.delenv("PROMPTMEND_ENV")
    directory = config._user_config_dir(os.environ)
    assert tmp_path in directory.parents
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """The editable-install checkout (conftest points _PROJECT_ROOT at it)."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text("")
    return root


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _tree(*roots: Path, skip: Path) -> dict[str, bytes]:
    """Every file under ``roots`` (outside ``skip``) with its bytes."""
    return {
        str(p): p.read_bytes()
        for root in roots
        for p in sorted(root.rglob("*"))
        if p.is_file() and skip not in p.parents
    }


# --- reading config.toml and secrets.toml --------------------------------------------------


# Precedence: default < config.toml < secret store < real environment.
def test_saved_layers_precedence(saved_mode: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(
        saved_mode / "config.toml",
        'config_version = 1\nOPENROUTER_MODEL = "x/saved"\nOLLAMA_THINK = true\n'
        'OPENROUTER_MAX_TOKENS = 900\nPROMPT_TEMPERATURE = 0.5\nOLLAMA_MODEL = "saved"\n',
    )
    FileSecretStore(saved_mode / "secrets.toml").set("OPENROUTER_API_KEY", OPENROUTER_VALUE)
    monkeypatch.setenv("OLLAMA_MODEL", "from-env")

    layers = ConfigLayers.resolve()
    settings = layers.settings()

    assert [layer.source for layer in layers.layers] == [
        "default",
        f"file:{saved_mode / 'config.toml'}",
        f"file:{saved_mode / 'secrets.toml'}",
        "env",
    ]
    assert settings.openrouter_model == "x/saved"
    assert settings.ollama_think is True
    assert (settings.openrouter_max_tokens, settings.temperature) == (900, 0.5)
    assert settings.openrouter_api_key == OPENROUTER_VALUE
    assert settings.ollama_model == "from-env"
    assert layers.entries["OLLAMA_MODEL"].shadows == (f"file:{saved_mode / 'config.toml'}",)


# Once config.toml exists, no .env is read: a lingering one never shadows a saved value.
# Repair mode says it is ignored; strict mode (the triggers) stays silent.
def test_lingering_env_never_shadows_saved_toml(saved_mode: Path, project: Path) -> None:
    _write(saved_mode / "config.toml", 'OPENROUTER_MODEL = "x/saved"\n')
    _write(project / ".env", "OPENROUTER_MODEL=x/old\nOLLAMA_MODEL=old\n")
    _write(saved_mode / ".env", "OPENROUTER_MODEL=x/older\n")

    assert Settings.load().openrouter_model == "x/saved"
    assert Settings.load().ollama_model == "qwen3:8b"
    repair = ConfigLayers.resolve(strict=False)
    assert {f.source for f in repair.findings} == {
        f"file:{project / '.env'}",
        f"file:{saved_mode / '.env'}",
    }
    assert all("ignored" in f.message for f in repair.findings)
    assert ConfigLayers.resolve().findings == ()


# Without config.toml everything is as before, except that a secret in the secret store wins
# over the .env's.
def test_without_toml_env_still_applies_and_store_beats_it(saved_mode: Path, project: Path) -> None:
    _write(
        project / ".env", f"OLLAMA_MODEL=from-env-file\nOPENROUTER_API_KEY={OPENROUTER_VALUE}x\n"
    )
    assert Settings.load().ollama_model == "from-env-file"
    FileSecretStore(saved_mode / "secrets.toml").set("OPENROUTER_API_KEY", OPENROUTER_VALUE)
    assert Settings.load().openrouter_api_key == OPENROUTER_VALUE


# Legacy mode: the PROMPTMEND_ENV file alone, exactly as before.
def test_legacy_mode_ignores_saved_files(tmp_path: Path) -> None:
    directory = config._user_config_dir(os.environ)
    _write(directory / "config.toml", 'OLLAMA_MODEL = "saved"\n')
    FileSecretStore(directory / "secrets.toml").set("OPENROUTER_API_KEY", OPENROUTER_VALUE)
    _write(tmp_path / ".env", "OLLAMA_MODEL=legacy\n")

    layers = ConfigLayers.resolve()

    assert layers.settings().ollama_model == "legacy"
    assert layers.settings().openrouter_api_key == ""
    assert [layer.source for layer in layers.layers] == [
        "default",
        f"file:{tmp_path / '.env'}",
        "env",
    ]


# A broken config.toml fails closed (strict raises with the position only, no content); the
# .env does not step in. Repair mode reports it and falls back to the defaults.
def test_invalid_toml_fails_closed(saved_mode: Path, project: Path) -> None:
    _write(
        saved_mode / "config.toml",
        f'OPENROUTER_MODEL = "x\nOPENROUTER_API_KEY = "{OPENROUTER_VALUE}"\n',
    )
    _write(project / ".env", "OPENROUTER_MODEL=x/env\n")

    with pytest.raises(ValueError, match=r"config.toml is not valid TOML \(at line 1") as exc:
        ConfigLayers.resolve()
    assert OPENROUTER_VALUE not in str(exc.value)
    repair = ConfigLayers.resolve(strict=False)
    assert repair.settings().openrouter_model == "google/gemini-3.5-flash-lite"
    assert "not valid TOML" in repair.findings[0].message


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("OLLAMA_THINK = 1\n", "OLLAMA_THINK must be true or false"),
        ("OLLAMA_MODEL = [1]\n", "OLLAMA_MODEL in config.toml must be text, a number"),
        ("config_version = 'one'\n", "config_version in config.toml must be a whole number"),
        ("config_version = true\n", "config_version in config.toml must be a whole number"),
    ],
    ids=["bad-value", "array", "text-version", "bool-version"],
)
def test_invalid_toml_values_raise(saved_mode: Path, text: str, error: str) -> None:
    _write(saved_mode / "config.toml", text)
    with pytest.raises(ValueError, match=error):
        ConfigLayers.resolve()
    assert ConfigLayers.resolve(strict=False).findings


# Unknown keys and a newer config_version are read but noted in repair mode only; a secret
# in config.toml is used, and repair mode says to move it.
def test_toml_notes(saved_mode: Path) -> None:
    _write(
        saved_mode / "config.toml",
        'config_version = 9\nFOO = 1\nOLLAMA_MODEL = "m"\n'
        f'OPENROUTER_API_KEY = "{OPENROUTER_VALUE}"\n',
    )
    assert ConfigLayers.resolve().findings == ()
    repair = ConfigLayers.resolve(strict=False)
    messages = " | ".join(f.message for f in repair.findings)
    assert "config_version 9, newer than this version reads (1)" in messages
    assert "'FOO' in config.toml is not a setting" in messages
    assert "OPENROUTER_API_KEY is a secret; move it" in messages
    assert OPENROUTER_VALUE not in messages
    assert repair.settings().ollama_model == "m"


# The secret store only contributes secrets; anything else in it is ignored.
def test_secret_store_holds_only_secrets(saved_mode: Path) -> None:
    _write(
        saved_mode / "secrets.toml",
        f'OLLAMA_MODEL = "x"\nOPENROUTER_API_KEY = "{OPENROUTER_VALUE}"\n',
    )
    settings = Settings.load()
    assert (settings.ollama_model, settings.openrouter_api_key) == ("qwen3:8b", OPENROUTER_VALUE)
    repair = ConfigLayers.resolve(strict=False)
    assert "'OLLAMA_MODEL' is not a secret setting" in repair.findings[0].message


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_exposed_secrets_file_is_reported(saved_mode: Path) -> None:
    path = _write(saved_mode / "secrets.toml", f'OPENROUTER_API_KEY = "{OPENROUTER_VALUE}"\n')
    path.chmod(0o644)
    repair = ConfigLayers.resolve(strict=False)
    assert "can be read by other users" in repair.findings[0].message
    assert ConfigLayers.resolve().findings == ()


# --- fail closed ---------------------------------------------------------------------------


# An invalid config makes improve print its marker without building a provider; persona still
# prints a persona that an unrelated bad value does not affect (#32).
def test_invalid_config_fails_closed_in_cli(saved_mode: Path, stub_provider: StubProvider) -> None:
    _write(saved_mode / "config.toml", "OPENROUTER_MAX_TOKENS = 0\n")
    result = runner.invoke(app, ["improve", "--source", "argument", "--text", "draft"])
    assert result.exit_code == 0
    assert result.stdout == (
        "[promptmend: OPENROUTER_MAX_TOKENS must be a whole number above 0, got '0']"
    )
    assert stub_provider.built == []
    _write(saved_mode / "config.toml", 'PROMPT_PERSONA = "I am x."\nOLLAMA_THINK = "y"\n')
    assert runner.invoke(app, ["persona"]).stdout == "I am x."
    _write(saved_mode / "config.toml", 'PROMPT_PERSONA = "I am x."\nOLLAMA_THINK = \n')
    assert runner.invoke(app, ["persona"]).stdout == PERSONA_PLACEHOLDER


# A secret store that fails (a future keyring) fails the call: the marker, no provider and no
# fallback to a plaintext file.
def test_store_error_never_falls_back(
    saved_mode: Path, monkeypatch: pytest.MonkeyPatch, stub_provider: StubProvider
) -> None:
    class Broken:
        source = "keyring:test"

        def read(self) -> None:
            raise SecretStoreError("keyring is locked")

        def set(self, name: str, value: str) -> None:
            raise SecretStoreError("keyring is locked")

        def delete(self, name: str) -> None:
            raise SecretStoreError("keyring is locked")

    monkeypatch.setattr(config_files, "secret_store", lambda _directory: Broken())
    result = runner.invoke(app, ["improve", "--source", "argument", "--text", "draft"])
    assert result.stdout == "[promptmend: keyring is locked]"
    assert stub_provider.built == []
    with pytest.raises(SecretStoreError):
        config_store.save_secret("OPENROUTER_API_KEY", OPENROUTER_VALUE)
    assert not list(saved_mode.rglob("*"))


# The trigger path never imports the TOML writer, even with saved files present.
def test_trigger_path_does_not_import_writer(saved_mode: Path, tmp_path: Path) -> None:
    _write(saved_mode / "config.toml", 'OLLAMA_MODEL = "m"\n')
    FileSecretStore(saved_mode / "secrets.toml").set("OPENROUTER_API_KEY", OPENROUTER_VALUE)
    code = (
        "import sys, promptmend.cli as cli; cli.Settings.load(); "
        "print(sorted({'tomli_w', 'tomlkit', 'keyring'} & sys.modules.keys()))"
    )
    env = {k: v for k, v in os.environ.items() if k != "PROMPTMEND_ENV"}
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"


# --- saving config.toml --------------------------------------------------------------------


def test_save_settings_round_trip(saved_mode: Path) -> None:
    snapshot = config_store.read_settings()
    assert (snapshot.digest, snapshot.values) == (None, {})

    saved = config_store.save_settings(
        snapshot,
        {"OPENROUTER_MODEL": "x/m", "OLLAMA_THINK": "TRUE", "PROMPT_TIMEOUT_SECONDS": "12"},
    )

    text = (saved_mode / "config.toml").read_text("utf-8")
    assert text.startswith("config_version = 1\n")
    assert "OLLAMA_THINK = true\n" in text
    assert "PROMPT_TIMEOUT_SECONDS = 12.0\n" in text
    assert saved.values["OPENROUTER_MODEL"] == "x/m"
    settings = Settings.load()
    assert (settings.openrouter_model, settings.ollama_think, settings.timeout) == (
        "x/m",
        True,
        12.0,
    )
    # None removes a setting (its default applies again); unknown keys survive a rewrite.
    _write(saved.path, saved.path.read_text("utf-8") + "FUTURE_KEY = 'kept'\n")
    saved = config_store.save_settings(config_store.read_settings(), {"OLLAMA_THINK": None})
    assert "OLLAMA_THINK" not in saved.table
    assert saved.table["FUTURE_KEY"] == "kept"
    assert [p.name for p in saved_mode.iterdir()] == ["config.toml"]  # no temp file left


# An empty value is saved as "" (PROMPT_TEMPERATURE: omit the temperature), never taken for
# None, which removes the setting so its default applies again.
def test_save_settings_empty_value(saved_mode: Path) -> None:
    saved = config_store.save_settings(config_store.read_settings(), {"PROMPT_TEMPERATURE": ""})
    assert saved.table["PROMPT_TEMPERATURE"] == ""
    assert Settings.load().temperature is None
    saved = config_store.save_settings(saved, {"PROMPT_TEMPERATURE": None})
    assert "PROMPT_TEMPERATURE" not in saved.table
    assert Settings.load().temperature == 0.2


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"OPENROUTER_API_KEY": OPENROUTER_VALUE}, "OPENROUTER_API_KEY is a secret"),
        ({"OLLAMA_THINK": "1"}, "OLLAMA_THINK must be true or false"),
        ({"NOT_A_SETTING": "x"}, "'NOT_A_SETTING' is not a setting"),
        ({"OPENROUTER_MAX_TOKENS": OPENROUTER_VALUE}, "redacted"),
    ],
    ids=["secret", "bad-bool", "unknown", "secret-as-value"],
)
def test_save_settings_validates_first(
    saved_mode: Path, changes: dict[str, str], error: str
) -> None:
    with pytest.raises(ValueError, match=error) as exc:
        config_store.save_settings(config_store.read_settings(), changes)
    assert OPENROUTER_VALUE not in str(exc.value)
    assert not (saved_mode / "config.toml").exists()


# A concurrent edit is refused and reported by setting name, never by value.
def test_concurrent_edit_is_refused_with_diff(saved_mode: Path) -> None:
    path = _write(saved_mode / "config.toml", 'OLLAMA_MODEL = "a"\nLMSTUDIO_MODEL = "b"\n')
    snapshot = config_store.read_settings()
    _write(path, 'OLLAMA_MODEL = "changed"\nLMSTUDIO_MODEL = "b"\nPROMPT_PROFILE = "general"\n')
    edited = path.read_bytes()

    with pytest.raises(StaleEditError) as exc:
        config_store.save_settings(snapshot, {"OPENROUTER_MODEL": "x/m"})

    assert exc.value.changed == ("OLLAMA_MODEL", "PROMPT_PROFILE")
    assert "changed since it was read (OLLAMA_MODEL, PROMPT_PROFILE)" in str(exc.value)
    assert "general" not in str(exc.value)
    assert path.read_bytes() == edited
    snapshot = config_store.read_settings()
    _write(path, edited.decode() + "# a comment\n")
    with pytest.raises(StaleEditError, match="comments or layout only") as exc:
        config_store.save_settings(snapshot, {"OPENROUTER_MODEL": "x/m"})
    assert exc.value.changed == ()


# A newer config_version is read-only; a secret already in config.toml blocks a rewrite.
def test_save_settings_refusals(saved_mode: Path) -> None:
    path = _write(saved_mode / "config.toml", "config_version = 2\n")
    with pytest.raises(ReadOnlyConfigError, match="unknown config_version"):
        config_store.save_settings(config_store.read_settings(), {"OLLAMA_MODEL": "m"})
    _write(path, f'OPENROUTER_API_KEY = "{OPENROUTER_VALUE}"\n')
    with pytest.raises(ConfigStoreError, match=r"OPENROUTER_API_KEY in config\.toml is a secret"):
        config_store.save_settings(config_store.read_settings(), {"OLLAMA_MODEL": "m"})


# --- the secret store ----------------------------------------------------------------------


def test_save_and_delete_secret(saved_mode: Path) -> None:
    config_store.save_secret("OPENROUTER_API_KEY", f"  {OPENROUTER_VALUE}\n")
    config_store.save_secret("ANTHROPIC_API_KEY", ANTHROPIC_VALUE)
    assert config_store.saved_secret_names() == ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY")
    assert Settings.load().openrouter_api_key == OPENROUTER_VALUE
    assert not (saved_mode / "config.toml").exists()

    config_store.delete_secret("ANTHROPIC_API_KEY")
    config_store.delete_secret("ANTHROPIC_API_KEY")  # already gone: no error
    assert config_store.saved_secret_names() == ("OPENROUTER_API_KEY",)
    with pytest.raises(ValueError, match="'OLLAMA_MODEL' is not a secret setting"):
        config_store.save_secret("OLLAMA_MODEL", "x")
    with pytest.raises(ValueError, match="one line of visible characters") as exc:
        config_store.save_secret("OPENROUTER_API_KEY", f"{OPENROUTER_VALUE}\nOLLAMA_MODEL=x")
    assert OPENROUTER_VALUE not in str(exc.value)


def _link(link: Path, target: Path | str) -> None:
    try:
        link.symlink_to(target)
    except OSError:  # Windows without the symlink privilege
        pytest.skip("cannot create symlinks here")


def _dotfiles(saved_mode: Path, tmp_path: Path, *, relative: bool = False) -> Path:
    """Link config.toml and secrets.toml to a dotfiles folder holding neither (dangling)."""
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    for name in ("config.toml", "secrets.toml"):
        target = dotfiles / name
        _link(saved_mode / name, os.path.relpath(target, saved_mode) if relative else target)
    return dotfiles


# A config.toml or secrets.toml kept as a symlink (into a dotfiles repo) is written through
# to its target; the link stays a link (#236). A dangling link creates its target, and a
# relative link resolves from the link's folder.
@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative"])
def test_saving_writes_through_a_symlink(saved_mode: Path, tmp_path: Path, relative: bool) -> None:
    dotfiles = _dotfiles(saved_mode, tmp_path, relative=relative)
    (dotfiles / "config.toml").write_text("config_version = 1\n", "utf-8")

    config_store.save_settings(config_store.read_settings(), {"OPENROUTER_MODEL": "x/m"})
    config_store.save_secret("OPENROUTER_API_KEY", OPENROUTER_VALUE)

    assert (saved_mode / "config.toml").is_symlink()
    assert (saved_mode / "secrets.toml").is_symlink()
    assert 'OPENROUTER_MODEL = "x/m"' in (dotfiles / "config.toml").read_text("utf-8")
    if os.name != "nt":
        assert stat.S_IMODE((dotfiles / "secrets.toml").stat().st_mode) == 0o600
    settings = Settings.load()
    assert (settings.openrouter_model, settings.openrouter_api_key) == ("x/m", OPENROUTER_VALUE)
    assert sorted(p.name for p in dotfiles.iterdir()) == ["config.toml", "secrets.toml"]


# A link into a missing folder fails (no folder tree is created elsewhere), and a link to a
# folder is refused by name, not with a raw error from the rename.
def test_saving_through_a_bad_symlink_fails(saved_mode: Path, tmp_path: Path) -> None:
    _link(saved_mode / "secrets.toml", tmp_path / "missing" / "secrets.toml")
    with pytest.raises(SecretStoreError, match=r"could not write secrets\.toml"):
        config_store.save_secret("OPENROUTER_API_KEY", OPENROUTER_VALUE)
    assert not (tmp_path / "missing").exists()

    (tmp_path / "folder").mkdir()
    (saved_mode / "secrets.toml").unlink()
    _link(saved_mode / "secrets.toml", tmp_path / "folder")
    with pytest.raises(SecretStoreError, match="it is a folder, not a file"):
        config_store.save_secret("OPENROUTER_API_KEY", OPENROUTER_VALUE)
    assert list((tmp_path / "folder").iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_secrets_file_is_mode_600_from_creation(saved_mode: Path) -> None:
    old = os.umask(0)  # even with no umask, the file is never group or world readable
    try:
        config_store.save_secret("OPENROUTER_API_KEY", OPENROUTER_VALUE)
    finally:
        os.umask(old)
    assert stat.S_IMODE((saved_mode / "secrets.toml").stat().st_mode) == 0o600


def _icacls_principals(path: Path) -> list[str]:
    """The principals in the file's ACL as icacls (an independent tool) lists them."""
    icacls = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "icacls.exe"
    out = subprocess.run([str(icacls), str(path)], capture_output=True, timeout=30, check=True)
    lines = out.stdout.decode("utf-8", errors="replace").splitlines()
    aces = [lines[0][len(str(path)) :], *lines[1:]]
    entries = []
    for ace in aces:
        if not ace.strip():
            break
        principal, _, perms = ace.strip().rpartition(":")
        assert "(I)" not in perms, f"inherited entry: {ace}"
        entries.append(principal)
    return entries


# Exactly one ACE, for the current user: not SYSTEM, Administrators, OWNER RIGHTS or anything
# inherited, even though Python creates the config dir with mode 0o700 (a protected DACL of
# its own on Windows).
@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL check")
def test_secrets_file_is_private_to_the_user_on_windows(saved_mode: Path) -> None:
    config_store.save_secret("OPENROUTER_API_KEY", OPENROUTER_VALUE)
    config_store.save_secret("ANTHROPIC_API_KEY", ANTHROPIC_VALUE)  # a rewrite keeps it private
    path = saved_mode / "secrets.toml"
    sid = config_files.current_user_sid()
    dacl = config_files._read_dacl(path)
    alias = config_files._canonical_dacl(f"D:P(A;;FA;;;{sid})").rpartition(";")[2].rstrip(")")
    assert config_files.only_user(dacl, (sid, alias)), dacl
    assert dacl.count("(") == 1, dacl
    whoami = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "whoami.exe"
    me = subprocess.run([str(whoami)], capture_output=True, timeout=30, check=True).stdout
    principals = _icacls_principals(path)
    assert [p.lower() for p in principals] == [me.decode("utf-8", "replace").strip().lower()]
    assert Settings.load().anthropic_api_key == ANTHROPIC_VALUE


SID = "S-1-5-21-1-2-3-1001"


@pytest.mark.parametrize(
    ("sddl", "private"),
    [
        (f"D:P(A;;FA;;;{SID})", True),
        ("D:P(A;;FA;;;LA)", True),
        ("D:P(A;;FA;;;LA)(A;;FA;;;BA)", False),
        (f"D:PAI(A;;FA;;;{SID})", True),
        (f"D:P(A;;FA;;;{SID})(A;;FA;;;SY)", False),
        (f"D:P(A;;FA;;;{SID})(A;;FA;;;BA)(A;;FA;;;OW)", False),
        (f"D:(A;;FA;;;{SID})", False),
        (f"D:P(D;;FA;;;{SID})", False),
        ("D:P", False),
        ("D:NO_ACCESS_CONTROL", False),
    ],
    ids=[
        "user",
        "alias",
        "alias-and-admins",
        "auto-inherited-flag",
        "system",
        "python-0o700",
        "unprotected",
        "deny",
        "empty",
        "null-dacl",
    ],
)
def test_only_user(sddl: str, private: bool) -> None:
    assert config_files.only_user(sddl, (SID, "LA")) is private


# restrict_to_user sets a protected single-ACE DACL, reads it back, and refuses anything
# else; write_atomic then removes its temporary file, so a secret never lands in a file open
# to other accounts and nothing falls back to plaintext.
@pytest.mark.parametrize(
    ("read_back", "error"),
    [
        (f"D:P(A;;FA;;;{SID})(A;;FA;;;SY)", "still open to other accounts"),
        (OSError(5, "denied"), "could not make .* private to your user"),
    ],
    ids=["leftover-ace", "os-error"],
)
def test_restrict_to_user_refuses_leftovers(
    saved_mode: Path, monkeypatch: pytest.MonkeyPatch, read_back: str | OSError, error: str
) -> None:
    applied: list[str] = []

    def read_dacl(path: Path) -> str:
        if isinstance(read_back, Exception):
            raise read_back
        return read_back

    monkeypatch.setattr(config_files, "_WINDOWS", True)
    monkeypatch.setattr(config_files, "current_user_sid", lambda: SID)
    monkeypatch.setattr(config_files, "_set_dacl", lambda path, sddl: applied.append(sddl))
    monkeypatch.setattr(config_files, "_read_dacl", read_dacl)
    monkeypatch.setattr(config_files, "_canonical_dacl", lambda sddl: sddl)
    with pytest.raises(SecretStoreError, match=error):
        config_store.save_secret("OPENROUTER_API_KEY", OPENROUTER_VALUE)
    assert applied == [f"D:P(A;;FA;;;{SID})"]
    assert list(saved_mode.iterdir()) == []

    monkeypatch.setattr(config_files, "_read_dacl", lambda path: f"D:P(A;;FA;;;{SID})")
    config_store.save_secret("OPENROUTER_API_KEY", OPENROUTER_VALUE)
    assert Settings.load().openrouter_api_key == OPENROUTER_VALUE


# --- migrating .env ------------------------------------------------------------------------


def _example_env() -> str:
    return (
        f"OPENROUTER_API_KEY={OPENROUTER_VALUE}\nPROMPT_PERSONA=I am a tester.\n"
        "OPENROUTER_MODEL=google/gemini-3.5-flash-lite\nOPENROUTER_MAX_TOKENS=900\n"
        "PROMPT_TIMEOUT_SECONDS=30.0\nHTTPS_PROXY=http://127.0.0.1:9\n"
    )


def test_migration_preview_apply_and_idempotence(saved_mode: Path, project: Path) -> None:
    repo_env = _write(project / ".env", _example_env())
    user_env = _write(saved_mode / ".env", "OLLAMA_MODEL=never-active\n")
    original = repo_env.read_bytes()
    before = Settings.load()

    plan = config_store.plan_migration()

    assert plan.status == "ready"
    assert [(s.path, s.active) for s in plan.sources] == [(repo_env, True), (user_env, False)]
    assert dict(plan.settings) == {
        "PROMPT_PERSONA": "I am a tester.",
        "OPENROUTER_MAX_TOKENS": "900",
    }
    assert plan.secrets == ("OPENROUTER_API_KEY",)
    assert plan.defaults == ("OPENROUTER_MODEL", "PROMPT_TIMEOUT_SECONDS")
    assert plan.ignored == ("'HTTPS_PROXY'",)
    preview = "\n".join(plan.describe())
    assert "To config.toml: PROMPT_PERSONA, OPENROUTER_MAX_TOKENS" in preview
    assert "To the secret store: OPENROUTER_API_KEY" in preview

    # Consent is the preview's token; anything else is refused and changes nothing.
    for consent in ("", "yes", plan.token[::-1]):
        with pytest.raises(MigrationError, match="not confirmed"):
            config_store.apply_migration(consent=consent)
    assert repo_env.read_bytes() == original
    assert not (saved_mode / "config.toml").exists()

    result = config_store.apply_migration(consent=plan.token)

    assert Settings.load() == before
    assert not repo_env.exists()
    assert not user_env.exists()
    assert result.moved[repo_env].read_bytes() == original
    assert result.moved[repo_env].parent == result.backup
    assert result.left == ()
    assert config_store.saved_secret_names() == ("OPENROUTER_API_KEY",)
    # The copied defaults were not saved, so a later default change reaches this user.
    assert "OPENROUTER_MODEL" not in (saved_mode / "config.toml").read_text("utf-8")
    # The marker stops a silent repeat.
    assert config_store.plan_migration().status == "migrated"
    with pytest.raises(MigrationError, match="already migrated"):
        config_store.apply_migration(consent=plan.token)
    # An old .env dropped back in never shadows the saved values.
    _write(repo_env, "PROMPT_PERSONA=old\nOPENROUTER_MAX_TOKENS=1\n")
    assert Settings.load() == before


def test_rollback_restores_exact_state(saved_mode: Path, project: Path) -> None:
    _write(project / ".env", _example_env())
    _write(saved_mode / ".env", "OLLAMA_MODEL=never-active\n")
    FileSecretStore(saved_mode / "secrets.toml").set("ANTHROPIC_API_KEY", ANTHROPIC_VALUE)
    backups = saved_mode / "backups"
    snapshot = _tree(project, saved_mode, skip=backups)
    before = Settings.load()

    config_store.apply_migration(consent=config_store.plan_migration().token)
    assert _tree(project, saved_mode, skip=backups) != snapshot
    rollback = config_store.plan_rollback()
    assert len(rollback.restores) == 2
    with pytest.raises(MigrationError, match="not confirmed"):
        config_store.apply_rollback(consent="")
    config_store.apply_rollback(consent=rollback.token)

    assert _tree(project, saved_mode, skip=backups) == snapshot
    assert Settings.load() == before
    assert not (saved_mode / "migration.json").exists()
    # Nothing was deleted: the files the migration wrote were moved into the backup.
    kept = {p.name.rsplit("-", 1)[-1] for p in backups.rglob("rolled-back-*")}
    assert kept == {"config.toml", "secrets.toml"}
    # After a rollback, migrating again works.
    assert config_store.plan_migration().status == "ready"
    with pytest.raises(MigrationError, match="no migration to roll back"):
        config_store.plan_rollback()


# Across file systems the .env is renamed in place instead (still never deleted); rollback
# finds it there.
def test_migration_falls_back_to_rename_in_place(
    saved_mode: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_env = _write(project / ".env", "OLLAMA_MODEL=m\n")
    original = repo_env.read_bytes()
    real_move = config_store._move
    # Moving into the backup fails (another file system); a rename within project works.
    monkeypatch.setattr(
        config_store,
        "_move",
        lambda s, t: real_move(s, t) if t.parent == project or s.parent != project else False,
    )

    result = config_store.apply_migration(consent=config_store.plan_migration().token)

    inactive = result.moved[repo_env]
    assert inactive.parent == project
    assert inactive.name.startswith(".env.inactive-")
    assert inactive.read_bytes() == original
    record = json.loads((saved_mode / "migration.json").read_text("utf-8"))
    assert record["sources"][0]["in_place"] == str(inactive)
    monkeypatch.setattr(config_store, "_move", real_move)
    config_store.apply_rollback(consent=config_store.plan_rollback().token)
    assert repo_env.read_bytes() == original


def test_migration_unmovable_env_is_left_and_ignored(
    saved_mode: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_env = _write(project / ".env", "OLLAMA_MODEL=m\n")
    real_move = config_store._move
    monkeypatch.setattr(config_store, "_move", lambda s, t: False)
    result = config_store.apply_migration(consent=config_store.plan_migration().token)
    assert result.left == (repo_env,)
    assert result.moved == {}
    assert Settings.load().ollama_model == "m"
    assert any("ignored" in f.message for f in ConfigLayers.resolve(strict=False).findings)
    monkeypatch.setattr(config_store, "_move", real_move)
    config_store.apply_rollback(consent=config_store.plan_rollback().token)
    assert repo_env.exists()
    assert not (saved_mode / "config.toml").exists()


# A failed verification undoes the writes: no config.toml, the old secrets.toml bytes, the
# .env in place, no marker.
def test_failed_verification_undoes_everything(
    saved_mode: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_env = _write(project / ".env", _example_env())
    secrets = _write(saved_mode / "secrets.toml", f'ANTHROPIC_API_KEY = "{ANTHROPIC_VALUE}"\n')
    old_secrets = secrets.read_bytes()
    plan = config_store.plan_migration()
    real = config_store._effective
    calls = []

    def effective(environ: Mapping[str, str]) -> dict[str, tuple[Any, str]]:
        calls.append(1)
        result = real(environ)
        if len(calls) == 2:
            result["OLLAMA_MODEL"] = ("other", "default")
        return result

    monkeypatch.setattr(config_store, "_effective", effective)
    with pytest.raises(
        MigrationError, match="nothing was migrated: reloading changed OLLAMA_MODEL"
    ):
        config_store.apply_migration(consent=plan.token)

    assert repo_env.exists()
    assert secrets.read_bytes() == old_secrets
    assert not (saved_mode / "config.toml").exists()
    assert not (saved_mode / "migration.json").exists()


# A failed migration through dangling config.toml/secrets.toml links removes the files it
# created at their targets (one with the key in it) and keeps the links (#236).
def test_failed_migration_through_symlinks_cleans_targets(
    saved_mode: Path, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotfiles = _dotfiles(saved_mode, tmp_path)
    _write(project / ".env", _example_env())
    plan = config_store.plan_migration()
    real = config_store._effective
    calls = []

    def effective(environ: Mapping[str, str]) -> dict[str, tuple[Any, str]]:
        calls.append(1)
        result = real(environ)
        if len(calls) == 2:
            result["OLLAMA_MODEL"] = ("other", "default")
        return result

    monkeypatch.setattr(config_store, "_effective", effective)
    with pytest.raises(MigrationError, match="nothing was migrated"):
        config_store.apply_migration(consent=plan.token)

    assert list(dotfiles.iterdir()) == []
    assert (saved_mode / "config.toml").is_symlink()
    assert (saved_mode / "secrets.toml").is_symlink()


# A rollback copies a linked config.toml/secrets.toml into the backup (a regular file),
# keeps the links and writes secrets.toml's exact old bytes through its link (#236).
def test_rollback_keeps_symlinks(saved_mode: Path, project: Path, tmp_path: Path) -> None:
    dotfiles = _dotfiles(saved_mode, tmp_path)
    old = f'ANTHROPIC_API_KEY = "{ANTHROPIC_VALUE}"\n'.encode()
    (dotfiles / "secrets.toml").write_bytes(old)
    _write(project / ".env", _example_env())
    config_store.apply_migration(consent=config_store.plan_migration().token)
    assert (dotfiles / "config.toml").exists()

    config_store.apply_rollback(consent=config_store.plan_rollback().token)

    assert (saved_mode / "config.toml").is_symlink()
    assert (saved_mode / "secrets.toml").is_symlink()
    assert sorted(p.name for p in dotfiles.iterdir()) == ["secrets.toml"]
    assert (dotfiles / "secrets.toml").read_bytes() == old
    kept = list((saved_mode / "backups").rglob("rolled-back-*"))
    assert {p.name.rsplit("-", 1)[-1] for p in kept} == {"config.toml", "secrets.toml"}
    assert all(p.is_file() and not p.is_symlink() for p in kept)
    assert (project / ".env").exists()


# A store that fails mid-migration also undoes the writes.
def test_migration_store_failure_undoes(
    saved_mode: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(project / ".env", _example_env())
    plan = config_store.plan_migration()

    def broken(self: FileSecretStore, name: str, value: str) -> None:
        raise SecretStoreError("disk full")

    monkeypatch.setattr(FileSecretStore, "set", broken)
    with pytest.raises(MigrationError, match="nothing was migrated: disk full"):
        config_store.apply_migration(consent=plan.token)
    assert not (saved_mode / "config.toml").exists()
    assert (project / ".env").exists()


@pytest.mark.parametrize(
    ("env_text", "error"),
    [
        ("OLLAMA_THINK=maybe\n", "fix the current settings first: OLLAMA_THINK must be"),
        (b"\xff\xfe", "is not UTF-8 text"),
    ],
    ids=["invalid-value", "not-utf8"],
)
def test_migration_refuses_invalid_env(
    saved_mode: Path, project: Path, env_text: str | bytes, error: str
) -> None:
    path = project / ".env"
    path.write_bytes(env_text if isinstance(env_text, bytes) else env_text.encode())
    with pytest.raises(MigrationError, match=error):
        config_store.plan_migration()


# A bad .env value hidden by a real environment variable is still caught before migrating.
def test_migration_refuses_shadowed_invalid_value(
    saved_mode: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(project / ".env", "OLLAMA_THINK=maybe\n")
    monkeypatch.setenv("OLLAMA_THINK", "true")
    with pytest.raises(MigrationError, match=r"fix the \.env first: OLLAMA_THINK must be"):
        config_store.plan_migration()


def test_migration_statuses(
    saved_mode: Path, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert config_store.plan_migration().status == "nothing"
    assert "nothing to migrate" in config_store.plan_migration().describe()[0]
    _write(saved_mode / "config.toml", "")
    _write(project / ".env", "OLLAMA_MODEL=m\n")
    plan = config_store.plan_migration()
    assert plan.status == "saved"
    with pytest.raises(MigrationError, match="already saved"):
        config_store.apply_migration(consent=plan.token)
    monkeypatch.setenv("PROMPTMEND_ENV", str(tmp_path / ".env"))
    with pytest.raises(MigrationError, match="PROMPTMEND_ENV is set"):
        config_store.plan_migration()


# A secret already in the store wins today, so the .env's is not migrated over it.
def test_migration_keeps_stored_secret(saved_mode: Path, project: Path) -> None:
    _write(project / ".env", f"OPENROUTER_API_KEY={OPENROUTER_VALUE}x\n")
    FileSecretStore(saved_mode / "secrets.toml").set("OPENROUTER_API_KEY", OPENROUTER_VALUE)
    plan = config_store.plan_migration()
    assert plan.secrets == ()
    config_store.apply_migration(consent=plan.token)
    assert Settings.load().openrouter_api_key == OPENROUTER_VALUE


def test_rollback_refusals(saved_mode: Path, project: Path) -> None:
    repo_env = _write(project / ".env", "OLLAMA_MODEL=m\n")
    result = config_store.apply_migration(consent=config_store.plan_migration().token)
    _write(repo_env, "OLLAMA_MODEL=new\n")
    with pytest.raises(MigrationError, match="exists again"):
        config_store.plan_rollback()
    repo_env.unlink()
    _write(result.moved[repo_env], "tampered\n")
    with pytest.raises(MigrationError, match="changed since the migration"):
        config_store.plan_rollback()
    result.moved[repo_env].unlink()
    with pytest.raises(MigrationError, match=r"backup of .* is missing"):
        config_store.plan_rollback()
    _write(saved_mode / "migration.json", "[]")
    with pytest.raises(MigrationError, match="damaged"):
        config_store.plan_rollback()


# --- sentinel: a secret never reaches config.toml or anything shown ------------------------


def test_secret_sentinel(saved_mode: Path, project: Path) -> None:
    _write(project / ".env", _example_env() + f"ANTHROPIC_API_KEY={ANTHROPIC_VALUE}\n")
    plan = config_store.plan_migration()
    shown = [repr(plan), *plan.describe()]
    result = config_store.apply_migration(consent=plan.token)
    shown += [repr(result), (saved_mode / "migration.json").read_text("utf-8")]
    shown += [(saved_mode / "config.toml").read_text("utf-8")]
    snapshot = config_store.read_settings()
    shown += [repr(snapshot), repr(ConfigLayers.resolve(strict=False))]
    shown += [f.message for f in ConfigLayers.resolve(strict=False).findings]
    _write(saved_mode / "config.toml", "OLLAMA_MODEL = 'edited'\n")
    with pytest.raises(StaleEditError) as stale:
        config_store.save_settings(snapshot, {"OLLAMA_MODEL": "m"})
    shown.append(str(stale.value))
    shown += [repr(config_store.plan_rollback()), *config_store.plan_rollback().describe()]
    for secret in (OPENROUTER_VALUE, ANTHROPIC_VALUE):
        assert not [text for text in shown if secret in text]
    assert Settings.load().anthropic_api_key == ANTHROPIC_VALUE


# Every secret field is flagged, so it can never be saved in config.toml.
def test_secret_names() -> None:
    assert secret_names() == ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY")
    assert set(secret_names()) <= set(env_names())


# --- .env.example --------------------------------------------------------------------------


# Copying .env.example sets only the key and the persona: the model, endpoint and effort keep
# following the defaults (#28).
def test_copied_env_example_pins_no_defaults(tmp_path: Path) -> None:
    shutil.copy(REPO / ".env.example", tmp_path / ".env")
    layers = ConfigLayers.resolve()
    active = {k for layer in layers.layers[1:-1] for k in layer.values}
    assert active == {"OPENROUTER_API_KEY", "PROMPT_PERSONA"}
    for name in ("OPENROUTER_MODEL", "OPENROUTER_PROVIDER", "OPENROUTER_REASONING_EFFORT"):
        assert layers.entries[name].source == "default"


# A secrets.toml or config.toml that cannot be parsed fails closed, naming only the file.
@pytest.mark.parametrize(
    ("name", "data", "error"),
    [
        ("secrets.toml", b'OPENROUTER_API_KEY = "unterminated\n', "secrets.toml is not valid TOML"),
        ("config.toml", b"OLLAMA_MODEL = '\xff'\n", "config.toml is not UTF-8 text"),
    ],
    ids=["secrets-syntax", "config-encoding"],
)
def test_unreadable_saved_file_fails_closed(
    saved_mode: Path, name: str, data: bytes, error: str
) -> None:
    (saved_mode / name).write_bytes(data)
    with pytest.raises(ValueError, match=error):
        Settings.load()
    assert ConfigLayers.resolve(strict=False).findings[0].message.startswith(error)


# A .env edited after the preview is left where it is (ignored from now on), never moved.
def test_env_edited_during_migration_is_left(
    saved_mode: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_env = _write(project / ".env", "OLLAMA_MODEL=m\n")
    plan = config_store.plan_migration()
    real = config_store._effective

    def effective(environ: Mapping[str, str]) -> dict[str, tuple[Any, str]]:
        if (saved_mode / "config.toml").exists():
            _write(repo_env, "OLLAMA_MODEL=m\n# edited\n")
        return real(environ)

    monkeypatch.setattr(config_store, "_effective", effective)
    result = config_store.apply_migration(consent=plan.token)
    assert result.left == (repo_env,)
    config_store.apply_rollback(consent=config_store.plan_rollback().token)
    assert repo_env.read_text("utf-8").endswith("# edited\n")
    assert not (saved_mode / "config.toml").exists()


# --- odd config paths behave exactly as when nothing is saved -----------------------------


def _baseline(project: Path) -> dict[str, tuple[object, str]]:
    _write(project / ".env", "OLLAMA_MODEL=from-repo\n")
    return {k: (e.value, e.source) for k, e in ConfigLayers.resolve().entries.items()}


def _check_same(baseline: dict[str, tuple[object, str]]) -> list[str]:
    """Strict mode matches the baseline exactly; repair mode runs and returns its messages."""
    assert {k: (e.value, e.source) for k, e in ConfigLayers.resolve().entries.items()} == baseline
    assert Settings.load().ollama_model == "from-repo"
    return [f.message for f in ConfigLayers.resolve(strict=False).findings]


# The config dir is a file: there is no config.toml or secrets.toml, as before #84.
def test_config_dir_that_is_a_file(saved_mode: Path, project: Path) -> None:
    baseline = _baseline(project)
    saved_mode.rmdir()
    saved_mode.write_text("not a folder\n")
    assert _check_same(baseline) == []
    with pytest.raises(MigrationError):  # nowhere to back up to; nothing changes
        config_store.apply_migration(consent=config_store.plan_migration().token)
    assert (project / ".env").read_text("utf-8") == "OLLAMA_MODEL=from-repo\n"


# config.toml or secrets.toml is a folder: ignored, reported in repair mode only.
@pytest.mark.parametrize("name", ["config.toml", "secrets.toml"])
def test_saved_file_that_is_a_folder(saved_mode: Path, project: Path, name: str) -> None:
    baseline = _baseline(project)
    (saved_mode / name).mkdir()
    assert _check_same(baseline) == [f"{name} is not a file; it was ignored"]


# An unreadable config dir (mode 000) cannot even be checked: as if absent, never a crash.
@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions")
def test_unreadable_config_dir(saved_mode: Path, project: Path) -> None:
    baseline = _baseline(project)
    saved_mode.chmod(0)
    try:
        _check_same(baseline)
    finally:
        saved_mode.chmod(0o700)


# A config.toml that is a real file but cannot be read still fails closed.
@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions")
def test_unreadable_config_toml_fails_closed(saved_mode: Path, project: Path) -> None:
    _baseline(project)
    path = _write(saved_mode / "config.toml", 'OLLAMA_MODEL = "saved"\n')
    path.chmod(0)
    try:
        with pytest.raises(ValueError, match=r"config\.toml cannot be read"):
            Settings.load()
    finally:
        path.chmod(0o600)


# A byte-order mark (Windows Notepad) is not a syntax error.
def test_bom_is_accepted(saved_mode: Path) -> None:
    (saved_mode / "config.toml").write_bytes(b'\xef\xbb\xbfOLLAMA_MODEL = "m"\n')
    (saved_mode / "secrets.toml").write_bytes(
        b"\xef\xbb\xbf" + f'OPENROUTER_API_KEY = "{OPENROUTER_VALUE}"\n'.encode()
    )
    settings = Settings.load()
    assert (settings.ollama_model, settings.openrouter_api_key) == ("m", OPENROUTER_VALUE)


# --- migration robustness ------------------------------------------------------------------


# The marker is written before anything changes; if it cannot be written, nothing changes.
def test_marker_write_failure_changes_nothing(
    saved_mode: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_env = _write(project / ".env", _example_env())
    plan = config_store.plan_migration()

    def broken(directory: Path, record: Mapping[str, Any]) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(config_store, "_write_marker", broken)
    with pytest.raises(MigrationError, match="nothing was migrated"):
        config_store.apply_migration(consent=plan.token)
    assert repo_env.read_text("utf-8") == _example_env()
    assert not (saved_mode / "config.toml").exists()
    assert not (saved_mode / "secrets.toml").exists()
    assert config_store.plan_migration().status == "ready"


# A run cut short after the writes (here: before any .env moved) is rolled back from the
# marker written up front.
def test_interrupted_migration_can_be_rolled_back(
    saved_mode: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_env = _write(project / ".env", _example_env())
    plan = config_store.plan_migration()
    real_move = config_store._move

    def crash(source: Path, target: Path) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(config_store, "_move", crash)
    with pytest.raises(KeyboardInterrupt):
        config_store.apply_migration(consent=plan.token)
    assert config_store.plan_migration().status == "migrated"
    monkeypatch.setattr(config_store, "_move", real_move)
    config_store.apply_rollback(consent=config_store.plan_rollback().token)
    assert repo_env.read_text("utf-8") == _example_env()
    assert not (saved_mode / "config.toml").exists()


# A preview's token cannot be replayed once a migration and its rollback restored the files.
def test_consent_token_is_not_replayable(saved_mode: Path, project: Path) -> None:
    _write(project / ".env", _example_env())
    first = config_store.plan_migration().token
    config_store.apply_migration(consent=first)
    config_store.apply_rollback(consent=config_store.plan_rollback().token)
    with pytest.raises(MigrationError, match="not confirmed"):
        config_store.apply_migration(consent=first)
    assert config_store.plan_migration().token != first


# Smaller refusals: each is an error naming no value, and nothing is written.
def test_more_refusals(saved_mode: Path, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(project / ".env", _example_env())
    monkeypatch.setattr(
        FileSecretStore, "read", lambda self: (_ for _ in ()).throw(SecretStoreError("locked"))
    )
    with pytest.raises(MigrationError, match="fix the current settings first: locked"):
        config_store.plan_migration()


def test_store_write_failure_and_odd_sid(saved_mode: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    saved_mode.rmdir()
    saved_mode.write_text("")
    with pytest.raises(SecretStoreError, match=r"could not write secrets\.toml"):
        config_store.save_secret("OPENROUTER_API_KEY", OPENROUTER_VALUE)
    monkeypatch.setattr(config_files, "current_user_sid", lambda: "")
    with pytest.raises(SecretStoreError, match="private to your user"):
        config_files.restrict_to_user(saved_mode)


def test_move_never_overwrites(tmp_path: Path) -> None:
    source, target = _write(tmp_path / "a", "a"), _write(tmp_path / "b", "b")
    assert config_store._move(source, target) is False
    assert config_store._move(tmp_path / "missing", tmp_path / "c") is False
    assert target.read_text() == "b"


def test_damaged_marker(saved_mode: Path) -> None:
    _write(saved_mode / "migration.json", "{not json")
    with pytest.raises(MigrationError, match="damaged"):
        config_store.plan_rollback()


# Windows reports a config dir that is a file as FileExistsError on mkdir: still refused
# cleanly, with nothing changed.
def test_backup_folder_failure(
    saved_mode: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_env = _write(project / ".env", _example_env())
    plan = config_store.plan_migration()

    def broken(directory: Path) -> tuple[Path, str]:
        raise FileExistsError(183, "Cannot create a file when that file already exists")

    monkeypatch.setattr(config_store, "_new_backup_dir", broken)
    with pytest.raises(MigrationError, match="cannot create the backup folder"):
        config_store.apply_migration(consent=plan.token)
    assert repo_env.read_text("utf-8") == _example_env()


# A UTF-8 byte order mark is not part of the first key, so migration keeps it (#32).
def test_migration_reads_env_with_byte_order_mark(saved_mode: Path, project: Path) -> None:
    (project / ".env").write_bytes(b"\xef\xbb\xbfOLLAMA_MODEL=m\n")
    plan = config_store.plan_migration()
    assert dict(plan.settings) == {"OLLAMA_MODEL": "m"}
    assert plan.ignored == ()


# A value cut at an unquoted ` #` is never carried into config.toml: the migration is refused
# until the value is quoted (#32).
def test_migration_refuses_a_value_cut_at_a_comment(saved_mode: Path, project: Path) -> None:
    _write(project / ".env", "PROMPT_EXTRA_PATTERNS=ticket #\\d{5}\n")
    with pytest.raises(
        MigrationError,
        match=r"fix the \.env first: the value of PROMPT_EXTRA_PATTERNS was cut at ' #'",
    ):
        config_store.plan_migration()
    _write(project / ".env", 'PROMPT_EXTRA_PATTERNS="ticket #\\d{5}"\n')
    assert dict(config_store.plan_migration().settings) == {
        "PROMPT_EXTRA_PATTERNS": "ticket #\\d{5}"
    }


# Any other setting's ` # note` is an ordinary comment: migrated without it, not refused.
def test_migration_keeps_an_ordinary_inline_comment(saved_mode: Path, project: Path) -> None:
    _write(project / ".env", "OLLAMA_MODEL=m  # a note\n")
    assert dict(config_store.plan_migration().settings) == {"OLLAMA_MODEL": "m"}
