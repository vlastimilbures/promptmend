"""The install scripts, read as text only (never run: they install a uv tool or the frozen
exe, and the contributor ones deploy into the real Espanso), and the rendering of the Windows
user installer, scripts/install.ps1, that the release workflow attaches (#186, #185)."""

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
HASH = "0123456789abcdef" * 4


def _lines() -> list[str]:
    """install.ps1 without comments and blank lines."""
    lines = [line.split("#", 1)[0].strip() for line in INSTALL.read_text("utf-8").splitlines()]
    return [line for line in lines if line]


def _code_lines() -> list[str]:
    """Without the lines that only print or throw, or continue such a message."""
    printed = ("Write-", "throw", "'", '"')
    return [line for line in _lines() if not line.startswith(printed)]


# The release workflow fills in the version and the zip's hash: each placeholder is there
# exactly once, and the script refuses to run unrendered (its own checks spell the
# placeholders in two parts).
def test_placeholder() -> None:
    text = INSTALL.read_text("utf-8")
    for placeholder in (render_installer.PLACEHOLDER, render_installer.ZIP_PLACEHOLDER):
        assert text.count(placeholder) == 1
    assert "'__PROMPTMEND_' + 'VERSION__'" in text
    assert "'__PROMPTMEND_' + 'ZIP_SHA256__'" in text
    rendered = render_installer.render(text, "1.2.3", HASH)
    assert "$ReleaseVersion = '1.2.3'" in rendered
    assert f"$ReleaseZipSha256 = '{HASH}'" in rendered
    assert render_installer.PLACEHOLDER not in rendered
    assert render_installer.ZIP_PLACEHOLDER not in rendered


@pytest.mark.parametrize("version", ["1.2", "v1.2.3", "1.2.3; rm", ""])
def test_render_bad_version(version: str) -> None:
    with pytest.raises(ValueError, match=r"X\.Y\.Z"):
        render_installer.render(INSTALL.read_text("utf-8"), version, HASH)


@pytest.mark.parametrize("digest", ["", HASH.upper(), HASH[:-1], HASH + "0", "g" * 64, "' + x"])
def test_render_bad_hash(digest: str) -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        render_installer.render(INSTALL.read_text("utf-8"), "1.2.3", digest)


def test_render_needs_one_placeholder() -> None:
    with pytest.raises(ValueError, match="0 times"):
        render_installer.render("nothing", "1.2.3", HASH)
    with pytest.raises(ValueError, match="__PROMPTMEND_ZIP_SHA256__ appears 2 times"):
        render_installer.render(
            "__PROMPTMEND_VERSION__ __PROMPTMEND_ZIP_SHA256__ __PROMPTMEND_ZIP_SHA256__",
            "1.2.3",
            HASH,
        )


def test_render_cli(tmp_path: Path) -> None:
    import hashlib

    out = tmp_path / ASSET
    archive = tmp_path / "promptmend-0.20.0-windows-x64.zip"
    archive.write_bytes(b"zip bytes" * 300_000)  # more than one read block
    render_installer.main(["0.20.0", str(out), "--zip", str(archive)])
    text = out.read_text("ascii")
    assert "$ReleaseVersion = '0.20.0'" in text
    assert f"$ReleaseZipSha256 = '{hashlib.sha256(archive.read_bytes()).hexdigest()}'" in text
    with pytest.raises(SystemExit, match="render_installer: "):
        render_installer.main(["0.20", str(out), "--zip", str(archive)])
    with pytest.raises(SystemExit, match=r"render_installer: .*missing\.zip"):
        render_installer.main(["0.20.0", str(out), "--zip", str(tmp_path / "missing.zip")])
    with pytest.raises(SystemExit):  # the zip is required: the default channel needs its hash
        render_installer.main(["0.20.0", str(out)])


# Windows PowerShell 5.1 reads a file without a byte order mark in the ANSI code page.
def test_ascii() -> None:
    INSTALL.read_bytes().decode("ascii")


