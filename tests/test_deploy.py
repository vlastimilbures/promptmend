"""The managed Espanso deployment (#86). Every test works in temp dirs: no test reads or writes
the real Espanso folder, and the real espanso, uv and brew are never run (conftest refuses
deploy.run_command; a test passes or patches in a fake runner)."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner, Result

from promptmend import __version__, assets, deploy
from promptmend.cli import app
from promptmend.config import user_data_dir
from promptmend.match_history import KNOWN_SOURCES

runner = CliRunner()
REPO = Path(__file__).resolve().parents[1]
MATCH = REPO / "espanso" / "match"
NAMES = sorted(p.name for p in MATCH.glob("*.yml"))
LAUNCHER = "/Users/me/.local/bin/promptmend"
VERSION = __version__
# Captured at import, before conftest swaps it for a refusal in every test.
REAL_RUN_COMMAND = deploy.run_command
EXE = "promptmend.exe" if os.name == "nt" else "promptmend"
STATIC = "prompts-core.yml"  # the only match file that never calls the CLI
# The CLI match files as v0.20.0 shipped them, the last release that ran the CLI from shell
# vars (#18). Kept as files, since CI's checkout has no tags.
SHELL_0_20 = Path(__file__).parent / "golden" / "match-0.20.0"


# --- Today's install-script rendering, kept verbatim to prove deploy writes the same bytes ---


def _script_render_macos(source: str, cli_path: str) -> str:
    # install_macos.sh: python's str.replace of the placeholder, nothing else.
    return source.replace("__PROMPT_WORKFLOW__", cli_path)


def _script_render_windows(source: str, cli: str) -> str:
    # install_windows.ps1: $Cli -replace '\\', '/', then a literal .Replace().
    return source.replace("__PROMPT_WORKFLOW__", cli.replace("\\", "/"))


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize(
    "path", ["/Users/me/.local/bin/promptmend", "/Users/Jan Novák/a|b&c/promptmend"]
)
def test_render_matches_the_macos_script_plus_stamp(name: str, path: str) -> None:
    source = (MATCH / name).read_bytes().decode("utf-8")
    rendered = deploy.render(source, deploy.launcher_text(path, windows=False), "1.2.3")
    stamp = "# promptmend 1.2.3 (managed; edit at your own risk)\n"
    assert rendered == stamp + _script_render_macos(source, path)
    # The stamp is a comment: the YAML Espanso reads is unchanged.
    assert yaml.safe_load(rendered) == yaml.safe_load(_script_render_macos(source, path))


@pytest.mark.parametrize("name", NAMES)
def test_render_matches_the_windows_script_plus_stamp(name: str) -> None:
    cli = r"C:\Users\Jan Novák\.local\bin\promptmend.exe"
    source = (MATCH / name).read_bytes().decode("utf-8")
    rendered = deploy.render(source, deploy.launcher_text(cli, windows=True), "1.2.3")
    assert rendered.split("\n", 1)[1] == _script_render_windows(source, cli)
    assert "C:/Users/Jan Novák/.local/bin/promptmend.exe" in rendered or name == STATIC


# The packaged files are the ones the scripts deployed from.
def test_assets_are_the_repo_match_files() -> None:
    assert assets.match_names() == NAMES


# The launcher is a double-quoted YAML string that Espanso runs with no shell (#18): refused
# are what YAML would read as an escape or the string's end, characters YAML cannot hold,
# `{{` (Espanso fills {{name}} from its variables in every script param), and the %HOME%,
# %CONFIG% and %PACKAGES% Espanso replaces in every script arg.
_REFUSED = [
    *['"', "\\", "\n", "\t", "\x7f", "\x85", "\udc80", "\uffff", "{{x}}"],
    *["%HOME%", "%CONFIG%", "%PACKAGES%"],
]
_REFUSED_IDS = [
    *["quote", "bslash", "lf", "tab", "del", "nel", "surr", "ffff", "var"],
    *["home", "config", "packages"],
]


@pytest.mark.parametrize("bad", _REFUSED, ids=_REFUSED_IDS)
def test_launcher_guard_posix(bad: str) -> None:
    with pytest.raises(deploy.DeployError, match="quote, a backslash, a control character"):
        deploy.launcher_text(f"/opt/a{bad}b/promptmend", windows=False)


# On Windows a backslash becomes a slash first, so only the rest is refused.
@pytest.mark.parametrize(
    "bad", [b for b in _REFUSED if b != "\\"], ids=_REFUSED_IDS[:1] + _REFUSED_IDS[2:]
)
def test_launcher_guard_windows(bad: str) -> None:
    with pytest.raises(deploy.DeployError, match="quote, a backslash, a control character"):
        deploy.launcher_text(rf"C:\a{bad}b\promptmend.exe", windows=True)


# No shell runs the launcher any more, so what only sh or cmd.exe would interpret is allowed.
_SHELL_ONLY = ["$", "`", "%", "^", "&", "|", "<", ">", "'", " ", "#", ",", "]", "%HOM", "{x}"]
_ALLOWED = ["dollar", "tick", "pct", "caret", "amp", "pipe", "lt", "gt", "apos", "sp", "hash"]
_ALLOWED += ["comma", "bracket", "partial", "brace"]


@pytest.mark.parametrize("char", _SHELL_ONLY, ids=_ALLOWED)
@pytest.mark.parametrize("windows", [False, True], ids=["posix", "win"])
def test_launcher_shell_characters_are_allowed(char: str, windows: bool) -> None:
    path = rf"C:\a{char}b\promptmend.exe" if windows else f"/opt/a{char}b/promptmend"
    text = deploy.launcher_text(path, windows=windows)
    assert text == (path.replace("\\", "/") if windows else path)


# The rendered file is valid YAML whose every script var starts with the launcher exactly as
# given: spaces, non-ASCII letters, YAML flow characters and Windows forward slashes included.
@pytest.mark.parametrize(
    ("path", "windows"),
    [
        ("/Users/Jan Novák/a|b&c $x/promptmend", False),
        ("/opt/a, b]#c'd/promptmend", False),
        (r"C:\Users\Jan Novák\100% ^x\.local\bin\promptmend.exe", True),
    ],
    ids=["posix", "flow", "windows"],
)
def test_rendered_args_start_with_the_launcher(path: str, windows: bool) -> None:
    launcher = deploy.launcher_text(path, windows=windows)
    for name in NAMES:
        rendered = deploy.render(assets.read_match(name), launcher, "1.2.3")
        args = [
            var["params"]["args"]
            for match in yaml.safe_load(rendered)["matches"]
            for var in match.get("vars", [])
            if var.get("type") == "script"
        ]
        assert all(a[0] == launcher for a in args), name
        assert bool(args) is (name != STATIC), name
        assert deploy.launchers_in(rendered) == (set() if name == STATIC else {launcher})


# The launcher is found in the script args (#18) and in the shell cmd lines every earlier
# release wrote, so doctor and adoption see both.
def test_launchers_in_reads_both_shapes() -> None:
    script = 'args: ["/opt/new/promptmend", "improve", "--trigger-id", "i"]'
    shell = 'cmd: "\\"C:/old dir/promptmend.exe\\" improve --trigger-id i"'
    assert deploy.launchers_in(f"{script}\n{shell}\n") == {
        "/opt/new/promptmend",
        "C:/old dir/promptmend.exe",
    }


def test_launcher_windows_slashes() -> None:
    assert deploy.launcher_text(r"C:\x\promptmend.exe", windows=True) == ("C:/x/promptmend.exe")


# --- Plan, apply, states ------------------------------------------------------------------


@pytest.fixture
def espanso(tmp_path: Path) -> Path:
    root = tmp_path / "espanso"
    (root / "match").mkdir(parents=True)
    return root


def _plan(espanso: Path, launcher: str = LAUNCHER) -> deploy.Plan:
    return deploy.plan(espanso, launcher, deploy.Manifest.load())


def _states(espanso: Path, launcher: str = LAUNCHER) -> dict[str, str]:
    return {s.name: s.state for s in _plan(espanso, launcher).steps}


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_manifest_lives_in_the_user_data_dir(espanso: Path) -> None:
    deploy.apply(_plan(espanso))
    path = user_data_dir() / deploy.MANIFEST_NAME
    assert path.is_relative_to(Path(os.environ["XDG_DATA_HOME"]))
    data = json.loads(path.read_text("utf-8"))
    assert data["format"] == 1
    entry = data["files"][0]
    assert set(entry) == {"target", "asset_version", "digest", "launcher", "calls_cli", "backups"}
    assert entry["asset_version"] == VERSION
    assert entry["launcher"] == LAUNCHER


def test_deploy_twice_is_a_noop(espanso: Path) -> None:
    assert set(_states(espanso).values()) == {deploy.MISSING}
    first = deploy.apply(_plan(espanso))
    assert first.changed
    for name in NAMES:
        text = (espanso / "match" / name).read_text("utf-8")
        assert text.startswith(f"# promptmend {VERSION} (managed; edit at your own risk)\n")
    before = _tree(espanso)
    manifest = (user_data_dir() / deploy.MANIFEST_NAME).read_bytes()

    second = _plan(espanso)
    assert second.is_noop
    assert set(_states(espanso).values()) == {deploy.IN_SYNC}
    outcome = deploy.apply(second)
    assert (outcome.changed, outcome.lines) == (False, [])
    assert _tree(espanso) == before
    assert (user_data_dir() / deploy.MANIFEST_NAME).read_bytes() == manifest


# No code path writes to Espanso's config/ (#37), and deploy touches nothing but match/.
def test_deploy_never_touches_config(espanso: Path) -> None:
    config = espanso / "config" / "default.yml"
    config.parent.mkdir()
    config.write_text("toggle_key: ALT\n", "utf-8")
    deploy.apply(_plan(espanso))
    changed = {k for k in _tree(espanso) if not k.startswith("match")}
    assert changed == {str(Path("config") / "default.yml")}
    assert config.read_text("utf-8") == "toggle_key: ALT\n"


def test_apply_refuses_a_target_outside_match(espanso: Path) -> None:
    the_plan = _plan(espanso)
    the_plan.steps[0].target = espanso / "config" / "x.yml"
    with pytest.raises(deploy.DeployError, match="Refusing to write outside"):
        deploy.apply(the_plan)


def _edit(espanso: Path, name: str = "prompts-llm.yml") -> Path:
    target = espanso / "match" / name
    target.write_text(target.read_text("utf-8") + "# my tweak\n", "utf-8")
    return target


# A user-edited file is never overwritten without a choice.
def test_modified_file_is_kept_without_a_choice(espanso: Path) -> None:
    deploy.apply(_plan(espanso))
    target = _edit(espanso)
    edited = target.read_bytes()
    the_plan = _plan(espanso)
    assert [s.name for s in the_plan.conflicts] == ["prompts-llm.yml"]
    assert the_plan.conflicts[0].state == deploy.MODIFIED
    assert "+# my tweak" not in the_plan.conflicts[0].diff()
    assert "-# my tweak" in the_plan.conflicts[0].diff()
    outcome = deploy.apply(the_plan)
    assert target.read_bytes() == edited
    assert not outcome.changed
    assert any("kept your modified" in line for line in outcome.lines)
    # Still modified: the manifest kept our digest.
    assert _states(espanso)["prompts-llm.yml"] == deploy.MODIFIED


def test_modified_take_ours_backs_up(espanso: Path) -> None:
    deploy.apply(_plan(espanso))
    target = _edit(espanso)
    edited = target.read_bytes()
    deploy.apply(_plan(espanso), {"prompts-llm.yml": deploy.OURS})
    assert "# my tweak" not in target.read_text("utf-8")
    entry = deploy.Manifest.load().entries[str(target)]
    assert len(entry.backups) == 1
    backup = Path(entry.backups[0])
    assert backup.name.startswith("prompts-llm.yml.bak-")
    assert backup.read_bytes() == edited


def test_modified_side_by_side(espanso: Path) -> None:
    deploy.apply(_plan(espanso))
    target = _edit(espanso)
    edited = target.read_bytes()
    deploy.apply(_plan(espanso), {"prompts-llm.yml": deploy.SIDE})
    assert target.read_bytes() == edited
    side = target.with_name("prompts-llm.yml" + deploy.SIDE_SUFFIX)
    assert side.read_text("utf-8") == _plan(espanso).steps[1].rendered
    assert not side.name.endswith(".yml")  # Espanso does not load it


def test_unknown_choice(espanso: Path) -> None:
    deploy.apply(_plan(espanso))
    _edit(espanso)
    with pytest.raises(deploy.DeployError, match="Unknown choice"):
        deploy.apply(_plan(espanso), {"prompts-llm.yml": "merge"})


def test_foreign_file(espanso: Path) -> None:
    (espanso / "match" / STATIC).write_text("matches: []\n", "utf-8")
    the_plan = _plan(espanso)
    assert {s.name: s.state for s in the_plan.conflicts} == {STATIC: deploy.FOREIGN}
    deploy.apply(the_plan)
    assert (espanso / "match" / STATIC).read_text("utf-8") == "matches: []\n"
    assert str(espanso / "match" / STATIC) not in deploy.Manifest.load().entries


# A file the install scripts wrote (same body, no stamp, no manifest) is ours and stale.
def test_script_deployed_file_is_stale(espanso: Path) -> None:
    for name in NAMES:
        source = (MATCH / name).read_text("utf-8")
        (espanso / "match" / name).write_text(_script_render_macos(source, LAUNCHER), "utf-8")
    assert set(_states(espanso).values()) == {deploy.STALE}
    outcome = deploy.apply(_plan(espanso))
    assert all(line.startswith("updated") for line in outcome.lines)
    assert not list((espanso / "match").glob("*.bak-*"))
    assert _plan(espanso).is_noop


# The #25 drift case: an old prompts-template.yml deployed next to a newer CLI.
def test_status_reports_stale_after_an_upgrade(
    monkeypatch: pytest.MonkeyPatch, espanso: Path
) -> None:
    old_source = "# old template\nmatches: []\n"
    real = assets.read_match
    monkeypatch.setattr(deploy, "__version__", "0.15.0")
    monkeypatch.setattr(
        assets,
        "read_match",
        lambda name: old_source if name == "prompts-template.yml" else real(name),
    )
    deploy.apply(_plan(espanso))
    monkeypatch.setattr(deploy, "__version__", "0.16.0")
    monkeypatch.setattr(assets, "read_match", real)

    states = _states(espanso)
    assert states["prompts-template.yml"] == deploy.STALE
    assert {states[n] for n in NAMES if n != "prompts-template.yml"} == {deploy.STALE}
    result = runner.invoke(
        app, ["espanso", "status", "--espanso-dir", str(espanso), "--launcher", LAUNCHER]
    )
    assert result.exit_code == 0
    assert "stale     prompts-template.yml" in result.stdout
    deploy.apply(_plan(espanso))
    target = espanso / "match" / "prompts-template.yml"
    assert target.read_text("utf-8").startswith("# promptmend 0.16.0 (managed")
    assert deploy.Manifest.load().entries[str(target)].asset_version == "0.16.0"


# A new launcher path makes every CLI-calling file stale; the static one stays in sync.
def test_new_launcher_is_stale(espanso: Path) -> None:
    deploy.apply(_plan(espanso))
    states = _states(espanso, "/opt/homebrew/bin/promptmend")
    assert states == {n: deploy.IN_SYNC if n == STATIC else deploy.STALE for n in NAMES}


def test_backups_pruned_to_the_last_two_of_ours(
    monkeypatch: pytest.MonkeyPatch, espanso: Path
) -> None:
    deploy.apply(_plan(espanso))
    target = espanso / "match" / "prompts-llm.yml"
    theirs = target.with_name("prompts-llm.yml.bak-19990101000000")
    theirs.write_text("an old backup of the user's", "utf-8")
    stamps = iter(["20260101000001", "20260101000002", "20260101000003"])
    monkeypatch.setattr(deploy, "_stamp", lambda: next(stamps))
    for _ in range(3):
        _edit(espanso)
        deploy.apply(_plan(espanso), {"prompts-llm.yml": deploy.OURS})
    left = sorted(p.name for p in target.parent.glob("prompts-llm.yml.bak-*"))
    assert left == [
        "prompts-llm.yml.bak-19990101000000",
        "prompts-llm.yml.bak-20260101000002",
        "prompts-llm.yml.bak-20260101000003",
    ]
    entry = deploy.Manifest.load().entries[str(target)]
    assert [Path(b).name for b in entry.backups] == left[1:]


def test_backup_name_never_clobbers(espanso: Path) -> None:
    path = espanso / "match" / "a.yml"
    path.write_text("one", "utf-8")
    first = deploy._backup(path, "20260101000000")
    second = deploy._backup(path, "20260101000000")
    assert (first.name, second.name) == ("a.yml.bak-20260101000000", "a.yml.bak-20260101000000-1")


@pytest.mark.parametrize(
    ("launcher", "legacy"),
    [
        ("/Users/me/.local/bin/prompt-workflow", True),
        ("C:/Users/me/.local/bin/prompt-workflow.exe", True),
        ("C:\\Users\\me\\.local\\bin\\Prompt-Workflow.EXE", True),
        ("/Users/me/.local/bin/promptmend", False),
        ("/Users/me/prompt-workflow/bin/promptmend", False),
    ],
)
def test_is_legacy_launcher(launcher: str, legacy: bool) -> None:
    assert deploy.is_legacy_launcher(launcher) is legacy


def test_non_utf8_file_is_foreign(espanso: Path) -> None:
    (espanso / "match" / STATIC).write_bytes(b"\xff\xfe")
    assert _states(espanso)[STATIC] == deploy.FOREIGN


# --- Detach -------------------------------------------------------------------------------


def test_detach_keep_static(espanso: Path) -> None:
    deploy.apply(_plan(espanso))
    template = _edit(espanso, "prompts-template.yml")
    outcome = deploy.detach(deploy.Manifest.load(), espanso)
    match = espanso / "match"
    assert sorted(p.name for p in match.iterdir()) == sorted([STATIC, "prompts-template.yml"])
    assert "# my tweak" in template.read_text("utf-8")
    assert any("edited since" in line for line in outcome.lines)
    # The static snippets and the edited file stay on record; removed ones are forgotten.
    assert sorted(Path(t).name for t in deploy.Manifest.load().entries) == sorted(
        [STATIC, "prompts-template.yml"]
    )


def test_detach_remove_all(espanso: Path) -> None:
    deploy.apply(_plan(espanso))
    edited = _edit(espanso, STATIC)
    deploy.detach(deploy.Manifest.load(), espanso, remove_all=True)
    assert [p.name for p in (espanso / "match").iterdir()] == [STATIC]
    assert "# my tweak" in edited.read_text("utf-8")
    edited.unlink()
    outcome = deploy.detach(deploy.Manifest.load(), espanso, remove_all=True)
    assert any("already gone" in line for line in outcome.lines)
    assert not (user_data_dir() / deploy.MANIFEST_NAME).exists()


def _deploy_elsewhere(tmp_path: Path, name: str) -> Path:
    other = tmp_path / name
    (other / "match").mkdir(parents=True)
    deploy.apply(_plan(other))
    return other


def test_apply_forgets_entries_whose_file_is_gone(tmp_path: Path, espanso: Path) -> None:
    """Entries for a deleted folder are dropped; an entry outside the plan whose file still
    exists stays, and no file is touched."""
    gone = _deploy_elsewhere(tmp_path, "gone")
    kept = _deploy_elsewhere(tmp_path, "kept")
    deploy.apply(_plan(espanso))
    shutil.rmtree(gone)
    the_plan = _plan(espanso)
    assert the_plan.orphans == sorted(str(gone / "match" / n) for n in NAMES)
    assert not the_plan.is_noop
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.yml")}
    outcome = deploy.apply(the_plan)
    assert not outcome.changed
    assert sorted(outcome.lines) == sorted(
        f"forgot {gone / 'match' / n} (already gone)" for n in NAMES
    )
    entries = deploy.Manifest.load().entries
    assert sorted(entries) == sorted(str(d / "match" / n) for d in (espanso, kept) for n in NAMES)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.yml")} == before
    assert _plan(espanso).is_noop


def test_apply_keeps_an_orphan_that_came_back(tmp_path: Path, espanso: Path) -> None:
    gone = _deploy_elsewhere(tmp_path, "gone")
    shutil.rmtree(gone)
    the_plan = _plan(espanso)
    assert the_plan.orphans
    _deploy_elsewhere(tmp_path, "gone")  # back before the plan is applied
    deploy.apply(the_plan)
    assert all(str(gone / "match" / n) in deploy.Manifest.load().entries for n in NAMES)


@pytest.mark.skipif(os.name == "nt", reason="chmod cannot lock a folder on Windows")
def test_an_unreadable_folder_is_not_gone(tmp_path: Path, espanso: Path) -> None:
    """An entry under a folder we may not look into (chmod 000, a privacy-guarded folder) is
    neither a crash nor an orphan: it stays on record."""
    locked = _deploy_elsewhere(tmp_path, "locked")
    deploy.apply(_plan(espanso))
    (locked / "match").chmod(0)
    try:
        if os.access(locked / "match", os.R_OK | os.X_OK):
            pytest.skip("chmod is not effective (running as root)")
        the_plan = _plan(espanso)
        assert the_plan.orphans == []
        assert the_plan.is_noop
        deploy.apply(the_plan)
    finally:
        (locked / "match").chmod(0o755)
    assert all(str(locked / "match" / n) in deploy.Manifest.load().entries for n in NAMES)


def test_cli_deploy_forgets_gone_entries_when_in_sync(
    tmp_path: Path, espanso: Path, fake_run: FakeRunner
) -> None:
    deploy.apply(_plan(espanso))
    shutil.rmtree(_deploy_elsewhere(tmp_path, "gone"))
    result = _cli("deploy", "--yes", *_where(espanso))
    assert result.exit_code == 0, result.output
    assert "forget    " in result.stdout
    assert "forgot " in result.stdout
    assert fake_run.calls == []  # nothing Espanso loads changed: no restart
    assert "Nothing to do" in _cli("deploy", "--yes", *_where(espanso)).stdout


def test_detach_leaves_unowned_files(espanso: Path) -> None:
    other = espanso / "match" / "mine.yml"
    other.write_text("matches: []\n", "utf-8")
    deploy.apply(_plan(espanso))
    deploy.detach(deploy.Manifest.load(), espanso, remove_all=True)
    assert [p.name for p in (espanso / "match").iterdir()] == ["mine.yml"]


# --- Manifest -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("{not json", "cannot be read"),
        ('{"format": 99}', "unknown format"),
        ('{"format": 1, "files": [{"target": "x"}]}', "damaged"),
    ],
)
def test_bad_manifest(content: str, error: str) -> None:
    path = user_data_dir() / deploy.MANIFEST_NAME
    path.parent.mkdir(parents=True)
    path.write_text(content, "utf-8")
    with pytest.raises(deploy.DeployError, match=error):
        deploy.Manifest.load()


# --- Launcher resolver ----------------------------------------------------------------------


class FakeRunner:
    def __init__(self, answers: Mapping[str, str | deploy.CommandFailure] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> str | deploy.CommandFailure | None:
        self.calls.append(list(argv))
        return self.answers.get(" ".join(argv))


def _exe(path: Path, body: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n", "utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


# The same for every uv channel: a wheel URL, a local wheel and PyPI
# (`uv tool install promptmend`) all install into `<uv tool dir>/<project name>`.
def test_resolve_uv(tmp_path: Path) -> None:
    tools, bin_dir = tmp_path / "uv" / "tools", tmp_path / "bin"
    launcher = _exe(bin_dir / "promptmend")
    fake = FakeRunner({"uv tool dir": f"{tools}\n", "uv tool dir --bin": f"{bin_dir}\n"})
    found = deploy.resolve_launcher(
        runner=fake, prefix=tools / "promptmend", script=tmp_path / "x", windows=False
    )
    assert found == deploy.Launcher(launcher, "uv")


def test_resolve_uv_windows_exe(tmp_path: Path) -> None:
    tools, bin_dir = tmp_path / "tools", tmp_path / "bin"
    launcher = _exe(bin_dir / "promptmend.exe")
    fake = FakeRunner({"uv tool dir": str(tools), "uv tool dir --bin": str(bin_dir)})
    found = deploy.resolve_launcher(
        runner=fake, prefix=tools / "promptmend", script=tmp_path / "x", windows=True
    )
    assert found.path == launcher


def _cellar(tmp_path: Path, version: str) -> Path:
    return tmp_path / "brew" / "Cellar" / "promptmend" / version / "libexec"


def test_resolve_homebrew_never_cellar(tmp_path: Path) -> None:
    brew = tmp_path / "brew"
    launcher = _exe(brew / "bin" / "promptmend")
    _exe(_cellar(tmp_path, "0.15.0") / "bin" / "promptmend")
    found = deploy.resolve_launcher(
        runner=FakeRunner({"brew --prefix": f"{brew}\n"}),
        prefix=_cellar(tmp_path, "0.15.0"),
        script=_cellar(tmp_path, "0.15.0") / "bin" / "promptmend",
        windows=False,
    )
    assert found == deploy.Launcher(launcher, "homebrew")


def test_resolve_homebrew_opt(tmp_path: Path) -> None:
    brew = tmp_path / "brew"
    launcher = _exe(brew / "opt" / "promptmend" / "bin" / "promptmend")
    found = deploy.resolve_launcher(
        runner=FakeRunner({"brew --prefix": str(brew)}),
        prefix=_cellar(tmp_path, "0.15.0"),
        script=tmp_path / "x",
        windows=False,
    )
    assert found.path == launcher


# The tap's formula (scripts/brew_formula.py): Language::Python::Virtualenv builds the venv in
# `<Cellar>/promptmend/<version>/libexec` and links `<prefix>/bin/promptmend` to its
# console script. Python may report the venv through the Cellar path or the `opt` link.
@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need a privilege on Windows")
@pytest.mark.parametrize("via", ["cellar", "opt"])
def test_resolve_the_tap_formula(tmp_path: Path, via: str) -> None:
    brew = tmp_path / "homebrew"
    libexec = brew / "Cellar" / "promptmend" / "0.19.0" / "libexec"
    script = _exe(libexec / "bin" / "promptmend")
    (brew / "bin").mkdir(parents=True)
    (brew / "bin" / "promptmend").symlink_to(script)
    (brew / "opt").mkdir()
    (brew / "opt" / "promptmend").symlink_to(libexec.parent)
    prefix = libexec if via == "cellar" else brew / "opt" / "promptmend" / "libexec"
    fake = FakeRunner({"brew --prefix": f"{brew}\n"})
    found = deploy.resolve_launcher(
        runner=fake, prefix=prefix, script=brew / "bin" / "promptmend", windows=False
    )
    assert found == deploy.Launcher(brew / "bin" / "promptmend", "homebrew")
    assert "Cellar" not in deploy.launcher_text(found.path, windows=False)


# A venv under some other `opt` folder (or no brew at all) is not Homebrew's.
def test_resolve_an_opt_prefix_outside_homebrew(tmp_path: Path) -> None:
    script = _exe(tmp_path / "opt" / "tools" / "bin" / "promptmend")
    for answer in ({"brew --prefix": str(tmp_path / "homebrew")}, {}):
        found = deploy.resolve_launcher(
            runner=FakeRunner(answer),
            prefix=tmp_path / "opt" / "tools",
            script=script,
            windows=False,
        )
        assert found == deploy.Launcher(script, "script")


def test_resolve_scoop_shim(tmp_path: Path) -> None:
    scoop = tmp_path / "scoop"
    shim = _exe(scoop / "shims" / "promptmend.exe")
    found = deploy.resolve_launcher(
        runner=FakeRunner(),
        prefix=scoop / "apps" / "promptmend" / "current",
        script=tmp_path / "x",
        windows=True,
    )
    assert found == deploy.Launcher(shim, "scoop")


def test_resolve_running_script(tmp_path: Path) -> None:
    script = _exe(tmp_path / "pipx" / "bin" / "promptmend")
    found = deploy.resolve_launcher(
        runner=FakeRunner(), prefix=tmp_path / "venv", script=script, windows=False
    )
    assert found == deploy.Launcher(script, "script")


# Windows runs a console script as argv[0] without the .exe.
def test_resolve_running_script_windows(tmp_path: Path) -> None:
    exe = _exe(tmp_path / "Scripts" / "promptmend.exe")
    found = deploy.resolve_launcher(
        runner=FakeRunner(),
        prefix=tmp_path / "venv",
        script=tmp_path / "Scripts" / "promptmend",
        windows=True,
    )
    assert found.path == exe


@pytest.mark.parametrize(
    "parts",
    [("Cellar", "x", "bin"), ("app", "0.15.0", "bin"), ("checkout", ".venv", "bin")],
    ids=["cellar", "versioned", "project-venv"],
)
def test_resolve_refuses_an_unstable_script(tmp_path: Path, parts: tuple[str, ...]) -> None:
    script = _exe(tmp_path.joinpath(*parts) / "promptmend")
    with pytest.raises(deploy.DeployError, match="stable promptmend launcher"):
        deploy.resolve_launcher(
            runner=FakeRunner(), prefix=tmp_path / "venv", script=script, windows=False
        )


def test_resolve_nothing(tmp_path: Path) -> None:
    with pytest.raises(deploy.DeployError, match="pass --launcher"):
        deploy.resolve_launcher(
            runner=FakeRunner(), prefix=tmp_path, script=Path("promptmend"), windows=False
        )


# Upgrade: install N, deploy, install N+1, remove N; the deployed launcher still resolves and
# runs. Simulated with fake channel layouts rather than real venvs: building two venvs needs
# the network (or a warm uv cache) and real installs, which tests must not do. What matters is
# that deploy never writes a path inside the versioned install, which this checks end to end.
@pytest.mark.skipif(sys.platform == "win32", reason="runs a POSIX shell launcher")
def test_upgrade_keeps_the_uv_launcher(tmp_path: Path, espanso: Path) -> None:
    tools, bin_dir = tmp_path / "tools", tmp_path / "bin"
    venv = tools / "promptmend"
    _exe(venv / "bin" / "promptmend-real", "echo N")
    launcher = _exe(bin_dir / "promptmend", f'exec "{venv}/bin/promptmend-real"')
    fake = FakeRunner({"uv tool dir": str(tools), "uv tool dir --bin": str(bin_dir)})
    found = deploy.resolve_launcher(runner=fake, prefix=venv, script=tmp_path / "x", windows=False)
    deploy.apply(_plan(espanso, deploy.launcher_text(found.path, windows=False)))

    # uv tool install --force: the venv is removed and rebuilt, the bin entry rewritten.
    for p in sorted(venv.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    _exe(venv / "bin" / "promptmend-real", "echo N+1")

    deployed = (espanso / "match" / "prompts-llm.yml").read_text("utf-8")
    assert f'args: ["{launcher}", "improve"' in deployed
    out = subprocess.run([str(launcher)], capture_output=True, text=True, timeout=10, check=True)
    assert out.stdout.strip() == "N+1"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need a privilege on Windows")
def test_upgrade_keeps_the_homebrew_launcher(tmp_path: Path, espanso: Path) -> None:
    brew = tmp_path / "brew"
    old = _exe(_cellar(tmp_path, "0.15.0") / "bin" / "promptmend", "echo N")
    (brew / "bin").mkdir(parents=True)
    link = brew / "bin" / "promptmend"
    link.symlink_to(old)
    found = deploy.resolve_launcher(
        runner=FakeRunner({"brew --prefix": str(brew)}),
        prefix=_cellar(tmp_path, "0.15.0"),
        script=old,
        windows=False,
    )
    assert found.path == link
    deploy.apply(_plan(espanso, deploy.launcher_text(found.path, windows=False)))

    # brew upgrade: install N+1 in its own Cellar dir, relink, remove N.
    new = _exe(_cellar(tmp_path, "0.16.0") / "bin" / "promptmend", "echo N+1")
    link.unlink()
    link.symlink_to(new)
    old.unlink()

    deployed = (espanso / "match" / "prompts-llm.yml").read_text("utf-8")
    assert "Cellar" not in deployed
    out = subprocess.run([str(link)], capture_output=True, text=True, timeout=10, check=True)
    assert out.stdout.strip() == "N+1"


# --- Espanso folder and restart -----------------------------------------------------------------


def test_espanso_dir_from_espanso() -> None:
    fake = FakeRunner({"espanso path config": "/x/espanso\n"})
    assert deploy.espanso_dir(fake) == Path("/x/espanso")
    assert fake.calls == [["espanso", "path", "config"]]


def test_espanso_dir_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(deploy, "default_espanso_dir", lambda: tmp_path / "default")
    assert deploy.espanso_dir(FakeRunner()) == tmp_path / "default"


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("win32", {"APPDATA": "/r"}, Path("/r/espanso")),
        ("darwin", {}, Path("~/Library/Application Support/espanso")),
        ("linux", {"XDG_CONFIG_HOME": "/c"}, Path("/c/espanso")),
        ("linux", {}, Path("~/.config/espanso")),
    ],
)
def test_default_espanso_dir(platform: str, env: dict[str, str], expected: Path) -> None:
    # Expanded here: conftest gives each test its own home folder.
    assert deploy.default_espanso_dir(env, platform=platform) == expected.expanduser()


@pytest.mark.parametrize(
    ("answers", "ok", "calls"),
    [
        ({"espanso restart": ""}, True, [["espanso", "restart"]]),
        ({"espanso start": ""}, True, [["espanso", "restart"], ["espanso", "start"]]),
        ({}, False, [["espanso", "restart"], ["espanso", "start"]]),
    ],
)
def test_restart(answers: dict[str, str], ok: bool, calls: list[list[str]]) -> None:
    fake = FakeRunner(answers)
    assert deploy.restart_espanso(fake) is ok
    assert fake.calls == calls


def _ok(answer: str | deploy.CommandFailure) -> str:
    assert isinstance(answer, str), answer
    return answer


def _failed(answer: str | deploy.CommandFailure) -> deploy.CommandFailure:
    assert isinstance(answer, deploy.CommandFailure), answer
    return answer


# The real runner, tried on this test's own interpreter only.
def test_run_command() -> None:
    py = sys.executable
    assert _ok(REAL_RUN_COMMAND([py, "-c", "print('hi')"])).strip() == "hi"
    failed = _failed(REAL_RUN_COMMAND([py, "-c", "raise SystemExit(3)"]))
    assert failed == deploy.CommandFailure(found=True, path=failed.path, returncode=3)
    assert REAL_RUN_COMMAND([str(Path(py).parent / "no-such-binary")]) == deploy.CommandFailure()


def test_run_command_keeps_the_first_useful_stderr_line() -> None:
    """#115: Espanso panics with a location line first; the message comes after it."""
    stderr = (
        "thread 'main' panicked at espanso/src/main.rs:611:64:\n\n"
        "unable to load config: \x1b[31mmissing\u202e dir\nCaused by: x\n"
    )
    # Written as UTF-8 bytes, as Espanso writes them: a Windows child's text stderr would
    # encode with the console code page instead.
    script = f"import sys; sys.stderr.buffer.write({stderr.encode()!r}); sys.exit(101)"
    failed = _failed(REAL_RUN_COMMAND([sys.executable, "-c", script]))
    assert (failed.returncode, failed.error) == (101, "unable to load config: [31mmissing dir")
    argv = deploy.PATH_CONFIG
    assert failed.describe(argv) == (
        "`espanso path config` failed (exit 101): unable to load config: [31mmissing dir"
    )


