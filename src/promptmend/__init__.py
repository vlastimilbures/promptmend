from __future__ import annotations

# Single source of truth is pyproject.toml, read from the installed metadata. Read on first
# use, not at import: importlib.metadata is a large import, and every trigger starts a fresh
# process that imports this package but never needs the version.
_version: str | None = None


def installed_version() -> str:
    """The installed distribution's version, read once."""
    global _version
    if _version is None:
        from importlib.metadata import PackageNotFoundError, version

        try:
            _version = version("promptmend")
        except PackageNotFoundError:  # running from a source tree that was never installed
            _version = "0+unknown"
    return _version


def __getattr__(name: str) -> str:
    if name == "__version__":
        return installed_version()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