# install.ps1 is rendered in the job that built the zip, from that very zip, and travels with
# it to the release job, which checks the hash again before attesting and attaching both.
def test_release_renders() -> None:
    workflow = (REPO / ".github" / "workflows" / "release.yml").read_text("utf-8")
    windows = workflow[workflow.index("  build-windows:") : workflow.index("  test-artifacts:")]
    build = windows.index("scripts/build_windows.py build")
    render = windows.index(
        f'scripts/render_installer.py "$env:VERSION" dist/{ASSET} --zip '
        '"dist/promptmend-$env:VERSION-windows-x64.zip"'
    )
    assert build < render < windows.index("- name: Upload the zip")
    assert f"dist/{ASSET}" in windows[render:]
    assert "render_installer" not in workflow[: workflow.index("  build-windows:")]
    release = workflow[workflow.index("  release:") :]
    assert "sha256sum" in release
    assert release.index("$ReleaseZipSha256") < release.index("Attest build provenance")
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
    for override in ("VERSION", "WHEEL", "CONSTRAINTS", "DRY_RUN", "CHANNEL", "ZIP"):
        assert f"$env:PROMPTMEND_{override}" in code
    assert "Set-StrictMode -Version Latest" in code
    assert "$ErrorActionPreference = 'Stop'" in code
    assert "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false" in code
    # `exit` would close the window of a caller who ran `irm | iex` in their own session.
    assert not re.search(r"(?m)^exit\b", code)


def _function(name: str) -> str:
    """The code lines of one of install.ps1's functions, up to the next one."""
    code = "\n".join(_lines())
    match = re.search(rf"^function {re.escape(name)}[ (]", code, re.MULTILINE)
    assert match, name
    start = match.start()
    end = code.find("\nfunction ", start + 1)
    return code[start : end if end != -1 else None]


# uv is looked for only after PATH is refreshed, and the dry run stops before any uv call.
def test_order() -> None:
    code = _function("Install-WithUv")
    refresh = code.index("Update-SessionPath $guesses")
    assert refresh < code.index("Get-Command uv")
    assert code.index("$env:PROMPTMEND_DRY_RUN") < refresh


# The exe channel: the dry run stops before anything is touched; the hash is checked before
# anything is unpacked, the new exe runs once before the swap, and a running exe is refused
# before the download and again right before the swap.
def test_exe_order() -> None:
    code = _function("Install-Exe")
    dry = code.index("$env:PROMPTMEND_DRY_RUN")
    steps = [
        code.index("Assert-NotRunning $Dest"),
        code.index("$uvTool = Find-UvTool"),
        code.index("Remove-Stale $Programs $Dest"),
        code.index("Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Zip"),
        code.index("$Actual = Get-Sha256 $Zip"),
        code.index("if ($Expected -and $Actual -ne $Expected)"),
        code.index("Expand-Zip $Zip $Stage"),
        code.index("& $NewExe --version"),
        code.rindex("Assert-NotRunning $Dest"),
        code.index("Invoke-Retried { Rename-Item -LiteralPath $Dest -NewName $OldName }"),
        code.index("Invoke-Retried { Move-Item -LiteralPath $New -Destination $Dest }"),
        code.index("Invoke-Retried { Rename-Item -LiteralPath $Old -NewName $DestName }"),
        code.index("Invoke-Retried { Remove-Item -LiteralPath $Old -Recurse -Force }"),
        # Interrupted between the two renames: the old folder goes back.
        code.index("if ($hadOld -and -not [IO.Directory]::Exists($Dest)"),
        code.index("Add-UserPath $Dest"),
        code.index("Invoke-Native $Exe @('doctor', '--no-clipboard')"),
        code.index("if ($uvTool) {"),
    ]
    assert dry < steps[0]
    assert steps == sorted(steps)
    assert code.count("Assert-NotRunning $Dest") == 2
    # An unrendered copy says so before it asks for a version.
    assert code.index("if ($ReleaseZipSha256 -eq $UnrenderedHash) {") < code.index(
        "$Version = Get-Version"
    )
    assert "Expand-Archive" not in code


