"""Strips what no prompt needs and what does harm where Espanso pastes the text: control
characters, invisible and bidi format characters, stray emoji selectors and joiners, odd line
breaks. Applied to the draft before it is sent, to the reply, and to everything the triggers
print or the interface's Try tab shows. Split out of cli.py (#281), so the interface and the
trigger run share it without importing the CLI; it builds on redaction.DEFAULT_IGNORABLE."""

from __future__ import annotations

import re

from .redaction import DEFAULT_IGNORABLE

# Characters no prompt needs that do harm where Espanso pastes the text: C0/C1 controls other
# than tab and newline (an escape sequence can end a terminal's bracketed paste, so the lines
# after it run as commands), and every default-ignorable code point (invisible text that can
# smuggle instructions to the next model, or make text read differently than it is: zero-width
# and bidi characters, Unicode tags, Hangul fillers, variation selectors). The joiners and the
# emoji presentation selectors are judged by _keep_run() instead.
_UNSAFE_CHARS = re.compile(
    f"(?![\u200c\u200d\ufe0e\ufe0f])[\x00-\x08\x0b-\x1f\x7f-\x9f{DEFAULT_IGNORABLE}]"
)
# Other format characters (category Cf) are dropped as a class, so ones Unicode adds later are
# too, except the visible Arabic and Kaithi prepended concatenation marks.
_KEEP_CF = frozenset("\u200c\u200d\u0600\u0601\u0602\u0603\u0604\u0605\u06dd\u070f") | {
    "\u0890",
    "\u0891",
    "\u08e2",
    "\U000110bd",
    "\U000110cd",
}
# Line breaks other than \n would join or split lines differently where the text is pasted.
_LINE_BREAKS = str.maketrans(dict.fromkeys("\r\x0b\x0c\x85\u2028\u2029", "\n"))
# After a visible character, emoji need at most one text/emoji selector and one joiner
# (❤️‍🔥 is U+2764 U+FE0F U+200D U+1F525); a longer run could encode hidden bytes.
_SELECTOR_RUN = re.compile(r"[\u200c\u200d\ufe0e\ufe0f]+")
_ALLOWED_RUN = re.compile(r"[\ufe0e\ufe0f]?[\u200c\u200d]?")
# The only ASCII characters an emoji selector follows: keycaps (#️⃣, 1️⃣).
_KEYCAP_BASES = frozenset("#*0123456789")


def _keep_run(match: re.Match[str]) -> str:
    """Keep a selector/joiner run only in the form real text uses: one selector and one
    joiner, right after a visible character. After an ASCII character only a keycap's
    selector is kept, since no ASCII text needs a joiner, so English prose carries none."""
    run, start = match.group(), match.start()
    prev = match.string[start - 1] if start else " "
    if prev.isspace() or prev == "\u2800" or not _ALLOWED_RUN.fullmatch(run):
        return ""
    if prev.isascii() and (prev not in _KEYCAP_BASES or run not in ("\ufe0e", "\ufe0f")):
        return ""
    return run


def clean(text: str) -> str:
    text = _UNSAFE_CHARS.sub("", text.replace("\r\n", "\n").translate(_LINE_BREAKS))
    if not text.isascii():  # ASCII holds no format characters
        import unicodedata  # a native module on Windows: only for non-ASCII text (#312)

        text = "".join(c for c in text if c in _KEEP_CF or unicodedata.category(c) != "Cf")
    # After every other removal, so a dropped character cannot split a run into allowed pieces.
    return _SELECTOR_RUN.sub(_keep_run, text)
