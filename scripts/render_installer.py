"""Fill the release version and its Windows zip's SHA-256 into install.ps1 (#186, #185).

    python3 scripts/render_installer.py 0.20.0 dist/install.ps1 \
        --zip dist/promptmend-0.20.0-windows-x64.zip

Reads scripts/install.ps1, replaces its PLACEHOLDER with the version and its ZIP_PLACEHOLDER
with the SHA-256 of the zip (the file the same Release attaches, which the installer's
default channel downloads and refuses on any other hash), and writes the result, which the
workflow attaches to the GitHub Release. Standard library only. Exits nonzero with a message
if the version is not X.Y.Z, the zip cannot be read, or a placeholder is not there exactly
once.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "scripts" / "install.ps1"
PLACEHOLDER = "__PROMPTMEND_VERSION__"
ZIP_PLACEHOLDER = "__PROMPTMEND_ZIP_SHA256__"
_VERSION = re.compile(r"\d+\.\d+\.\d+")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def render(source: str, version: str, zip_sha256: str) -> str:
    if not _VERSION.fullmatch(version):
        raise ValueError(f"{version!r} is not X.Y.Z")
    if not _SHA256.fullmatch(zip_sha256):
        raise ValueError(f"{zip_sha256!r} is not a lowercase hex SHA-256")
    for placeholder in (PLACEHOLDER, ZIP_PLACEHOLDER):
        count = source.count(placeholder)
        if count != 1:
            raise ValueError(f"{placeholder} appears {count} times, not once")
    return source.replace(PLACEHOLDER, version).replace(ZIP_PLACEHOLDER, zip_sha256)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version")
    parser.add_argument("output", type=Path)
    parser.add_argument("--zip", type=Path, required=True, help="the Release's Windows zip")
    parser.add_argument("--source", type=Path, default=SOURCE)
    args = parser.parse_args(argv)
    try:
        text = render(args.source.read_text("utf-8"), args.version, sha256(args.zip))
        args.output.write_bytes(text.encode("ascii"))
    except (OSError, ValueError) as exc:
        sys.exit(f"render_installer: {exc}")


if __name__ == "__main__":
    main()
