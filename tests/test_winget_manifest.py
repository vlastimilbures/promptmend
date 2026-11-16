"""scripts/winget_manifest.py: the WinGet manifests rendered from packaging/winget/ (#185).
Offline: a fake zip, nothing is downloaded."""

import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
_SCRIPT = REPO / "scripts" / "winget_manifest.py"
if TYPE_CHECKING:
    import release_notes as notes
    import winget_manifest as wm
else:
    _spec = importlib.util.spec_from_file_location("winget_manifest", _SCRIPT)
    assert _spec
    assert _spec.loader
    wm = importlib.util.module_from_spec(_spec)
    sys.modules["winget_manifest"] = wm
    _spec.loader.exec_module(wm)
    notes = wm._release_notes()

SHA = "ab" * 32
VERSION = "1.2.3"
SCHEMA = "1.28.0"


def _parsed(**kwargs: Any) -> dict[str, dict[str, Any]]:
    rendered = wm.render(VERSION, SHA, release_date="2026-10-07", **kwargs)
    return {name: yaml.safe_load(text) for name, text in rendered.items()}


def test_templates_are_the_three_manifests() -> None:
    names = sorted(p.name for p in wm.TEMPLATES.iterdir())
    assert names == sorted(wm.FILES)


def test_rendered_manifests_parse_with_their_fields() -> None:
    parsed = _parsed()
    version = parsed["vlastimilbures.PromptMend.yaml"]
    installer = parsed["vlastimilbures.PromptMend.installer.yaml"]
    locale = parsed["vlastimilbures.PromptMend.locale.en-US.yaml"]
    for manifest in parsed.values():
        assert manifest["PackageIdentifier"] == "vlastimilbures.PromptMend"
        assert manifest["PackageVersion"] == VERSION
        assert manifest["ManifestVersion"] == SCHEMA
    assert version["ManifestType"] == "version"
    assert version["DefaultLocale"] == "en-US"
    assert installer["ManifestType"] == "installer"
    assert installer["InstallerType"] == "zip"
    assert installer["NestedInstallerType"] == "portable"
    assert installer["NestedInstallerFiles"] == [
        {"RelativeFilePath": "promptmend\\promptmend.exe", "PortableCommandAlias": "promptmend"}
    ]
    assert str(installer["ReleaseDate"]) == "2026-10-07"
    assert installer["Installers"] == [
        {
            "Architecture": "x64",
            "InstallerUrl": "https://github.com/vlastimilbures/promptmend/releases/download/"
            "v1.2.3/promptmend-1.2.3-windows-x64.zip",
            "InstallerSha256": SHA.upper(),
        }
    ]
    assert locale["ManifestType"] == "defaultLocale"
    assert locale["PackageLocale"] == "en-US"
    assert locale["PackageName"] == "PromptMend"
    assert locale["License"] == "MIT"
    assert locale["PackageUrl"] == wm.REPO_URL
    assert locale["ReleaseNotesUrl"] == f"{wm.REPO_URL}/releases/tag/v1.2.3"
    assert locale["LicenseUrl"] == f"{wm.REPO_URL}/blob/v1.2.3/LICENSE"


def test_locale_keeps_the_schema_limits() -> None:
    locale = _parsed()["vlastimilbures.PromptMend.locale.en-US.yaml"]
    installer = _parsed()["vlastimilbures.PromptMend.installer.yaml"]
    assert 3 <= len(locale["ShortDescription"]) <= 256
    assert 3 <= len(locale["Description"]) <= 10000
    assert "`" not in locale["Description"]
    assert 1 <= len(locale["Tags"]) <= 16
    assert all(tag == tag.lower() and " " not in tag and len(tag) <= 40 for tag in locale["Tags"])
    assert locale["Moniker"] == locale["Moniker"].lower()
    assert " " not in locale["Moniker"]
    assert installer["MinimumOSVersion"] == "10.0.17763.0"


# The "required" lists of the 1.28.0 schemas (winget-cli schemas/JSON/manifests/v1.28.0),
# copied here so the test stays offline; the CI frozen-windows job runs `winget validate`.
REQUIRED = {
    "version": {"PackageIdentifier", "PackageVersion", "DefaultLocale"},
    "installer": {"PackageIdentifier", "PackageVersion", "Installers"},
    "defaultLocale": {
        "PackageIdentifier",
        "PackageVersion",
        "PackageLocale",
        "Publisher",
        "PackageName",
        "License",
        "ShortDescription",
    },
}
REQUIRED_PER_INSTALLER = {"Architecture", "InstallerUrl", "InstallerSha256"}