def test_run_command_resolves_through_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows `espanso` is `espanso.cmd`: it is run by the path `shutil.which` gives, so
    an installed command is never reported as missing (#115). (Since #210 a Windows run
    answers `espanso` itself; that is tested in test_espanso_windows.py.)"""
    seen = []

    def which(name: str) -> str:
        seen.append(name)
        return sys.executable

    monkeypatch.setattr(shutil, "which", which)
    assert _ok(REAL_RUN_COMMAND(["espanso", "-c", "print('ran')"])).strip() == "ran"
    assert seen == ["espanso"]

    def broken(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("access denied")

    monkeypatch.setattr(subprocess, "Popen", broken)
    failed = _failed(REAL_RUN_COMMAND(deploy.PATH_CONFIG))
    assert failed == deploy.CommandFailure(found=True, path=sys.executable, error="access denied")
    assert failed.describe(deploy.PATH_CONFIG) == (
        "`espanso path config` could not be run: access denied"
    )


def test_run_command_survives_undecodable_output() -> None:
    """Output that is not UTF-8 is replaced, never raised as UnicodeDecodeError."""
    script = (
        "import sys; sys.stdout.buffer.write(b'out \\xff'); "
        "sys.stderr.buffer.write(b'bad \\x8d byte\\n'); sys.exit(101)"
    )
    failed = _failed(REAL_RUN_COMMAND([sys.executable, "-c", script]))
    assert (failed.returncode, failed.error) == (101, "bad \ufffd byte")
    ok = REAL_RUN_COMMAND(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'C:/\\xc4\\x8d')"]
    )
    assert ok == "C:/\u010d"


def test_run_command_timeout_does_not_wait_for_a_grandchild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A timed-out command whose child keeps the pipes open (espanso.cmd's espansod.exe on
    Windows) still returns: the tree is killed, and the pipes are not waited on for ever."""
    monkeypatch.setattr(deploy, "COMMAND_TIMEOUT", 0.5)
    monkeypatch.setattr(deploy, "GIVE_UP_TIMEOUT", 1)
    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({str(pid_file)!r}, 'w').write(str(child.pid)); time.sleep(60)"
    )
    started = time.monotonic()
    try:
        failed = _failed(REAL_RUN_COMMAND([sys.executable, "-c", script]))
        # A hang would wait the grandchild's 60 s; the margin keeps a slow runner green.
        assert time.monotonic() - started < 30
        assert failed.timed_out
    finally:
        if pid_file.exists() and pid_file.read_text():
            with contextlib.suppress(OSError):
                os.kill(int(pid_file.read_text()), signal.SIGTERM)


