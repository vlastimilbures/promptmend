"""The update check (#197): PyPI's newest final release, cached for a day, never raising.
Offline: every request is answered by fake_http, through the real fetch that conftest stubs
out for every other test."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import truststore
from conftest import FakeHttp

from promptmend import config, config_files, update_check
from promptmend.config import Settings
from promptmend.update_check import AVAILABLE, LATEST, OFF, UNKNOWN, UpdateStatus, check

# conftest replaces _fetch in every test; this is the real one, kept at import.
REAL_FETCH = update_check._fetch
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def pypi(fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch) -> FakeHttp:
    """The real fetch, answered by fake_http; the installed version is 0.21.0."""
    monkeypatch.setattr(update_check, "_fetch", REAL_FETCH)
    monkeypatch.setattr(update_check, "installed_version", lambda: "0.21.0")
    return fake_http


def _file(yanked: bool = False) -> dict[str, Any]:
    return {"filename": "promptmend.whl", "yanked": yanked}


def _releases(*versions: str, **extra: list[dict[str, Any]]) -> dict[str, Any]:
    return {"info": {}, "releases": {**{v: [_file()] for v in versions}, **extra}}


def _cfg(on: bool = True) -> Settings:
    return Settings.load() if on else Settings(update_check=False)


def _cache(checked_at: str, latest: str = "0.22.0") -> None:
    path = update_check.cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"checked_at": checked_at, "latest": latest}), "utf-8")


def test_the_installed_release_is_the_latest(pypi: FakeHttp) -> None:
    pypi.reply(_releases("0.9.0", "0.20.1", "0.21.0"))
    found = check(_cfg(), NOW)
    assert found == UpdateStatus(LATEST, "0.21.0", "2026-10-07T12:00:00+00:00")
    assert [str(r.url) for r in pypi.requests] == [update_check.URL]
    assert pypi.requests[0].headers["User-Agent"].startswith("promptmend/")
    (kwargs,) = pypi.client_kwargs
    assert type(kwargs.pop("verify")) is truststore.SSLContext  # the OS trust store (#314)
    assert kwargs == {"timeout": httpx.Timeout(update_check.TIMEOUT)}
    cached = json.loads(update_check.cache_path().read_text("utf-8"))
    assert cached == {"checked_at": "2026-10-07T12:00:00+00:00", "latest": "0.21.0"}


def test_a_newer_release_is_available(pypi: FakeHttp) -> None:
    # Numeric order, not text order: 0.100.0 is newer than 0.21.0 and 0.9.0.
    pypi.reply(_releases("0.21.0", "0.9.0", "0.100.0"))
    assert check(_cfg(), NOW).state == AVAILABLE
    assert check(_cfg(), NOW).latest == "0.100.0"


@pytest.mark.parametrize("name", ["0.22.0rc1", "0.22.0.dev1", "0.22.0b2", "0.22.0.post1", "1.0"])
def test_a_pre_release_is_ignored(pypi: FakeHttp, name: str) -> None:
    pypi.reply(_releases("0.21.0", name))
    assert check(_cfg(), NOW).state == LATEST


def test_a_yanked_or_empty_release_is_ignored(pypi: FakeHttp) -> None:
    pypi.reply(_releases("0.21.0", **{"0.22.0": [_file(yanked=True)], "0.23.0": []}))
    assert check(_cfg(), NOW) == UpdateStatus(LATEST, "0.21.0", "2026-10-07T12:00:00+00:00")


def test_one_unyanked_file_keeps_a_release(pypi: FakeHttp) -> None:
    pypi.reply(_releases("0.21.0", **{"0.22.0": [_file(yanked=True), _file()]}))
    assert check(_cfg(), NOW).latest == "0.22.0"


def test_an_install_newer_than_pypi_is_the_latest(
    pypi: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    # an editable build
    monkeypatch.setattr(update_check, "installed_version", lambda: "0.22.0.dev3+g1234")
    pypi.reply(_releases("0.21.0"))
    assert check(_cfg(), NOW).state == LATEST


def test_an_installed_pre_release_is_older_than_its_release(
    pypi: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update_check, "installed_version", lambda: "0.22.0rc1")
    pypi.reply(_releases("0.21.0", "0.22.0"))
    assert check(_cfg(), NOW).state == AVAILABLE


def test_a_post_release_is_not_older(pypi: FakeHttp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_check, "installed_version", lambda: "0.22.0.post1")
    pypi.reply(_releases("0.22.0"))
    assert check(_cfg(), NOW).state == LATEST


@pytest.mark.parametrize(
    ("data", "state"),
    [
        ({"info": {"version": "0.22.0"}}, AVAILABLE),
        ({"info": {"version": "0.21.0"}, "releases": None}, LATEST),
        ({"info": {"version": "0.23.0rc1"}, "releases": []}, UNKNOWN),
    ],
    ids=["missing", "null", "pre"],
)
def test_without_a_release_map_the_info_version_counts(
    pypi: FakeHttp, data: dict[str, Any], state: str
) -> None:
    pypi.reply(data)
    assert check(_cfg(), NOW).state == state


def test_a_naive_now_is_utc(pypi: FakeHttp) -> None:
    pypi.reply(_releases("0.21.0"))
    found = check(_cfg(), NOW.replace(tzinfo=None))
    assert found == UpdateStatus(LATEST, "0.21.0", "2026-10-07T12:00:00+00:00")
    assert check(_cfg(), NOW.replace(tzinfo=None) + timedelta(hours=1)).state == LATEST
    assert len(pypi.requests) == 1


def test_the_whole_call_is_bounded(pypi: FakeHttp, monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that holds the request open is given up after TIMEOUT, whatever httpx's
    per-phase timeouts would allow."""
    release = threading.Event()
    answer = pypi.handler

    def slow(request: httpx.Request) -> httpx.Response:
        release.wait(5)
        return answer(request)

    monkeypatch.setattr(pypi, "handler", slow)
    monkeypatch.setattr(update_check, "TIMEOUT", 0.05)
    pypi.reply(_releases("0.22.0"))
    try:
        assert check(_cfg(), NOW).state == UNKNOWN
    finally:
        release.set()