def test_manifests_have_the_schema_required_fields() -> None:
    parsed = _parsed()
    assert sorted(m["ManifestType"] for m in parsed.values()) == sorted(REQUIRED)
    for name, manifest in parsed.items():
        missing = REQUIRED[manifest["ManifestType"]] | {"ManifestType", "ManifestVersion"}
        assert not missing - set(manifest), name
    installer = parsed["vlastimilbures.PromptMend.installer.yaml"]
    for entry in installer["Installers"]:
        assert not REQUIRED_PER_INSTALLER - set(entry)


def test_license_matches_the_repo() -> None:
    assert (REPO / "LICENSE").read_text("utf-8").startswith("MIT License")
    copyright_line = (REPO / "LICENSE").read_text("utf-8").splitlines()[2]
    assert _parsed()["vlastimilbures.PromptMend.locale.en-US.yaml"]["Copyright"] == (copyright_line)


def test_schema_headers_match_the_manifest_type() -> None:
    for name, text in wm.render(VERSION, SHA, release_date="2026-10-07").items():
        kind = yaml.safe_load(text)["ManifestType"]
        assert text.startswith(
            f"# yaml-language-server: $schema=https://aka.ms/winget-manifest.{kind}.{SCHEMA}"
            ".schema.json\n"
        ), name
        assert not wm.PLACEHOLDER.search(text)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/promptmend-1.2.3-windows-x64.zip",
        "http://localhost/promptmend-1.2.3-windows-x64.zip",
        "http://[::1]:8765/dist/promptmend-1.2.3-windows-x64.zip",
        "https://example.org/mirror/promptmend-1.2.3-windows-x64.zip",
    ],
    ids=["v4", "localhost", "v6", "https"],
)
def test_url_override(url: str) -> None:
    installer = _parsed(url=url)["vlastimilbures.PromptMend.installer.yaml"]
    assert installer["Installers"][0]["InstallerUrl"] == url


@pytest.mark.parametrize(
    ("version", "sha", "url", "date"),
    [
        ("1.2", SHA, None, "2026-10-07"),
        ("v1.2.3", SHA, None, "2026-10-07"),
        ("1.2.3rc1", SHA, None, "2026-10-07"),
        ("01.2.3", SHA, None, "2026-10-07"),
        (VERSION, "ab" * 31, None, "2026-10-07"),
        (VERSION, "zz" * 32, None, "2026-10-07"),
        (VERSION, SHA, "ftp://x/promptmend-1.2.3-windows-x64.zip", "2026-10-07"),
        (VERSION, SHA, "https://x/promptmend-1.2.3-windows-x64.zip\nKey: 1", "2026-10-07"),
        (VERSION, SHA, "http://example.org/promptmend-1.2.3-windows-x64.zip", "2026-10-07"),
        (VERSION, SHA, "http://127.0.0.2/promptmend-1.2.3-windows-x64.zip", "2026-10-07"),
        (VERSION, SHA, "https:///promptmend-1.2.3-windows-x64.zip", "2026-10-07"),
        (VERSION, SHA, "https://x/promptmend-1.2.4-windows-x64.zip", "2026-10-07"),
        (VERSION, SHA, "https://x/promptmend-1.2.3-windows-x64.zip?a", "2026-10-07"),
        (VERSION, SHA, None, "20261007"),
        (VERSION, SHA, None, "2026-13-01"),
    ],
    ids=[
        "short",
        "vprefix",
        "pre",
        "zero",
        "shalen",
        "shahex",
        "ftp",
        "newline",
        "httpother",
        "httpnotloop",
        "nohost",
        "basename",
        "query",
        "dnum",
        "dbad",
    ],
)
def test_refuses_bad_input(version: str, sha: str, url: str | None, date: str) -> None:
    with pytest.raises(wm.ManifestError):
        wm.render(version, sha, url, date)


def test_leftover_placeholder_is_refused(tmp_path: Path) -> None:
    for name in wm.FILES:
        (tmp_path / name).write_text("PackageVersion: __VERSION__\nX: __NEW__\n", "utf-8")
    with pytest.raises(wm.ManifestError, match="__NEW__"):
        wm.render(VERSION, SHA, release_date="2026-10-07", templates=tmp_path)


CHANGELOG = """# Changelog

## Unreleased

## 1.2.3 - 2026-09-30

### Added
- Something.

## 1.2.2 - 2026-09-01

### Added
- Earlier.
"""