def test_run_command_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploy, "COMMAND_TIMEOUT", 0.2)
    failed = _failed(REAL_RUN_COMMAND([sys.executable, "-c", "import time; time.sleep(10)"]))
    assert (failed.found, failed.timed_out) == (True, True)
    assert failed.describe(deploy.PATH_CONFIG) == "`espanso path config` timed out after 0.2 s"


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("", None),
        ("\n  \n", None),
        ("thread 'main' panicked at x.rs:1:2:\n", None),
        ("thread 'main' (3545792) panicked at x.rs:1:2:\nunable to load\n", "unable to load"),
        ("thread 'main' (x) panicked at\n", "thread 'main' (x) panicked at"),
        (
            "thread 'main' panicked at 'unable to load config: missing', src/main.rs:611:64\n"
            "note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace\n",
            "unable to load config: missing",
        ),
        ("note: run with `RUST_BACKTRACE=1` to display a backtrace\nreal\n", "real"),
        (
            "thread 'main' panicked at 'unable to load config: unable to load config\n\n"
            "Caused by:\n    missing config directory', espanso/src/main.rs:611:64\n",
            "unable to load config: unable to load config",
        ),
        ("plain error\nsecond\n", "plain error"),
        ("\x07bell\u2066 and bidi\r\n", "bell and bidi"),
        ("x" * 500, "x" * 199 + "\u2026"),
    ],
)
def test_error_line(stderr: str, expected: str | None) -> None:
    assert deploy._error_line(stderr) == expected


