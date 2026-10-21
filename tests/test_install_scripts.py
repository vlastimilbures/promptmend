"""The install scripts, read as text only (never run: they install a uv tool, and the
contributor ones deploy into the real Espanso), and the rendering of the Windows user
installer, scripts/install.ps1, that the release workflow attaches (#186)."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if TYPE_CHECKING:
    import render_installer
else:
    render_installer = _load("render_installer")

INSTALL = SCRIPTS / "install.ps1"
ASSET = "install.ps1"


def _lines() -> list[str]:
    """install.ps1 without comments and blank lines."""
    lines = [line.split("#", 1)[0].strip() for line in INSTALL.read_text("utf-8").splitlines()]
    return [line for line in lines if line]


def _code_lines() -> list[str]:
    """Without the lines that only print or throw, or continue such a message."""
    printed = ("Write-", "throw", "'", '"')
    return [line for line in _lines() if not line.startswith(printed)]


# The release workflow fills in the version: the placeholder is there exactly once, and the
# script refuses to run unrendered (its own check spells the placeholder in two parts).
def test_placeholder() -> None:
    text = INSTALL.read_text("utf-8")
    assert text.count(render_installer.PLACEHOLDER) == 1
    assert "'__PROMPTMEND_' + 'VERSION__'" in text
    rendered = render_installer.render(text, "1.2.3")
    assert "$ReleaseVersion = '1.2.3'" in rendered
    assert render_installer.PLACEHOLDER not in rendered


@pytest.mark.parametrize("version", ["1.2", "v1.2.3", "1.2.3; rm", ""])
def test_render_bad_version(version: str) -> None:
    with pytest.raises(ValueError, match=r"X\.Y\.Z"):
        render_installer.render(INSTALL.read_text("utf-8"), version)


def test_render_needs_one_placeholder() -> None:
    with pytest.raises(ValueError, match="0 times"):
        render_installer.render("nothing", "1.2.3")


def test_render_cli(tmp_path: Path) -> None:
    out = tmp_path / ASSET
    render_installer.main(["0.20.0", str(out)])
    assert "$ReleaseVersion = '0.20.0'" in out.read_text("ascii")
    with pytest.raises(SystemExit, match="render_installer: "):
        render_installer.main(["0.20", str(out)])


# Windows PowerShell 5.1 reads a file without a byte order mark in the ANSI code page.
def test_ascii() -> None:
    INSTALL.read_bytes().decode("ascii")


def test_release_renders() -> None:
    workflow = (REPO / ".github" / "workflows" / "release.yml").read_text("utf-8")
    assert f'scripts/render_installer.py "$VERSION" dist/{ASSET}' in workflow
    # dist/* is what the release job attests and attaches, the wheel among them under the
    # name install.ps1 downloads (the pypi job picks the same file).
    assert "dist/*" in workflow
    assert '"promptmend-$VERSION-py3-none-any.whl"' in workflow


# Every install compiles the bytecode then, where a wait is expected, rather than on the first
# run, which takes seconds on a machine that scans each new file (#216).
@pytest.mark.parametrize("name", ["install.ps1", "install_macos.sh", "install_windows.ps1"])
def test_install_compiles_bytecode(name: str) -> None:
    text = (SCRIPTS / name).read_text("utf-8")
    calls = re.findall(r"(?m)^\s*(?:uv tool install|\$InstallArgs = @\('tool', 'install').*$", text)
    assert calls
    assert all("--compile-bytecode" in call for call in calls), calls


def test_readme_url() -> None:
    readme = (REPO / "README.md").read_text("utf-8")
    url = "https://github.com/vlastimilbures/promptmend/releases/latest/download/"
    assert f'powershell -ExecutionPolicy ByPass -c "irm {url}{ASSET} | iex"' in readme
    assert f"{url}{ASSET}" in INSTALL.read_text("utf-8")


# Setup and the deploy are only named as the next step, never run.
def test_never_deploys() -> None:
    code = "\n".join(_code_lines())
    assert "doctor" in code, "the filter dropped the code lines"
    for word in ("setup", "deploy", "espanso", "detach"):
        assert word not in code.lower(), word
    printed = INSTALL.read_text("utf-8")
    assert "Write-Host '  promptmend                   # the interface" in printed
    assert "promptmend setup" in printed
    assert "promptmend espanso deploy" in printed


# The wheel and constraints.txt attached to the same Release (attested, there before PyPI's).
def test_install_command() -> None:
    code = "\n".join(_lines())
    assert '$Download = "$Repo/releases/download/v$Version"' in code
    assert '$Spec = "$Download/promptmend-$Version-py3-none-any.whl"' in code
    assert '$Constraints = "$Download/constraints.txt"' in code
    assert (
        "$InstallArgs = @('tool', 'install', '--force', '--compile-bytecode', $Spec, "
        "'-c', $Constraints)" in code
    )
    assert "Assert-Native 'uv' $InstallArgs" in code
    assert "promptmend==" not in code
    assert "@('tool', 'update-shell')" in code
    assert "@('doctor', '--no-clipboard')" in code
    assert "'--id', 'astral-sh.uv', '-e'" in code
    assert "'--source', 'winget'" in code
    assert "$WingetAlreadyInstalled = -1978335189" in code
    assert "'irm https://astral.sh/uv/install.ps1 | iex'" in code
    for override in ("VERSION", "WHEEL", "CONSTRAINTS", "DRY_RUN"):
        assert f"$env:PROMPTMEND_{override}" in code
    assert "Set-StrictMode -Version Latest" in code
    assert "$ErrorActionPreference = 'Stop'" in code
    assert "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false" in code
    # `exit` would close the window of a caller who ran `irm | iex` in their own session.
    assert not re.search(r"(?m)^exit\b", code)


# uv is looked for only after PATH is refreshed, and the dry run stops before any uv call.
def test_order() -> None:
    code = "\n".join(_lines())
    refresh = code.index("Update-SessionPath $guesses")
    assert refresh < code.index("Get-Command uv")
    assert code.index("$env:PROMPTMEND_DRY_RUN") < refresh


def test_version_check() -> None:
    code = "\n".join(_lines())
    assert r"$Version -cnotmatch '^[0-9]+\.[0-9]+\.[0-9]+([a-z]+[0-9]+)?\z'" in code
    assert "$Oldest = [version]'0.19.0'" in code
    assert "-lt $Oldest" in code


def _anchors(page: Path) -> set[str]:
    """GitHub's anchor for each heading of a Markdown page."""
    headings = re.findall(r"^#{1,6} (.+)$", page.read_text("utf-8"), re.MULTILINE)
    return {re.sub(r"[^\w\- ]", "", h.strip().lower()).replace(" ", "-") for h in headings}


# install.ps1 runs outside a checkout (`irm | iex`), so every docs page it names is an
# absolute GitHub URL; the checkout scripts name a docs page by its path. Each page and
# anchor exists.
@pytest.mark.parametrize("name", ["install.ps1", "install_macos.sh", "install_windows.ps1"])
def test_scripts_point_at_existing_docs(name: str) -> None:
    text = (SCRIPTS / name).read_text("utf-8")
    refs = re.findall(r"(\S*)docs/([a-z-]+\.md)(?:#([\w-]+))?", text)
    assert refs, f"{name} names no docs page"
    for prefix, page, anchor in refs:
        if name == "install.ps1":
            assert prefix.endswith("$Repo/blob/main/"), f"{name}: docs/{page} is not absolute"
        else:
            assert prefix in ("", "("), f"{name}: docs/{page}"
        assert (REPO / "docs" / page).is_file(), f"{name}: no docs/{page}"
        if anchor:
            assert anchor in _anchors(REPO / "docs" / page), f"{name}: no #{anchor} in {page}"
