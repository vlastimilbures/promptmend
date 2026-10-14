"""The PromptMend logo and the interface's About facts (#112). Plain ASCII only, so every
terminal, font, NO_COLOR and screen reader shows it the same; the frame around it is a Textual
border, not box-drawing text. No Textual here, so a test can check it without the interface."""

from __future__ import annotations

import platform
from collections.abc import Mapping

from .. import __version__, config, config_store, update_check

# The interface's display name: the header, the wordmark and About all read it
# here, so a rename (#169) changes this one line (and the snapshots).
NAME = "PromptMend"
TAGLINE = "A rough draft in, a precise prompt out: type -i- in any text field."
REPO_URL = "https://github.com/vlastimilbures/promptmend"
LICENCE = "MIT"
# A terminal this narrow (or narrower) gets the text alone: the mark would wrap.
NARROW = 70


# The logo: a mended speech bubble beside `uvx pyfiglet -f small PromptMend`, ASCII and
# narrower than NARROW. About shows it above the tagline.
MARK = (
    " .-------.    ___                    _   __  __             _",
    r" |  [+]  |   | _ \_ _ ___ _ __  _ __| |_|  \/  |___ _ _  __| |",
    r" '--. .--'   |  _/ '_/ _ \ '  \| '_ \  _| |\/| / -_) ' \/ _` |",
    r"    |/       |_| |_| \___/_|_|_| .__/\__|_|  |_\___|_||_\__,_|",
    "                               |_|",
)
LOGO = "\n".join((*MARK, "", TAGLINE))


def runtime() -> dict[str, str]:
    """The Python and Textual versions this interface runs on. Textual is imported here only
    when the About screen asks, and the interface is already running on it."""
    import textual

    return {"python": platform.python_version(), "textual": textual.__version__}


# What About says of the update check (#197), by its state; ``available`` names the release.
_UPDATE_WORDS = {
    update_check.LATEST: "this is the latest release",
    update_check.OFF: "not checked (PROMPT_UPDATE_CHECK=false)",
    update_check.UNKNOWN: "unknown (pypi.org could not be asked)",
}


def about_facts(
    install: Mapping[str, object] | None,
    version: str | None = None,
    update: update_check.UpdateStatus | None = None,
) -> list[str]:
    """What the About screen lists: version, whether a newer release exists (#197, from the
    State), install channel (doctor's install check, from the State; unknown before it is
    read), runtime, where settings and history live, licence and repository. Paths only,
    never a setting's value."""
    channel = str((install or {}).get("channel") or "unknown")
    found = runtime()
    update = update or update_check.UpdateStatus()
    if update.state == update_check.AVAILABLE:
        from ..doctor import upgrade_hint

        news = f"{update.latest} available: {upgrade_hint(install or {})}"
    else:
        news = _UPDATE_WORDS.get(update.state, update.state)
    return [
        f"Version:   {NAME} {version or __version__}",
        f"Update:    {news}",
        f"Installed: {channel}",
        f"Runs on:   Python {found['python']}, Textual {found['textual']}",
        f"Settings:  {config_store.config_dir()}",
        f"History:   {config.user_data_dir()}",
        f"Licence:   {LICENCE}",
        f"Source:    {REPO_URL}",
    ]
