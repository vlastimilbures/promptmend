# PyInstaller spec for the frozen Windows build (#185): onedir, a console promptmend.exe
# with its _internal\ folder next to it, so a trigger never unpacks anything (onefile would,
# on every call). scripts/build_windows.py runs it and zips the result; run it from the repo
# root after `uv sync --locked --group build`.
#
# What a static import scan misses is collected by hand:
# - promptmend's own lazily imported modules (cli._LazyGroup imports commands.* by name);
# - textual's widgets (textual.widgets.__getattr__ imports them by name) and its data;
# - the package data: the profiles (prompts/*.md) and the Espanso match files, which the
#   wheel holds under promptmend/espanso/match (assets.match_dir() reads them there);
# - promptmend's metadata, which __version__ reads.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parents[1]  # noqa: F821 - SPECPATH is set by PyInstaller

hiddenimports = [
    *collect_submodules("promptmend"),
    *collect_submodules("textual"),
    # Imported inside functions only: listed so a refactor that hides them cannot drop them.
    "tomli_w",
    "tomllib",
    "sqlite3",
    "csv",
    "decimal",
]
datas = [
    *collect_data_files("promptmend"),
    *collect_data_files("textual"),
    *copy_metadata("promptmend"),
    # The repo's match files are the source of truth (the wheel's force-include copies them
    # to the same place), so an editable or a wheel install builds the same zip.
    (str(ROOT / "espanso" / "match" / "*.yml"), "promptmend/espanso/match"),
]

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "windows" / "promptmend_main.py")],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "mypy", "ruff", "coverage", "PyInstaller"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    # UTF-8 mode, as PYTHONIOENCODING=utf-8 gives an installed CLI's children: piped stdout
    # (the interface's command line, the smoke test) is UTF-8, not the ANSI code page.
    [("X utf8", None, "OPTION")],
    exclude_binaries=True,
    name="promptmend",
    console=True,
    debug=False,
    strip=False,
    upx=False,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="promptmend",
)