def _release_date(**kwargs: Any) -> str:
    text = wm.render(VERSION, SHA, **kwargs)["vlastimilbures.PromptMend.installer.yaml"]
    return str(yaml.safe_load(text)["ReleaseDate"])


def test_release_date_comes_from_the_changelog(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, "utf-8")
    assert _release_date(changelog=changelog) == "2026-09-30"
    assert _release_date(changelog=changelog, release_date="2026-10-01") == "2026-10-01"


@pytest.mark.parametrize(
    "text",
    [CHANGELOG.replace("## 1.2.3 - 2026-09-30", "## 1.2.4 - 2026-09-30"), "## 1.2.3\n- x\n"],
    ids=["absent", "undated"],
)
def test_release_date_needs_a_dated_heading(tmp_path: Path, text: str) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(text, "utf-8")
    with pytest.raises(wm.ManifestError, match="release"):
        _release_date(changelog=changelog)
    assert _release_date(changelog=changelog, release_date="2026-10-01") == "2026-10-01"


def test_release_date_is_today_with_a_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wm, "_today", lambda: "2031-02-03")
    url = "http://127.0.0.1:8765/promptmend-1.2.3-windows-x64.zip"
    assert _release_date(url=url, changelog=tmp_path / "missing.md") == "2031-02-03"


def test_today_is_a_utc_date() -> None:
    assert len(wm._today()) == 10


def test_the_checkout_changelog_dates_its_version() -> None:
    newest = notes.releases((REPO / "CHANGELOG.md").read_text("utf-8"))[0]
    assert wm.changelog_date(newest.version) == newest.date.isoformat()


def test_main_writes_the_winget_pkgs_layout(tmp_path: Path, capsys: Any) -> None:
    archive = tmp_path / "promptmend-1.2.3-windows-x64.zip"
    archive.write_bytes(b"not really a zip")
    out = tmp_path / "out"
    argv = ["--version", VERSION, "--zip", str(archive), "--release-date", "2026-10-07"]
    assert wm.main([*argv, "-o", str(out)]) == 0
    target = out / "manifests" / "v" / "vlastimilbures" / "PromptMend" / VERSION
    assert sorted(p.name for p in target.iterdir()) == sorted(wm.FILES)
    installer = yaml.safe_load((target / "vlastimilbures.PromptMend.installer.yaml").read_text())
    expected = hashlib.sha256(b"not really a zip").hexdigest().upper()
    assert installer["Installers"][0]["InstallerSha256"] == expected
    assert b"\r\n" not in (target / wm.FILES[0]).read_bytes()
    printed = capsys.readouterr()
    assert str(target / wm.FILES[0]) in printed.out
    assert "warning: this checkout is" in printed.err


def test_main_on_the_checkout_version(tmp_path: Path, capsys: Any) -> None:
    newest = notes.releases((REPO / "CHANGELOG.md").read_text("utf-8"))[0]
    out = tmp_path / "out"
    assert wm.main(["--version", newest.version, "--sha256", SHA, "-o", str(out)]) == 0
    installer = (wm.manifest_dir(out, newest.version) / wm.FILES[1]).read_text("utf-8")
    assert f"ReleaseDate: {newest.date.isoformat()}" in installer
    assert capsys.readouterr().err == ""


def test_main_takes_a_sha(tmp_path: Path) -> None:
    out = tmp_path / "out"
    argv = ["--version", VERSION, "--sha256", SHA, "--release-date", "2026-10-07"]
    assert wm.main([*argv, "-o", str(out)]) == 0
    assert (wm.manifest_dir(out, VERSION) / wm.FILES[1]).is_file()


@pytest.mark.parametrize(
    "extra",
    [
        ["--sha256", "nothex"],
        ["--zip", "missing.zip"],
        ["--zip", "SELF"],
        ["--sha256", SHA, "--url", "http://example.org/promptmend-1.2.3-windows-x64.zip"],
        ["--sha256", SHA],
    ],
    ids=["sha", "missing", "name", "http", "nodate"],
)
def test_main_refuses(tmp_path: Path, extra: list[str]) -> None:
    other = tmp_path / "promptmend-9.9.9-windows-x64.zip"
    other.write_bytes(b"x")
    argv = [str(other) if item == "SELF" else item for item in extra]
    with pytest.raises(SystemExit) as exc:
        wm.main(["--version", VERSION, *argv, "-o", str(tmp_path / "out")])
    assert exc.value.code == 2
    assert not (tmp_path / "out").exists()