# The zip is unpacked with .NET (it throws; 5.1's Expand-Archive ignores the error
# preference) and every file entry must arrive; renames retry while a scanner holds a file;
# only this script's own leftover folders are swept, never the previous install's only copy.
def test_exe_helpers() -> None:
    unzip = _function("Expand-Zip")
    assert "[IO.Compression.ZipFile]::ExtractToDirectory($Zip, $Folder)" in unzip
    assert "if ($files -ne $entries) {" in unzip
    retried = _function("Invoke-Retried")
    assert "if ($i -ge 10) { throw }" in retried
    assert "Start-Sleep -Milliseconds 500" in retried
    stale = _function("Remove-Stale")
    assert r"'^promptmend\.(new|old)-[0-9a-f]{8}\z'" in stale
    assert "(Get-RunningFrom $dir.FullName).Count -gt 0" in stale
    assert "-not (Test-Path -LiteralPath $Dest)" in stale
    assert "if ($dir.LastWriteTime -gt $recent) { continue }" in stale


# The finally block uses .NET only (5.1 runs no cmdlet there after Ctrl+C), and the new exe's
# start check needs an exit code set by that very call and a version on its stdout.
def test_exe_finally_and_start_check() -> None:
    code = _function("Install-Exe")
    final = code[code.index("} finally {\nif ($hadOld") : code.index("Write-Step 'Adding it")]
    assert "[IO.Directory]::Move($Old, $Dest)" in final
    for cmdlet in ("Rename-Item", "Remove-Item", "Test-Path", "Write-Host", "Write-Warning"):
        assert cmdlet not in final, cmdlet
    start = code.index("$global:LASTEXITCODE = -1")
    assert start < code.index("& $NewExe --version") < code.index("$code = $LASTEXITCODE")
    assert "The new promptmend.exe did not start ($($_.Exception.Message))" in code
    assert "if ($got -cnotmatch '^[0-9]+\\.[0-9]+\\.[0-9]+') {" in code


# With a uv install still there, every next step names the exe by its full path and the bare
# `promptmend` block (which may run the uv CLI) is skipped.
def test_uv_install_found() -> None:
    code = _function("Install-Exe")
    found = code[code.index("if ($uvTool) {") :]
    found = found[: found.index("Write-Step 'Installed.")]
    assert "Write-Warning" in found
    assert "return" in found
    text = INSTALL.read_text("utf-8")
    assert '$quoted = $Exe.Replace("\'", "\'\'")' in found
    assert "Write-Host \"  & '$quoted' setup " in text
    assert "& '$Exe'" not in text
    assert "PROMPTMEND_CHANNEL=uv set" in text


# exe is the default, uv the named fallback; an unknown value, or a local file of the other
# channel, is refused. The exe channel only takes the zip of this file's own release.
def test_channels() -> None:
    code = "\n".join(_lines())
    assert "$Channel = 'exe'" in code
    assert "if ($Channel -cne 'exe' -and $Channel -cne 'uv') {" in code
    assert "$Channel = 'uv'" in code  # PROMPTMEND_WHEEL implies it
    text = INSTALL.read_text("utf-8")
    assert "is not exe or uv." in text
    assert "not both." in text
    exe = _function("Install-Exe")
    assert "if ($Version -ne $ReleaseVersion) {" in exe
    assert '$ZipName = "promptmend-$Version-windows-x64.zip"' in exe
    assert '$Source = "$Repo/releases/download/v$Version/$ZipName"' in exe
    assert "$Expected = $ReleaseZipSha256.ToLowerInvariant()" in exe
    assert "if ($ReleaseZipSha256 -eq $UnrenderedHash) {" in exe
    assert "Join-Path $env:LOCALAPPDATA 'Programs'" in exe
    assert "$Dest = Join-Path $Programs 'promptmend'" in exe
    # The user PATH is written unexpanded, with its own registry type.
    assert "DoNotExpandEnvironmentNames" in text
    assert "$key.SetValue('Path', $value, $kind)" in text


# Both channels take their version from Get-Version, which checks it.
def test_version_check() -> None:
    code = _function("Get-Version")
    assert r"$version -cnotmatch '^[0-9]+\.[0-9]+\.[0-9]+([a-z]+[0-9]+)?\z'" in code
    assert "-lt $Oldest" in code
    assert "$Oldest = [version]'0.19.0'" in "\n".join(_lines())
    assert "$Version = Get-Version" in _function("Install-Exe")
    assert "$Version = Get-Version" in _function("Install-WithUv")


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