def test_a_fresh_cache_is_reused(pypi: FakeHttp) -> None:
    _cache((NOW - timedelta(hours=23)).isoformat())
    found = check(_cfg(), NOW)
    assert (found.state, found.latest) == (AVAILABLE, "0.22.0")
    assert pypi.requests == []


@pytest.mark.parametrize(
    "checked_at",
    [
        (NOW - timedelta(hours=24)).isoformat(),
        (NOW + timedelta(minutes=5)).isoformat(),  # a clock set back
        "2026-10-07T11:00:00",  # no time zone
        "yesterday",
    ],
    ids=["old", "future", "naive", "bad"],
)
def test_an_expired_cache_asks_again(pypi: FakeHttp, checked_at: str) -> None:
    _cache(checked_at)
    pypi.reply(_releases("0.21.0"))
    assert check(_cfg(), NOW).state == LATEST
    assert len(pypi.requests) == 1


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        '{"latest": "0.22.0"}',
        '{"checked_at": 1, "latest": "x"}',
        '{"checked_at": 1, "latest": "0.22.0"}',
    ],
)
def test_a_damaged_cache_asks_again(pypi: FakeHttp, text: str) -> None:
    path = update_check.cache_path()
    path.parent.mkdir(parents=True)
    path.write_text(text, "utf-8")
    pypi.reply(_releases("0.21.0"))
    assert check(_cfg(), NOW).state == LATEST
    assert len(pypi.requests) == 1


def test_off_asks_nothing_and_reads_nothing(
    pypi: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_read() -> None:
        raise AssertionError("the cache was read")

    monkeypatch.setattr(update_check, "cache_path", no_read)
    assert check(_cfg(on=False), NOW) == UpdateStatus(OFF)
    assert pypi.requests == []


def test_the_setting_turns_it_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPT_UPDATE_CHECK", "false")
    assert Settings.load().update_check is False
    assert check(Settings.load()).state == OFF


@pytest.mark.parametrize(
    "reply",
    [
        {"bad_json": True},
        {"json_data": {"info": {}}},  # no releases
        {"json_data": {"releases": []}},
        {"json_data": {"releases": {"0.22.0rc1": [{"yanked": False}]}}},  # nothing final
        {"json_data": _releases("0.22.0"), "status_code": 404},
        {"json_data": _releases("0.22.0"), "status_code": 503},
    ],
    ids=["html", "no-rel", "list", "none", "404", "503"],
)
def test_a_bad_reply_is_unknown(pypi: FakeHttp, reply: dict[str, Any]) -> None:
    pypi.reply(**reply)
    assert check(_cfg(), NOW) == UpdateStatus(UNKNOWN, None, "2026-10-07T12:00:00+00:00")
    cached = json.loads(update_check.cache_path().read_text("utf-8"))
    assert cached == {"checked_at": "2026-10-07T12:00:00+00:00", "latest": None}


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError("offline"), httpx.ReadTimeout("slow")],
    ids=["offline", "timeout"],
)
def test_a_network_error_is_unknown(pypi: FakeHttp, exc: Exception) -> None:
    pypi.exc = exc
    assert check(_cfg(), NOW).state == UNKNOWN