@pytest.mark.parametrize(
    ("answer", "fallback"),
    [
        (None, "espanso was not found on PATH"),
        (deploy.CommandFailure(), "espanso was not found on PATH"),
        (deploy.CommandFailure(found=True, timed_out=True), "`espanso path config` timed out"),
        (
            deploy.CommandFailure(found=True, returncode=101, error="unable to load config"),
            "`espanso path config` failed (exit 101): unable to load config",
        ),
        ("\n", "`espanso path config` printed nothing"),
    ],
)
def test_locate_espanso_dir_says_why_it_falls_back(
    answer: str | deploy.CommandFailure | None,
    fallback: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(deploy, "default_espanso_dir", lambda: tmp_path / "default")
    found = deploy.locate_espanso_dir(lambda argv: answer)
    assert found.path == tmp_path / "default"
    assert found.fallback
    assert found.fallback.startswith(fallback)
    assert deploy.locate_espanso_dir(lambda argv: "/x/espanso\n") == deploy.EspansoDir(
        Path("/x/espanso")
    )


# --- CLI ------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI tests answer prompts through CliRunner's input, as a person at a terminal would;
    without a terminal the commands refuse to ask (tests/test_commands.py)."""
    from promptmend.commands import common

    monkeypatch.setattr(common, "stdin_is_tty", lambda: True)


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    fake = FakeRunner({"espanso restart": ""})
    monkeypatch.setattr(deploy, "run_command", fake)
    return fake


def _cli(*args: str, input: str | None = None) -> Result:
    return runner.invoke(app, ["espanso", *args], input=input)


def _where(espanso: Path) -> list[str]:
    return ["--espanso-dir", str(espanso), "--launcher", LAUNCHER]


def test_cli_deploy_yes_then_noop(espanso: Path, fake_run: FakeRunner) -> None:
    result = _cli("deploy", "--yes", *_where(espanso))
    assert result.exit_code == 0, result.output
    assert "missing   prompts-llm.yml" in result.stdout
    assert fake_run.calls == [["espanso", "restart"]]
    result = _cli("deploy", "--yes", *_where(espanso))
    assert "Nothing to do" in result.stdout
    assert fake_run.calls == [["espanso", "restart"]]  # no second restart


def test_cli_deploy_asks_first(espanso: Path, fake_run: FakeRunner) -> None:
    result = _cli("deploy", *_where(espanso), input="n\n")
    assert result.exit_code == 1
    assert not list((espanso / "match").iterdir())
    assert fake_run.calls == []


def test_cli_deploy_yes_keeps_modified(espanso: Path, fake_run: FakeRunner) -> None:
    deploy.apply(_plan(espanso))
    target = _edit(espanso)
    result = _cli("deploy", "--yes", *_where(espanso))
    assert result.exit_code == 0
    assert "modified  prompts-llm.yml" in result.stdout
    assert "-# my tweak" in result.stdout
    assert "# my tweak" in target.read_text("utf-8")


def test_cli_deploy_interactive_choice(espanso: Path, fake_run: FakeRunner) -> None:
    deploy.apply(_plan(espanso))
    target = _edit(espanso)
    result = _cli("deploy", *_where(espanso), input="ours\ny\n")
    assert result.exit_code == 0, result.output
    assert "# my tweak" not in target.read_text("utf-8")
    assert "backed up to prompts-llm.yml.bak-" in result.stdout


def test_cli_deploy_on_conflict_flag(espanso: Path, fake_run: FakeRunner) -> None:
    deploy.apply(_plan(espanso))
    _edit(espanso)
    result = _cli("deploy", "--yes", "--on-conflict", "side", *_where(espanso))
    assert result.exit_code == 0
    assert (espanso / "match" / ("prompts-llm.yml" + deploy.SIDE_SUFFIX)).exists()
    assert fake_run.calls == []  # nothing changed, so no restart


def test_cli_deploy_no_restart(espanso: Path, fake_run: FakeRunner) -> None:
    result = _cli("deploy", "--yes", "--no-restart", *_where(espanso))
    assert "not restarted" in result.stdout
    assert fake_run.calls == []


def test_cli_deploy_bad_on_conflict(espanso: Path, fake_run: FakeRunner) -> None:
    result = _cli("deploy", "--yes", "--on-conflict", "merge", *_where(espanso))
    assert result.exit_code == 1
    assert "--on-conflict must be one of" in result.stderr


def test_cli_deploy_unsafe_launcher(espanso: Path, fake_run: FakeRunner) -> None:
    result = _cli("deploy", "--yes", "--espanso-dir", str(espanso), "--launcher", '/a"b/pw')
    assert result.exit_code == 1
    assert "error: The CLI path contains" in result.stderr


def test_cli_restart_failure_is_reported(espanso: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploy, "run_command", FakeRunner())
    result = _cli("deploy", "--yes", *_where(espanso))
    assert result.exit_code == 0
    assert "Could not restart Espanso" in result.stderr


def test_cli_resolves_launcher_and_espanso_dir(
    espanso: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = _exe(tmp_path / "pipx" / "bin" / EXE)
    monkeypatch.setattr(sys, "argv", [str(script)])
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(
        deploy,
        "run_command",
        FakeRunner({"espanso path config": str(espanso), "espanso restart": ""}),
    )
    result = _cli("status")
    assert result.exit_code == 0, result.output
    assert f"Launcher (script): {script}" in result.stdout
    assert f"Espanso match folder: {espanso / 'match'}" in result.stdout


def test_cli_status_diff(espanso: Path, fake_run: FakeRunner) -> None:
    result = _cli("status", "--diff", *_where(espanso))
    assert result.exit_code == 0
    assert "+# promptmend" in result.stdout
    # Nothing deployed yet: the header says so instead of "on disk" (#222).
    assert "prompts-llm.yml (missing)" in result.stdout
    assert "(on disk)" not in result.stdout
    assert fake_run.calls == []  # status never restarts or writes


def test_cli_status_bad_manifest(espanso: Path, fake_run: FakeRunner) -> None:
    path = user_data_dir() / deploy.MANIFEST_NAME
    path.parent.mkdir(parents=True)
    path.write_text("{", "utf-8")
    result = _cli("status", *_where(espanso))
    assert result.exit_code == 1
    assert "cannot be read" in result.stderr


def _your_files(espanso: Path) -> None:
    """Espanso's own base.yml, a user overlay, and what is not a match file of the user's:
    our backup and side-by-side copy, a note, a package subfolder."""
    match = espanso / "match"
    (match / "base.yml").write_text("matches:\n  - trigger: ':me'\n", "utf-8")
    (match / "work.YAML").write_text("matches: []\n", "utf-8")
    (match / "prompts-llm.yml.bak-20260101000000").write_text("old", "utf-8")
    (match / "prompts-llm.yml.promptmend-new").write_text("new", "utf-8")
    (match / "notes.txt").write_text("notes", "utf-8")
    (match / "packages" / "pkg.yml").mkdir(parents=True)


# #38 (O2): status also names the user's own match files; nothing else lists or touches them.
def test_plan_lists_your_match_files(espanso: Path) -> None:
    _your_files(espanso)
    assert [p.name for p in _plan(espanso).yours] == ["base.yml", "work.YAML"]
    deploy.apply(_plan(espanso))  # our files now exist too: still not listed
    assert [p.name for p in _plan(espanso).yours] == ["base.yml", "work.YAML"]


def test_plan_lists_nothing_without_a_match_folder(tmp_path: Path) -> None:
    assert deploy.plan(tmp_path / "none", LAUNCHER, deploy.Manifest.load()).yours == []


# The pre-0.9 base.yml is no longer retired (#246): it is the user's, and deploy leaves it.
def test_pre_0_9_base_yml_is_yours(espanso: Path) -> None:
    base = espanso / "match" / "base.yml"
    base.write_text('trigger: "-p-" promptmend', "utf-8")
    assert _plan(espanso).yours == [base]
    deploy.apply(_plan(espanso))
    assert base.read_text("utf-8") == 'trigger: "-p-" promptmend'


def test_symlinked_match_file_is_listed_as_yours(tmp_path: Path, espanso: Path) -> None:
    real = tmp_path / "dotfiles" / "mine.yml"
    real.parent.mkdir()
    real.write_text("matches: []\n", "utf-8")
    _symlink(espanso / "match" / "mine.yml", real)
    _symlink(espanso / "match" / "gone.yml", tmp_path / "missing.yml")
    assert [p.name for p in _plan(espanso).yours] == ["gone.yml", "mine.yml"]
    result = _cli("status", *_where(espanso))
    assert "  yours     mine.yml (not managed, a link; never touched" in result.stdout


def test_cli_status_lists_yours_and_deploy_detach_leave_them(
    espanso: Path, fake_run: FakeRunner
) -> None:
    _your_files(espanso)
    before = _tree(espanso)
    result = _cli("status", *_where(espanso))
    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.startswith("  yours")]
    assert lines == [
        "  yours     base.yml (not managed; never touched by deploy or detach)",
        "  yours     work.YAML (not managed; never touched by deploy or detach)",
    ]
    assert fake_run.calls == []
    assert _tree(espanso) == before
    assert _cli("deploy", "--yes", *_where(espanso)).exit_code == 0
    assert _cli("detach", "--remove-all", "--yes", "--espanso-dir", str(espanso)).exit_code == 0
    assert _tree(espanso) == before


def test_cli_detach(espanso: Path, fake_run: FakeRunner) -> None:
    where = ["--espanso-dir", str(espanso)]
    assert "Nothing to do" in _cli("detach", "--yes", *where).stdout
    _cli("deploy", "--yes", *_where(espanso))
    assert _cli("detach", *where, input="n\n").exit_code == 1
    result = _cli("detach", "--yes", *where)
    assert result.exit_code == 0, result.output
    assert [p.name for p in (espanso / "match").iterdir()] == [STATIC]
    result = _cli("detach", "--remove-all", "--yes", "--no-restart", *where)
    assert not list((espanso / "match").iterdir())


def test_cli_detach_bad_manifest(fake_run: FakeRunner) -> None:
    path = user_data_dir() / deploy.MANIFEST_NAME
    path.parent.mkdir(parents=True)
    path.write_text("{", "utf-8")
    result = _cli("detach", "--yes")
    assert result.exit_code == 1


def test_cli_deploy_asks_again_after_a_bad_answer(espanso: Path, fake_run: FakeRunner) -> None:
    deploy.apply(_plan(espanso))
    target = _edit(espanso)
    result = _cli("deploy", *_where(espanso), input="merge\n\ny\n")
    assert result.exit_code == 0, result.output
    assert "Answer one of keep, ours, side." in result.stdout
    assert "# my tweak" in target.read_text("utf-8")  # the empty answer is keep


# A file that already is today's rendering but is not on record (a lost manifest) is adopted.
def test_in_sync_file_without_manifest_is_adopted(espanso: Path) -> None:
    deploy.apply(_plan(espanso))
    (user_data_dir() / deploy.MANIFEST_NAME).unlink()
    before = _tree(espanso)
    the_plan = _plan(espanso)
    assert set(_states(espanso).values()) == {deploy.IN_SYNC}
    assert not the_plan.is_noop
    outcome = deploy.apply(the_plan)
    assert not outcome.changed
    assert all(line.startswith("adopted") for line in outcome.lines)
    assert _tree(espanso) == before
    assert _plan(espanso).is_noop


# --- Hardening (#104 review) ----------------------------------------------------------------

_hist_spec = importlib.util.spec_from_file_location(
    "update_match_history", REPO / "scripts" / "update_match_history.py"
)
assert _hist_spec
assert _hist_spec.loader
history_script = importlib.util.module_from_spec(_hist_spec)
_hist_spec.loader.exec_module(history_script)


def _git_tags() -> bool:
    git = shutil.which("git")
    assert git
    if git is None:
        return False
    tags = subprocess.run(
        [git, "tag", "--list", "v*"], cwd=REPO, capture_output=True, text=True, timeout=30
    )
    return tags.returncode == 0 and bool(tags.stdout.split())


# Every current source is listed, so an editable install's own deploy is recognised too.
def test_match_history_has_the_current_sources() -> None:
    for name, digests in history_script.current_digests().items():
        assert digests <= KNOWN_SOURCES[name], (
            f"{name} changed: run `uv run python scripts/update_match_history.py`"
        )


# Every release, and every commit that changed a match file on this branch or the default
# branch (an editable install runs an untagged commit), is recognised.
@pytest.mark.skipif(not _git_tags(), reason="needs a git checkout with the release tags")
def test_match_history_has_every_release_and_commit() -> None:
    for source in (history_script.tagged_digests(), history_script.history_digests()):
        for name, digests in source.items():
            assert digests <= KNOWN_SOURCES.get(name, frozenset()), name


# Regenerating never drops a digest the module already lists.
def test_match_history_regeneration_keeps_listed_digests(monkeypatch: pytest.MonkeyPatch) -> None:
    for source in ("tagged_digests", "history_digests", "current_digests"):
        monkeypatch.setattr(history_script, source, dict)
    collected = history_script.collect()
    assert {name: frozenset(found) for name, found in collected.items()} == KNOWN_SOURCES


@pytest.mark.skipif(not _git_tags(), reason="needs a git checkout with the release tags")
@pytest.mark.parametrize("windows", [False, True], ids=["macos", "windows"])
def test_file_an_old_script_wrote_is_stale(espanso: Path, windows: bool) -> None:
    git = shutil.which("git")
    assert git
    old = subprocess.run(
        [git, "show", "v0.14.0:espanso/match/prompts-llm.yml"],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    ).stdout
    cli = r"C:\Users\me\.local\bin\promptmend.exe" if windows else "/Users/me/bin/promptmend"
    render = _script_render_windows if windows else _script_render_macos
    (espanso / "match" / "prompts-llm.yml").write_text(render(old, cli), "utf-8")
    assert _states(espanso)["prompts-llm.yml"] == deploy.STALE


# #38 dropped -prompt- from prompts-core.yml: the file 0.18.0 deployed is ours and stale, so
# the next deploy replaces it without a conflict, with or without its manifest entry.
# Rebuilt rather than read with `git show v0.18.0:...`, since CI's checkout has no tags.
_PROMPT_FORM_0_18 = """\
  - trigger: "-prompt-"
    label: "prompt-workflow: prompt form (role, objective, context)"
    left_word: true
    form: |
      Act as {{role}}.

      Objective:
      {{objective}}

      Context:
      {{context}}

      Constraints:
      {{constraints}}

      Required output:
      {{output}}

      Validate assumptions and identify missing information.
    form_fields:
      role:
        multiline: false
      objective:
        multiline: true
      context:
        multiline: true
      constraints:
        multiline: true
      output:
        multiline: true

"""
_CORE_0_18 = "1cf316c0745bbdc9827643530caacf3b782a59cb72312ccfebcfccd4ea73c656"


def _as_0_18(source: str) -> str:
    """A match source as 0.18.0 had it: the rename (#169) changed only the labels and the
    command named in comments."""
    return source.replace('label: "PromptMend: ', 'label: "prompt-workflow: ').replace(
        "promptmend", "prompt-workflow"
    )


# The rename (#169): a file 0.18.0 deployed (its `# prompt-workflow` stamp, its labels, its
# `prompt-workflow` launcher) is ours and stale, with or without its manifest entry, and the
# next deploy replaces it with the `promptmend` launcher without a conflict.
@pytest.mark.parametrize("name", ["prompts-llm.yml", "prompts-template.yml"])
@pytest.mark.parametrize("manifest", [True, False])
def test_a_file_0_18_deployed_is_stale(
    monkeypatch: pytest.MonkeyPatch, espanso: Path, name: str, manifest: bool
) -> None:
    source = _as_0_18((SHELL_0_20 / name).read_text("utf-8"))
    assert hashlib.sha256(source.encode()).hexdigest() in KNOWN_SOURCES[name]
    old_launcher = "/Users/me/.local/bin/prompt-workflow"
    rendered = "# prompt-workflow 0.18.0 (managed; edit at your own risk)\n" + source.replace(
        deploy.PLACEHOLDER, old_launcher
    )
    target = espanso / "match" / name
    target.write_text(rendered, encoding="utf-8")
    if manifest:
        entry = deploy.Entry(
            target=str(target),
            asset_version="0.18.0",
            digest=hashlib.sha256(rendered.encode()).hexdigest(),
            launcher=old_launcher,
            calls_cli=True,
        )
        manifest_file = deploy.Manifest.load()
        manifest_file.entries[str(target)] = entry
        manifest_file.save()
    assert _states(espanso)[name] == deploy.STALE
    outcome = deploy.apply(_plan(espanso))
    assert not outcome.kept
    text = target.read_text("utf-8")
    assert text.startswith(f"# promptmend {VERSION} (managed; edit at your own risk)\n")
    assert old_launcher not in text
    assert LAUNCHER in text
    assert not list(target.parent.glob(f"{name}.bak-*"))


# #18: a file 0.20.0 deployed (shell vars, the launcher quoted inside the cmd line) is ours
# and stale, with or without its manifest entry, on either OS, and the next deploy replaces
# it with script vars that start with the launcher, without a conflict or a backup.
@pytest.mark.parametrize("name", ["prompts-llm.yml", "prompts-template.yml"])
@pytest.mark.parametrize("manifest", [True, False], ids=["manifest", "bare"])
@pytest.mark.parametrize("windows", [False, True], ids=["posix", "win"])
def test_a_file_0_20_deployed_is_replaced_by_script_vars(
    espanso: Path, name: str, manifest: bool, windows: bool
) -> None:
    source = (SHELL_0_20 / name).read_text("utf-8")
    assert hashlib.sha256(source.encode()).hexdigest() in KNOWN_SOURCES[name]
    assert "type: shell" in source
    raw = r"C:\Users\Jan Novák\.local\bin\promptmend.exe" if windows else LAUNCHER
    launcher = deploy.launcher_text(raw, windows=windows)
    rendered = "# promptmend 0.20.0 (managed; edit at your own risk)\n" + source.replace(
        deploy.PLACEHOLDER, launcher
    )
    target = espanso / "match" / name
    target.write_text(rendered, encoding="utf-8")
    assert deploy.launchers_in(rendered) == {launcher}
    if manifest:
        manifest_file = deploy.Manifest.load()
        manifest_file.entries[str(target)] = deploy.Entry(
            target=str(target),
            asset_version="0.20.0",
            digest=hashlib.sha256(rendered.encode()).hexdigest(),
            launcher=launcher,
            calls_cli=True,
        )
        manifest_file.save()
    assert _states(espanso, launcher)[name] == deploy.STALE
    outcome = deploy.apply(_plan(espanso, launcher))
    assert not outcome.kept
    text = target.read_text("utf-8")
    assert text == _plan(espanso, launcher).steps[NAMES.index(name)].rendered
    assert "    type: shell" not in text
    assert f'args: ["{launcher}", ' in text
    assert not list(target.parent.glob(f"{name}.bak-*"))
    assert _plan(espanso, launcher).is_noop


@pytest.mark.parametrize("manifest", [True, False])
def test_0_18_prompts_core_with_prompt_form_is_replaced(
    monkeypatch: pytest.MonkeyPatch, espanso: Path, manifest: bool
) -> None:
    old = _as_0_18((MATCH / STATIC).read_text("utf-8")).replace(
        "matches:\n", "matches:\n" + _PROMPT_FORM_0_18, 1
    )
    assert hashlib.sha256(old.encode()).hexdigest() == _CORE_0_18  # v0.18.0's file, exactly
    assert _CORE_0_18 in KNOWN_SOURCES[STATIC]
    real = assets.read_match
    monkeypatch.setattr(deploy, "__version__", "0.18.0")
    monkeypatch.setattr(assets, "read_match", lambda name: old if name == STATIC else real(name))
    deploy.apply(_plan(espanso))
    monkeypatch.setattr(deploy, "__version__", VERSION)
    monkeypatch.setattr(assets, "read_match", real)
    if not manifest:
        (user_data_dir() / deploy.MANIFEST_NAME).unlink()
    assert _states(espanso)[STATIC] == deploy.STALE
    outcome = deploy.apply(_plan(espanso))
    assert not outcome.kept
    target = espanso / "match" / STATIC
    assert '"-prompt-"' not in target.read_text("utf-8")
    assert not list(target.parent.glob(f"{STATIC}.bak-*"))
    assert _plan(espanso).is_noop


def _old_release(monkeypatch: pytest.MonkeyPatch, name: str = "prompts-llm.yml") -> str:
    """An 'older release' of a match file, registered in the history like a real one."""
    old = 'matches:\n  - trigger: "-old-"\n    cmd: "\\"__PROMPT_WORKFLOW__\\" improve"\n'
    digest = hashlib.sha256(old.encode()).hexdigest()
    monkeypatch.setitem(KNOWN_SOURCES, name, KNOWN_SOURCES[name] | {digest})
    return old


@pytest.mark.parametrize("launcher", ["/Users/me/old/promptmend", "C:/Users/me/old/promptmend.exe"])
def test_older_release_with_another_launcher_is_stale(
    monkeypatch: pytest.MonkeyPatch, espanso: Path, launcher: str
) -> None:
    old = _old_release(monkeypatch)
    target = espanso / "match" / "prompts-llm.yml"
    target.write_text(old.replace("__PROMPT_WORKFLOW__", launcher), "utf-8")
    assert _states(espanso)["prompts-llm.yml"] == deploy.STALE
    # ...also behind a stamp an earlier deploy wrote.
    stamped = "# promptmend 0.1.0 (managed; edit at your own risk)\n" + target.read_text("utf-8")
    target.write_text(stamped, "utf-8")
    assert _states(espanso)["prompts-llm.yml"] == deploy.STALE
    outcome = deploy.apply(_plan(espanso))
    assert not outcome.kept
    assert target.read_text("utf-8") == _plan(espanso).steps[1].rendered


def test_edited_older_release_is_foreign(monkeypatch: pytest.MonkeyPatch, espanso: Path) -> None:
    old = _old_release(monkeypatch)
    edited = old.replace("__PROMPT_WORKFLOW__", "/x/promptmend") + "# mine\n"
    (espanso / "match" / "prompts-llm.yml").write_text(edited, "utf-8")
    assert _states(espanso)["prompts-llm.yml"] == deploy.FOREIGN


def test_cli_warns_about_kept_files(espanso: Path, fake_run: FakeRunner) -> None:
    (espanso / "match" / STATIC).write_text("matches: []\n", "utf-8")
    result = _cli("deploy", "--yes", *_where(espanso))
    assert result.exit_code == 0
    assert "WARNING: 1 match file(s) kept as you have them and NOT updated: " in result.stderr
    assert STATIC in result.stderr
    assert "up to date" not in result.stdout
    result = _cli("deploy", "--yes", "--on-conflict", "ours", *_where(espanso))
    assert "WARNING" not in result.stderr
    assert result.stdout.endswith("The match files are up to date.\n")


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("backups", "abc", "backups is not a list"),
        ("backups", [1], "backups is not a list of paths"),
        ("target", 5, "target is not a str"),
        ("calls_cli", "yes", "calls_cli is not a bool"),
        ("extra", 1, "has the fields"),
    ],
)
def test_manifest_field_types(
    espanso: Path, field: str, value: str | int | list[int], error: str
) -> None:
    deploy.apply(_plan(espanso))
    path = user_data_dir() / deploy.MANIFEST_NAME
    data = json.loads(path.read_text("utf-8"))
    data["files"][0][field] = value
    path.write_text(json.dumps(data), "utf-8")
    with pytest.raises(deploy.DeployError, match=error):
        deploy.Manifest.load()
    result = _cli("status", *_where(espanso))
    assert result.exit_code == 1
    assert "is damaged" in result.stderr


@pytest.mark.parametrize("files", ['"x"', '["x"]'])
def test_manifest_files_shape(files: str) -> None:
    path = user_data_dir() / deploy.MANIFEST_NAME
    path.parent.mkdir(parents=True)
    path.write_text(f'{{"format": 1, "files": {files}}}', "utf-8")
    with pytest.raises(deploy.DeployError, match="is damaged"):
        deploy.Manifest.load()


# A listed backup that is not one of ours (elsewhere, or not named like one) is never deleted.
def test_prune_deletes_only_our_backups(tmp_path: Path, espanso: Path) -> None:
    target = espanso / "match" / "prompts-llm.yml"
    elsewhere = tmp_path / "prompts-llm.yml.bak-20260101000000"
    misnamed = espanso / "match" / "prompts-core.yml.bak-20260101000000"
    ours = [espanso / "match" / f"prompts-llm.yml.bak-2026010100000{n}" for n in range(1, 5)]
    for path in [elsewhere, misnamed, *ours]:
        path.write_text("x", "utf-8")
    kept = deploy._prune([str(elsewhere), str(misnamed), *map(str, ours)], target)
    assert kept == [str(p) for p in ours[-2:]]
    assert elsewhere.exists()
    assert misnamed.exists()
    assert [p.exists() for p in ours] == [False, False, True, True]


def test_detach_only_touches_our_files_in_the_match_folder(tmp_path: Path, espanso: Path) -> None:
    deploy.apply(_plan(espanso))
    manifest = deploy.Manifest.load()
    outside = tmp_path / "important.txt"
    outside.write_text("keep me", "utf-8")
    elsewhere = tmp_path / "other" / "match" / "prompts-llm.yml"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("keep me too", "utf-8")
    for path in (outside, elsewhere):
        manifest.entries[str(path)] = deploy.Entry(
            str(path), VERSION, deploy._digest(path.read_text("utf-8")), LAUNCHER, True
        )
    outcome = deploy.detach(manifest, espanso, remove_all=True)
    assert outside.read_text("utf-8") == "keep me"
    assert elsewhere.read_text("utf-8") == "keep me too"
    assert sum("left" in line and "alone" in line for line in outcome.lines) == 2
    assert not list((espanso / "match").iterdir())


def test_relative_espanso_dir_is_stored_absolute(tmp_path: Path, fake_run: FakeRunner) -> None:
    (tmp_path / "rel" / "match").mkdir(parents=True)  # conftest chdirs into tmp_path
    result = _cli("deploy", "--yes", "--espanso-dir", "rel", "--launcher", LAUNCHER)
    assert result.exit_code == 0, result.output
    targets = list(deploy.Manifest.load().entries)
    assert targets
    assert all(Path(t).is_absolute() for t in targets)
    assert all(Path(t).parent == (tmp_path / "rel" / "match").resolve() for t in targets)


def _symlink(link: Path, to: Path) -> None:
    try:
        link.symlink_to(to)
    except OSError:  # Windows without the symlink privilege
        pytest.skip("symlinks are not available")


def test_symlinked_target_is_foreign_and_kept(tmp_path: Path, espanso: Path) -> None:
    real = tmp_path / "dotfiles" / "prompts-llm.yml"
    real.parent.mkdir()
    real.write_text("mine", "utf-8")
    link = espanso / "match" / "prompts-llm.yml"
    _symlink(link, real)
    assert _states(espanso)["prompts-llm.yml"] == deploy.FOREIGN
    deploy.apply(_plan(espanso))
    assert link.is_symlink()
    assert real.read_text("utf-8") == "mine"


def test_writes_never_follow_a_planted_link(tmp_path: Path, espanso: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("safe", "utf-8")
    match = espanso / "match"
    _symlink(match / "prompts-llm.yml.tmp", victim)  # the old fixed temp name
    deploy.apply(_plan(espanso))
    assert victim.read_text("utf-8") == "safe"
    assert not [p for p in match.iterdir() if p.name.startswith(".")]  # no temp left over


def test_backup_skips_a_planted_link(tmp_path: Path, espanso: Path) -> None:
    victim = tmp_path / "victim.txt"
    target = espanso / "match" / "prompts-llm.yml"
    target.write_text("mine", "utf-8")
    _symlink(target.with_name("prompts-llm.yml.bak-20260101000000"), victim)  # dangling
    backup = deploy._backup(target, "20260101000000")
    assert backup.name == "prompts-llm.yml.bak-20260101000000-1"
    assert backup.read_text("utf-8") == "mine"
    assert not victim.exists()


def test_backup_names_run_out(monkeypatch: pytest.MonkeyPatch, espanso: Path) -> None:
    target = espanso / "match" / "a.yml"
    target.write_text("x", "utf-8")
    monkeypatch.setattr(Path, "is_symlink", lambda self: True)
    with pytest.raises(deploy.DeployError, match="free backup name"):
        deploy._backup(target, "20260101000000")


def test_write_cleans_up_on_failure(monkeypatch: pytest.MonkeyPatch, espanso: Path) -> None:
    def boom(src: Any, dst: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        deploy._write(espanso / "match" / "a.yml", "x")
    assert not list((espanso / "match").iterdir())


def test_detach_leaves_a_link_alone(tmp_path: Path, espanso: Path) -> None:
    deploy.apply(_plan(espanso))
    link = espanso / "match" / "prompts-llm.yml"
    real = tmp_path / "real.yml"
    real.write_bytes(link.read_bytes())  # same digest as ours
    link.unlink()
    _symlink(link, real)
    outcome = deploy.detach(deploy.Manifest.load(), espanso)
    assert link.is_symlink()
    assert real.exists()
    assert any("it is a link" in line for line in outcome.lines)


# Before v0.11 a Windows checkout had CRLF sources, which the Windows script copied as they were.
def test_crlf_file_an_old_windows_script_wrote_is_stale(
    monkeypatch: pytest.MonkeyPatch, espanso: Path
) -> None:
    old = _old_release(monkeypatch)
    rendered = old.replace("__PROMPT_WORKFLOW__", "C:/Users/me/promptmend.exe")
    target = espanso / "match" / "prompts-llm.yml"
    target.write_bytes(rendered.replace("\n", "\r\n").encode("utf-8"))
    assert _states(espanso)["prompts-llm.yml"] == deploy.STALE
    target.write_bytes(("\ufeff" + rendered).encode("utf-8"))
    assert _states(espanso)["prompts-llm.yml"] == deploy.STALE


def test_triggers_in_reads_a_deployed_file() -> None:
    """The deployed text has the launcher where the shipped one has the placeholder (#217)."""
    text = deploy.render(assets.read_match("prompts-llm.yml"), "/opt/bin/promptmend")
    assert assets.triggers_in(text, "prompts-llm.yml") == [
        t for t in assets.triggers() if t.file == "prompts-llm.yml"
    ]
