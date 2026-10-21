"""Check what a built wheel really contains, from a venv outside the checkout.

A declaration in pyproject.toml does not prove what lands in the wheel, so CI installs the
built wheel into a clean venv and runs this with that venv's Python (not from the repo, so
`src/` is not importable):

    <venv python> scripts/check_wheel.py --repo . --wheel dist/<name>.whl

It checks that the package was imported from the venv, that every match file under the repo's
espanso/match/ and every profile under src/promptmend/prompts/ resolves through
importlib.resources, that espanso/config/ was not shipped, that `promptmend --version` prints
the version, and that `promptmend persona` runs with an empty config. With `--constraints`
(the release's constraints.txt, exported from uv.lock), every distribution installed next to
the package must be pinned there at the installed version. Exits nonzero on the first failure.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from importlib import metadata
from pathlib import Path

import promptmend
from promptmend import assets
from promptmend.cli import PERSONA_PLACEHOLDER
from promptmend.prompt_builder import PROFILES, system_prompt


def _fail(message: str) -> None:
    sys.exit(f"check_wheel: {message}")


def _check_import(repo: Path) -> Path:
    package = Path(promptmend.__file__).resolve().parent
    if package.is_relative_to(repo):
        _fail(f"promptmend was imported from the checkout ({package}), not the wheel")
    return package


def _check_match_files(repo: Path, package: Path) -> None:
    folder = Path(str(assets.match_dir())).resolve()
    if not folder.is_relative_to(package):
        _fail(f"match files resolve to {folder}, outside the installed package")
    expected = sorted(p.name for p in (repo / "espanso" / "match").glob("*.yml"))
    if assets.match_names() != expected:
        _fail(f"match files {assets.match_names()} != repo {expected}")
    for name in expected:
        if assets.read_match(name) != (repo / "espanso" / "match" / name).read_text("utf-8"):
            _fail(f"{name} differs from the repo copy")


def _check_profiles(repo: Path) -> None:
    expected = sorted(p.stem for p in (repo / "src" / "promptmend" / "prompts").glob("*.md"))
    if sorted(PROFILES) != expected:
        _fail(f"profiles {sorted(PROFILES)} != repo {expected}")
    for name in expected:
        if not system_prompt(name).strip():
            _fail(f"profile {name} is empty")


def _check_wheel_contents(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        shipped = [n for n in archive.namelist() if "/espanso/config/" in n]
    if shipped:
        _fail(f"espanso/config must not ship, found {shipped}")


def _exe(name: str) -> Path:
    return Path(sys.executable).parent / (f"{name}.exe" if os.name == "nt" else name)


def _check_commands() -> None:
    """`promptmend --version` prints only the version, and nothing on stderr."""
    want = metadata.version("promptmend")
    result = subprocess.run(  # noqa: S603 - the venv's own entry point
        [str(_exe("promptmend")), "--version"], capture_output=True, check=False, timeout=60
    )
    out = result.stdout.decode("utf-8").strip()
    if (result.returncode, out, result.stderr) != (0, want, b""):
        _fail(f"promptmend --version exited {result.returncode} with {out!r} {result.stderr!r}")


def _check_persona() -> None:
    exe = _exe("promptmend")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        env_file = home / "empty.env"
        env_file.write_text("", encoding="utf-8")
        env = {
            **os.environ,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(home / "config"),
            "APPDATA": str(home / "appdata"),
            # persona records its run in the usage history: keep it out of the real one.
            "XDG_DATA_HOME": str(home / "data"),
            "LOCALAPPDATA": str(home / "localappdata"),
            "PROMPTMEND_ENV": str(env_file),
        }
        env.pop("PROMPT_PERSONA", None)
        result = subprocess.run(  # noqa: S603 - the venv's own entry point
            [str(exe), "persona"], capture_output=True, env=env, cwd=home, check=False, timeout=60
        )
    stdout = result.stdout.decode("utf-8")
    if (result.returncode, stdout) != (0, PERSONA_PLACEHOLDER):
        _fail(f"persona exited {result.returncode} with {stdout!r} {result.stderr!r}")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def constraint_mismatches(constraints: str, installed: dict[str, str]) -> list[str]:
    """Installed distributions that `constraints` does not pin at their installed version.

    A pin's environment marker is not evaluated: a pin whose marker excludes this platform
    simply has nothing installed to compare against.
    """
    pins: dict[str, str] = {}
    for line in constraints.splitlines():
        requirement = line.split(";", 1)[0].strip()
        if requirement and not requirement.startswith("#"):
            name, sep, version = requirement.partition("==")
            if not sep:
                raise ValueError(f"constraint {requirement!r} is not name==version")
            pins[_normalize(name)] = version.strip()
    return [
        f"{name} {version} (constraints: {pins.get(_normalize(name), 'not pinned')})"
        for name, version in sorted(installed.items())
        if pins.get(_normalize(name)) != version
    ]


def _check_constraints(constraints: Path) -> None:
    own = _normalize(metadata.distribution("promptmend").metadata["Name"])
    installed = {
        dist.metadata["Name"]: dist.version
        for dist in metadata.distributions()
        if _normalize(dist.metadata["Name"]) != own
    }
    if not installed:
        _fail("no dependencies installed next to the package")
    mismatches = constraint_mismatches(constraints.read_text("utf-8"), installed)
    if mismatches:
        _fail(f"installed versions differ from the constraints: {mismatches}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo", type=Path, required=True, help="the checkout the wheel was built from"
    )
    parser.add_argument("--wheel", type=Path, required=True, help="the built .whl file")
    parser.add_argument(
        "--constraints", type=Path, help="constraints.txt the wheel was installed with"
    )
    args = parser.parse_args()
    repo = args.repo.resolve()
    package = _check_import(repo)
    _check_match_files(repo, package)
    _check_profiles(repo)
    _check_wheel_contents(args.wheel)
    _check_commands()
    _check_persona()
    if args.constraints:
        _check_constraints(args.constraints)
    print(f"check_wheel: ok ({len(assets.match_names())} match files, {len(PROFILES)} profiles)")


if __name__ == "__main__":
    main()