def test_a_failure_is_not_asked_again_within_the_hour(pypi: FakeHttp) -> None:
    pypi.exc = httpx.ConnectError("offline")
    assert check(_cfg(), NOW).state == UNKNOWN
    update_check._last = None  # another process: only the file says so
    assert check(_cfg(), NOW + timedelta(minutes=59)).state == UNKNOWN
    assert len(pypi.requests) == 1


def test_a_failure_is_asked_again_after_the_hour(pypi: FakeHttp) -> None:
    pypi.queue(httpx.ConnectError("offline"))
    pypi.reply(_releases("0.22.0"))
    assert check(_cfg(), NOW).state == UNKNOWN
    assert check(_cfg(), NOW + update_check.RETRY_AFTER).state == AVAILABLE
    assert len(pypi.requests) == 2


def test_a_cache_that_cannot_be_written_still_answers(
    pypi: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(config_files, "write_atomic", refuse)
    pypi.reply(_releases("0.21.0", "0.22.0"))
    assert check(_cfg(), NOW).state == AVAILABLE
    assert not update_check.cache_path().exists()
    # This process remembers it: a reload does not ask again.
    assert check(_cfg(), NOW + timedelta(minutes=1)).state == AVAILABLE
    assert len(pypi.requests) == 1


def test_a_failure_that_cannot_be_written_is_not_asked_again(
    pypi: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(config_files, "write_atomic", refuse)
    pypi.exc = httpx.ConnectError("offline")
    for _ in range(3):
        assert check(_cfg(), NOW).state == UNKNOWN
    assert len(pypi.requests) == 1


# --- check_configured: fails closed ---------------------------------------------------------


def test_configured_default_asks(pypi: FakeHttp) -> None:
    pypi.reply(_releases("0.22.0"))
    assert update_check.check_configured(NOW).state == AVAILABLE


def test_configured_off_asks_nothing(pypi: FakeHttp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPT_UPDATE_CHECK", "false")
    assert update_check.check_configured(NOW).state == OFF
    assert pypi.requests == []


def test_a_rejected_value_asks_nothing(pypi: FakeHttp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPT_UPDATE_CHECK", "nope")
    assert update_check.check_configured(NOW) == UpdateStatus(UNKNOWN)
    assert pypi.requests == []


def test_a_broken_settings_file_asks_nothing(
    pypi: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PROMPTMEND_ENV")
    saved = config.settings_file()
    saved.parent.mkdir(parents=True)
    saved.write_text("PROMPT_UPDATE_CHECK = [not toml", "utf-8")
    assert update_check.check_configured(NOW) == UpdateStatus(UNKNOWN)
    assert pypi.requests == []


def test_another_bad_setting_still_asks(pypi: FakeHttp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPT_HISTORY", "maybe")
    pypi.reply(_releases("0.22.0"))
    assert update_check.check_configured(NOW).state == AVAILABLE


def test_the_cache_is_in_the_data_folder() -> None:
    assert update_check.cache_path() == config.user_data_dir() / "update-check.json"


def test_an_unknown_installed_version_is_unknown(
    pypi: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update_check, "installed_version", lambda: "0+unknown")
    pypi.reply(_releases("0.22.0"))
    assert check(_cfg(), NOW).state == UNKNOWN


def test_check_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(now: datetime) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(update_check, "_cached", broken)
    assert check(_cfg(), NOW) == UpdateStatus(UNKNOWN)


def test_tests_never_reach_pypi() -> None:
    """conftest's stub: without the real fetch, every check is unknown."""
    assert check(Settings.load()).state == UNKNOWN


@pytest.mark.parametrize(
    ("channel", "command"),
    [
        ("uv", update_check._UV),
        ("homebrew", "brew update && brew upgrade promptmend"),
        ("installer", update_check.INSTALL_PS1),
        ("script", "see docs/install.md"),
        ("scoop", "see docs/install.md"),
        ("", "see docs/install.md"),
    ],
)
def test_upgrade_command(channel: str, command: str) -> None:
    assert update_check.upgrade_command(channel) == command


@pytest.mark.parametrize(
    ("platform", "command"),
    [
        ("darwin", "git pull, then ./scripts/install_macos.sh"),
        ("win32", r"git pull, then .\scripts\install_windows.ps1"),
        ("linux", "git pull, then reinstall as CONTRIBUTING.md says"),
    ],
)
def test_a_checkout_pulls_then_reinstalls(platform: str, command: str) -> None:
    assert update_check.upgrade_command("editable", platform) == command


def test_the_upgrade_commands_are_the_documented_ones() -> None:
    from pathlib import Path

    install = (Path(__file__).resolve().parents[1] / "docs" / "install.md").read_text("utf-8")
    for command in update_check.UPGRADE_COMMANDS.values():
        assert command in install, command
    for script in ("./scripts/install_macos.sh", r".\scripts\install_windows.ps1"):
        assert script in install, script
