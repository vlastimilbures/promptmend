"""Saved settings: config.toml and the secret store (#84).

Every file lives under tmp_path: conftest points HOME, XDG_CONFIG_HOME, APPDATA and the
editable-install root there, and these tests unset PROMPTMEND_ENV only through
``saved_mode``.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from promptmend import config, config_files, config_store
from promptmend.cli import PERSONA_PLACEHOLDER, app
from promptmend.config import ConfigLayers, Settings, env_names, secret_names
from promptmend.config_files import FileSecretStore, SecretStoreError
from promptmend.config_store import (
    ConfigStoreError,
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
    directory = config.user_config_dir(os.environ)
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


# Outside legacy mode no .env is read, with config.toml or without (#275): neither the
# checkout's nor one in the user config folder. Repair mode reports both (the checkout's for
# an editable install), so their settings never vanish without a word; strict mode (the
# triggers) stays silent.
@pytest.mark.parametrize("saved", [True, False], ids=["config.toml", "no-config.toml"])
def test_no_dotenv_is_read_outside_legacy_mode(
    saved_mode: Path, project: Path, saved: bool
) -> None:
    if saved:
        _write(saved_mode / "config.toml", 'OPENROUTER_MODEL = "x/saved"\n')
    _write(project / ".env", "OPENROUTER_MODEL=x/old\nOLLAMA_MODEL=old\n")
    _write(saved_mode / ".env", "OPENROUTER_MODEL=x/older\nOLLAMA_MODEL=older\n")

    settings = Settings.load()
    assert settings.openrouter_model == ("x/saved" if saved else Settings().openrouter_model)
    assert settings.ollama_model == Settings().ollama_model
    repair = ConfigLayers.resolve(strict=False)
    message = (
        ".env is no longer read: save its settings with `promptmend setup`, then remove it; "
        "or set PROMPTMEND_ENV to it"
    )
    assert [(f.source, f.message) for f in repair.findings] == [
        (f"file:{saved_mode / '.env'}", message),
        (f"file:{project / '.env'}", message),  # an editable install's checkout
    ]
    (project / "pyproject.toml").unlink()  # a release install: the package dir is no checkout
    assert [f.source for f in ConfigLayers.resolve(strict=False).findings] == [
        f"file:{saved_mode / '.env'}"
    ]
    assert ConfigLayers.resolve().findings == ()
    assert not any(layer.source.endswith(".env") for layer in repair.layers)


# Legacy mode: the PROMPTMEND_ENV file alone, exactly as before.
def test_legacy_mode_ignores_saved_files(tmp_path: Path) -> None:
    directory = config.user_config_dir(os.environ)
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


# A removed setting (#245) that the user saved, or still sets, is ignored without a
# finding in either mode: doctor and `config validate` must not flag it.
def test_retired_settings_are_ignored(
    saved_mode: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(saved_mode / "config.toml", 'PROMPT_UI_INTRO = false\nOLLAMA_MODEL = "m"\n')
    monkeypatch.setenv("PROMPT_UI_INTRO", "maybe")
    assert ConfigLayers.resolve().settings().ollama_model == "m"
    assert ConfigLayers.resolve(strict=False).findings == ()
    monkeypatch.setenv("PROMPTMEND_ENV", str(_write(tmp_path / ".env", "PROMPT_UI_INTRO=no\n")))
    assert ConfigLayers.resolve().findings == ()
    assert ConfigLayers.resolve(strict=False).findings == ()
    assert "PROMPT_UI_INTRO" not in env_names()


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


# --- sentinel: a secret never reaches config.toml or anything shown ------------------------


def test_secret_sentinel(saved_mode: Path) -> None:
    config_store.save_secret("ANTHROPIC_API_KEY", ANTHROPIC_VALUE)
    _write(saved_mode / ".env", f"OPENROUTER_API_KEY={OPENROUTER_VALUE}\n")  # no longer read
    snapshot = config_store.save_settings(config_store.read_settings(), {"OLLAMA_MODEL": "m"})
    shown = [(saved_mode / "config.toml").read_text("utf-8"), repr(snapshot)]
    shown += [repr(ConfigLayers.resolve(strict=False))]
    shown += [f.message for f in ConfigLayers.resolve(strict=False).findings]
    _write(saved_mode / "config.toml", "OLLAMA_MODEL = 'edited'\n")
    with pytest.raises(StaleEditError) as stale:
        config_store.save_settings(snapshot, {"OLLAMA_MODEL": "m"})
    shown.append(str(stale.value))
    for secret in (OPENROUTER_VALUE, ANTHROPIC_VALUE):
        assert not [text for text in shown if secret in text]
    assert Settings.load().anthropic_api_key == ANTHROPIC_VALUE
    assert Settings.load().openrouter_api_key == ""


# Every secret field is flagged, so it can never be saved in config.toml.
def test_secret_names() -> None:
    assert secret_names() == ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
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


# --- odd config paths behave exactly as when nothing is saved -----------------------------


def _baseline(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[object, str]]:
    monkeypatch.setenv("OLLAMA_MODEL", "from-env")
    return {k: (e.value, e.source) for k, e in ConfigLayers.resolve().entries.items()}


def _check_same(baseline: dict[str, tuple[object, str]]) -> list[str]:
    """Strict mode matches the baseline exactly; repair mode runs and returns its messages."""
    assert {k: (e.value, e.source) for k, e in ConfigLayers.resolve().entries.items()} == baseline
    assert Settings.load().ollama_model == "from-env"
    return [f.message for f in ConfigLayers.resolve(strict=False).findings]


# The config dir is a file: there is no config.toml or secrets.toml, as before #84.
def test_config_dir_that_is_a_file(saved_mode: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = _baseline(monkeypatch)
    saved_mode.rmdir()
    saved_mode.write_text("not a folder\n")
    assert _check_same(baseline) == []


# config.toml or secrets.toml is a folder: ignored, reported in repair mode only.
@pytest.mark.parametrize("name", ["config.toml", "secrets.toml"])
def test_saved_file_that_is_a_folder(
    saved_mode: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    baseline = _baseline(monkeypatch)
    (saved_mode / name).mkdir()
    assert _check_same(baseline) == [f"{name} is not a file; it was ignored"]


# An unreadable config dir (mode 000) cannot even be checked: as if absent, never a crash.
@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions")
def test_unreadable_config_dir(saved_mode: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = _baseline(monkeypatch)
    saved_mode.chmod(0)
    try:
        _check_same(baseline)
    finally:
        saved_mode.chmod(0o700)


# A config.toml that is a real file but cannot be read still fails closed.
@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions")
def test_unreadable_config_toml_fails_closed(
    saved_mode: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _baseline(monkeypatch)
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
