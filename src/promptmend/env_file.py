"""The legacy .env reader: the KEY=VALUE pairs of the file PROMPTMEND_ENV names, and
what it skipped or cut, reported through the caller's callbacks (config.ConfigLayers)."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from . import config_files
from .paths import ENV_FILE_VAR


def _parse_value(raw: str) -> str:
    """A .env value: the text inside a leading quote pair, else the text before an inline
    ` # comment` (so `OPENROUTER_PROVIDER=openai  # note` is just `openai`)."""
    value = raw.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        if end > 0:
            return value[1:end]
    return re.split(r"\s+#", value, maxsplit=1)[0]


def _cut_at_comment(raw: str) -> bool:
    """Whether _parse_value() dropped an unquoted ` #...` from ``raw``: a comment, or part of
    the value (a regex, a persona) that needed quotes. Only the user can tell."""
    return _parse_value(raw) != raw.strip() and raw.strip()[:1] not in ("'", '"')


# Free-text settings whose value may well hold ` #` (a regex for `#12345`, "I am #1"), so a cut
# there is reported. Elsewhere ` # note` is the usual dotenv comment and stays silent: no
# model slug, profile name, number or key holds a space, and a base URL never needs ` #`.
_HASH_IN_VALUE = frozenset({"PROMPT_EXTRA_PATTERNS", "PROMPT_PERSONA"})


def _env_text(data: bytes) -> str:
    """The text of a .env file's bytes. UTF-8, with a leading byte order mark (Windows
    Notepad) dropped, so it never becomes part of the first key."""
    return data.decode("utf-8-sig")


def _parse_env_text(text: str) -> tuple[dict[str, str], list[int], list[str]]:
    """KEY=VALUE pairs of .env text, the numbers of the lines skipped for lacking `=`, and the
    _HASH_IN_VALUE keys whose unquoted value was cut at ` #` (see _cut_at_comment)."""
    pairs = {}
    skipped = []
    cut = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip().removeprefix("export ")
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            skipped.append(number)
            continue
        key, _, value = line.partition("=")
        pairs[key.strip()] = _parse_value(value)
        if key.strip() in _HASH_IN_VALUE and _cut_at_comment(value):
            cut.append(key.strip())
    return pairs, skipped, cut


def _cut_value_error(key: str) -> str:
    return f"the value of {key} was cut at ' #' (a comment); quote the value"


def _read_env_file(
    path: Path,
    note: Callable[[str, str], None],
    fail: Callable[..., None],
) -> dict[str, str] | None:
    """The pairs of the .env PROMPTMEND_ENV names, or None when it cannot be read. A file
    that exists but cannot be read is noted, as is a line without `=` (by line number or
    setting name, never content), and a persona cut at ` #`. A file that is not UTF-8 text
    goes to ``fail``: its settings are lost, which strict mode must not hide. So does
    PROMPT_EXTRA_PATTERNS cut at ` #`: a gate missing part of its patterns fails closed, and
    repair mode drops the cut value (the next layer's applies)."""
    source = f"file:{path}"
    try:
        raw_text = _env_text(path.read_bytes())
    except UnicodeDecodeError:
        fail(source, f"the .env named by {ENV_FILE_VAR} is not UTF-8 text; save it as UTF-8")
        return None
    except OSError:
        if config_files.lexists(path):
            note(source, ".env cannot be read; it was skipped")
        return None
    pairs, skipped, cut = _parse_env_text(raw_text)
    for number in skipped:
        note(source, f"line {number} of .env has no '=' and was ignored")
    for key in cut:
        if key == "PROMPT_EXTRA_PATTERNS":
            fail(source, _cut_value_error(key), (key,))
            del pairs[key]
        else:
            note(source, _cut_value_error(key))
    return pairs
