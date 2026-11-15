"""Build the frozen Windows zip (#185) and time a trigger through it.

    uv sync --locked --group build
    uv run python scripts/build_windows.py build            # dist/promptmend-<v>-windows-x64.zip
    uv run python scripts/build_windows.py time <promptmend.exe>

`build` runs PyInstaller with packaging/windows/promptmend.spec (onedir: `promptmend.exe`
next to its `_internal` folder) and zips that folder as `promptmend\\promptmend.exe` and
`promptmend\\_internal\\...`, entries sorted with a fixed date, so the zip depends only on
what PyInstaller built. The zip is named for the pyproject version.

`time` runs the `-i-` trigger's real args (the draft as an argument) with the given launcher
against the setup check's local stub, a cold first run and then warm ones, and prints the
times; on GitHub Actions it also writes them to the job summary. It calls no real provider.
The timing itself lives in promptmend.smoke, which `promptmend doctor --timing` uses too.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

from promptmend.smoke import WARM_RUNS, summary, time_trigger

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "packaging" / "windows" / "promptmend.spec"
APP = "promptmend"
# Every entry gets this date (the zip format's earliest), so a rebuild of the same files
# makes the same bytes.
FIXED_DATE = (1980, 1, 1, 0, 0, 0)


def version(pyproject: Path = REPO / "pyproject.toml") -> str:
    return str(tomllib.loads(pyproject.read_text("utf-8"))["project"]["version"])


def zip_name(version: str) -> str:
    return f"{APP}-{version}-windows-x64.zip"


# Package data the frozen CLI reads (assets.match_dir(), the built-in profiles, which
# prompt_builder reads as plain files from the prompts/ folder next to its module, #312): a
# spec that lost them would build an exe whose deploy or rewrite fails only on the user's
# machine.
REQUIRED = (
    "_internal/promptmend/espanso/match/prompts-llm.yml",
    "_internal/promptmend/espanso/match/prompts-template.yml",
    "_internal/promptmend/prompts/default.md",
    "_internal/promptmend/prompts/general.md",
)


def make_zip(folder: Path, out: Path) -> list[str]:
    """Zip the onedir ``folder`` under a top folder named ``promptmend``; return the entry
    names. Refuses a folder without ``promptmend.exe``, ``_internal`` or the REQUIRED data."""
    if not (folder / f"{APP}.exe").is_file() or not (folder / "_internal").is_dir():
        raise FileNotFoundError(f"{folder} holds no {APP}.exe with its _internal folder")
    missing = [name for name in REQUIRED if not (folder / name).is_file()]
    if missing:
        raise FileNotFoundError(f"{folder} lacks the package data {', '.join(missing)}")
    names: list[str] = []
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(p for p in folder.rglob("*") if p.is_file()):
            name = f"{APP}/{path.relative_to(folder).as_posix()}"
            info = zipfile.ZipInfo(name, FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
            names.append(name)
    return names


def build(out_dir: Path = REPO / "dist", work: Path = REPO / "build" / "pyinstaller") -> Path:
    """Run PyInstaller on the spec, then zip its onedir output into ``out_dir``."""
    dist = work / "dist"
    subprocess.run(  # noqa: S603 - this interpreter's PyInstaller, a fixed argv
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(dist),
            "--workpath",
            str(work / "work"),
            str(SPEC),
        ],
        check=True,
        cwd=REPO,
    )
    out = out_dir / zip_name(version())
    make_zip(dist / APP, out)
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build")
    timing = sub.add_parser("time")
    timing.add_argument("launcher", nargs="+")
    timing.add_argument("--runs", type=int, default=WARM_RUNS)
    args = parser.parse_args(argv)
    if args.command == "build":
        print(build())
        return
    text = summary(time_trigger(args.launcher, max(args.runs, 1)), " ".join(args.launcher))
    print(text)
    if target := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text)


if __name__ == "__main__":
    main()
