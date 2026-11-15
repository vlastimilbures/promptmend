"""The sanitiser (sanitize.clean()): what it drops, what real text keeps, and that it stays
linear on adversarial input."""

from __future__ import annotations

import re
import time
import unicodedata

import pytest

from promptmend import sanitize
from promptmend.redaction import DEFAULT_IGNORABLE


# Every default-ignorable code point is removed, except the joiners and emoji selectors that
# _keep_run() judges in context.
def test_clean_drops_every_default_ignorable() -> None:
    ignorable = re.compile(f"[{DEFAULT_IGNORABLE}]")
    kept = set("\u200c\u200d\ufe0e\ufe0f")
    hidden = "".join(
        c for c in map(chr, range(0xE1000)) if c not in kept and ignorable.fullmatch(c)
    )
    assert len(hidden) > 4000
    assert sanitize.clean(f"a{hidden}b") == "ab"


# Every invisible format character, Hangul filler and stray selector is removed; the
# zero-width space between two selectors cannot rescue the run around it.
@pytest.mark.parametrize(
    "hidden",
    [
        "\u200b",  # zero-width space
        "\u2060",  # word joiner
        "\ufeff",  # byte order mark
        "\u180e",  # Mongolian vowel separator
        "\u00ad",  # soft hyphen
        "\u200e",  # left-to-right mark
        "\u200f",  # right-to-left mark
        "\u061c",  # Arabic letter mark
        "\u2061",  # invisible function application
        "\u2065",  # unassigned, default-ignorable
        "\ufff0",  # unassigned, default-ignorable
        "\U000e0200",  # unassigned, default-ignorable
        "\u034f",  # combining grapheme joiner
        "\u180b",  # Mongolian free variation selector
        "\u17b4",  # Khmer vowel inherent
        "\U0001d173",  # musical symbol begin beam
        "\ufe0f",  # an emoji selector after a letter
        "\u115f",  # Hangul choseong filler
        "\u1160",  # Hangul jungseong filler
        "\u3164",  # Hangul filler
        "\uffa0",  # halfwidth Hangul filler
        "\ufe00",  # a selector no emoji uses
        "\ufe0f\ufe0f",  # a run of emoji selectors
        "\ufe0f\u200b\ufe0f",  # a run split by a zero-width space
        "\U000e0101",  # ideographic variation selector
        "\u200d",  # a joiner between ASCII letters
        "\u200c\u200c",  # a run of joiners
    ],
    ids=lambda s: "+".join(f"U+{ord(c):04X}" for c in s),
)
def test_clean_drops_invisible_characters(hidden: str) -> None:
    assert sanitize.clean(f"pay{hidden}load") == "payload"


# A selector or joiner at the start of the text or after a space has nothing to attach to.
@pytest.mark.parametrize("text", ["\ufe0fx", " \ufe0fx", "\n\u200dx", "\u2800\ufe0fx"])
def test_clean_drops_selector_without_a_base(text: str) -> None:
    assert sanitize.clean(text) == text.replace("\ufe0f", "").replace("\u200d", "")


# Every other line break becomes a newline instead of joining words or breaking invisibly.
@pytest.mark.parametrize("brk", ["\r\n", "\r", "\x0b", "\x0c", "\x85", "\u2028", "\u2029"])
def test_clean_maps_line_breaks_to_newlines(brk: str) -> None:
    assert sanitize.clean(f"a{brk}b") == "a\nb"


# The visible prepended concatenation marks (Arabic number signs, Kaithi) are format
# characters that real text needs.
@pytest.mark.parametrize("mark", sorted(sanitize._KEEP_CF - {"\u200c", "\u200d"}))
def test_clean_keeps_prepended_concatenation_marks(mark: str) -> None:
    assert sanitize.clean(f"{mark}\u0661\u0662") == f"{mark}\u0661\u0662"


# Emoji, Czech, Vietnamese (composed and decomposed), Persian, Hindi and Arabic survive as-is.
@pytest.mark.parametrize(
    "text",
    [
        "\U0001f44d\U0001f3fd",  # thumbs up, skin tone
        "\u2764\ufe0f",  # red heart
        "\U0001f468\u200d\U0001f469\u200d\U0001f467",  # family
        "1\ufe0f\u20e3",  # keycap 1
        "\u2764\ufe0f\u200d\U0001f525",  # heart on fire
        "\U0001f441\ufe0f\u200d\U0001f5e8\ufe0f",  # eye in speech bubble
        "\U0001f3f3\ufe0f\u200d\U0001f308",  # rainbow flag
        "\u263a\ufe0e",  # text-style smiley
        "Příliš žluťoučký kůň úpěl ďábelské ódy.",
        "Tiếng Việt có dấu: bảo mật, mật độ.",
        unicodedata.normalize("NFD", "Tiếng Việt có dấu: bảo mật, mật độ."),
        "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",  # Persian with ZWNJ
        "\u0915\u094d\u200d\u0937",  # Devanagari with ZWJ
        "\u0600\u0661\u0662",  # Arabic number sign
        "tab\tand\nnewline",
    ],
)
def test_clean_keeps_real_text(text: str) -> None:
    assert sanitize.clean(text) == text


# Filtering stays linear on a large clipboard full of selectors and joiners.
@pytest.mark.parametrize(
    "text",
    ["\ufe0f" * 50_000, "a\ufe0f\u200d" * 17_000, "\u200b\ufe0f" * 25_000, "\u2764\ufe0f" * 25_000],
    ids=["selectors", "emoji-runs", "split-runs", "hearts"],
)
def test_clean_is_fast_on_adversarial_input(text: str) -> None:
    started = time.perf_counter()
    sanitize.clean(text)
    assert time.perf_counter() - started < 2
