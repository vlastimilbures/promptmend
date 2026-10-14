from __future__ import annotations

import base64
import binascii
import bisect
import re
import unicodedata
from collections.abc import Callable

# Patterns that suggest content should not leave the device for a cloud model.
# These are heuristics, not a guarantee: a draft blocked only for SOFT_FINDINGS can be sent
# once with --allow-flagged (-iok-), but false negatives are expected and the patterns
# should not be treated as a compliance control on their own.
#
# Every repeat is bounded ({1,64}, not +; tests/test_redaction.py checks each pattern): scan()
# tries each pattern at every position, so an unbounded class there makes a large clipboard
# quadratic and stalls Espanso before any request timeout applies. A value is matched only for
# its existence ({8}, not {8,}): search() needs no more, and nothing uses a match's span. A run
# that must be read whole (a JWT header, a PEM label) and is longer than its bound counts as a
# match, so a bound never hides a secret the unbounded pattern would have found.
#
# A secret name starts at a non-alphanumeric character; in camelCase, at an upper-case letter
# after a lower-case letter or digit (clientSecret, dbPassword); or, for the words that end a
# compound env name, anywhere (PGPASSWORD, GITHUBTOKEN, dbpassword). The camelCase lookarounds
# are case-sensitive even inside an IGNORECASE pattern, or MYAPIKEY would be MY + APIKEY.
_NAME_START = (
    r"(?:(?<![A-Za-z0-9])|(?-i:(?<=[a-z0-9])(?=[A-Z]))"
    r"|(?<=[A-Za-z0-9])(?=(?:password|passwd|secret|token)(?![a-z])))"
)
_PW_WORDS = r"(?:password|passwd|passphrase|pwd)"
_KEY_WORDS = (
    r"(?:secret[_-]?access[_-]?key|(?:secret|private|signing|encryption|account|access|master)"
    r"[_-]?key(?:[_-]?base)?|client[_-]?secret|secret|api[_-]?key"
    r"|(?:auth|access|refresh)[_-]?token|token)"
)
# name: value, name = "value", "name": "value", 'name' => 'value' (PHP), name := "value" (Go),
# define('NAME', 'value'), environ["NAME"] = "value". A comma counts only after a quote.
_ASSIGN = r"(?:[\"']\]?\s{0,32}(?:=>|:=|[:=,])|\]?\s{0,32}(?:=>|:=|[:=]))\s{0,32}[\"']?"
# The value must contain a digit, so prose such as "token: explanation" does not trip it.
_WITH_DIGIT = r"(?=[^\s\"']{0,256}\d)"
# A key or token also contains a letter, unlike a count or an ID (maxToken: 16384000).
_WITH_LETTER = r"(?=[^\s\"']{0,256}[A-Za-z])"
# A password in prose is not a description of one ("is base64-encoded", "is 6-digit").
_NOT_A_DESCRIPTION = r"(?![^\s\"']{0,64}-[a-z]{3,20}\b)"

# A confidentiality label as a line holds it: "Confidential", "Highly confidential",
# "Internal use only", Vietnamese "Mật" (secret), "Nội bộ" (internal).
_LABEL = (
    r"(?:(?:strictly|highly|company|proprietary and)\s)?"
    r"(?:confidential|restricted|internal(?: use)? only|tối mật|tuyệt mật|mật|nội bộ)"
)

_PATTERNS: dict[str, re.Pattern[str]] = {
    # Separators copied from PDFs and web pages: up to three spaces, tabs, dots, slashes,
    # underscores, hyphens, dashes or minus signs, or one line break (see _is_card).
    "payment_card": re.compile(
        r"\b(?:\d(?:[ \t./_\u2010-\u2015\u2212-]{1,3}|[ \t]?\r?\n)?){13,19}\b"
    ),
    # A CCCD citizen ID: checked against its province and century digits (see _is_cccd).
    "vietnam_id_12": re.compile(r"\b\d{12}\b"),
    # A 9-digit CMND or passport number, only next to a word that names the document; a bare
    # "ID" or "số" (number) is too common before ticket and contract numbers.
    "vietnam_id_9": re.compile(
        r"\b(?:CMND|CCCD|(?-i:CMT)|chứng minh (?:nhân dân|thư)|căn cước(?: công dân)?|hộ chiếu"
        r"|passport|national ID|ID card)\D{0,20}\d{9}\b",
        re.IGNORECASE,
    ),
    "email": re.compile(r"\b[\w.+-]{1,64}@[\w-]{1,63}\.[\w.-]{1,253}\b"),
    # Vendor keys contain '-'/'_' after a short prefix (sk-or-v1-, sk-ant-api03-, sk-proj-),
    # so the key bodies allow both.
    "openrouter_key": re.compile(r"\bsk-or-v1-[0-9a-f]{32}"),
    "anthropic_key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20}"),
    "openai_key": re.compile(r"\bsk-(?!or-|ant-)[A-Za-z0-9_-]{20}"),
    "stripe_key": re.compile(r"\b[spr]k_(?:live|test)_[A-Za-z0-9]{16}"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22})"),
    "gitlab_token": re.compile(r"\bglpat-[A-Za-z0-9_-]{20}"),
    "huggingface_token": re.compile(r"\bhf_[A-Za-z0-9]{34}"),
    "slack_token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10}"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    "xai_key": re.compile(r"\bxai-[A-Za-z0-9]{20}"),
    # AKIA: long-term keys; ASIA: temporary (STS) keys.
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # header.payload is enough: the header's run must end at its dot, so it is read whole
    # (possessive: no backtracking into it), and one longer than the bound counts as a match.
    # Past the dot, a payload that is base64url JSON too (eyJ + 7) shows it exists; the
    # signature is not needed.
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,8192}+(?:\.eyJ[A-Za-z0-9_-]{7}|[A-Za-z0-9_-])"),
    # PEM, OpenSSH and PGP (`-----BEGIN PGP PRIVATE KEY BLOCK-----`) private keys.
    # A label longer than the bound counts as a match.
    "pem_private_key": re.compile(
        r"-----BEGIN (?:[A-Z ]{0,64}PRIVATE KEY(?: BLOCK)?-----|[A-Z ]{65})"
    ),
    # The token's first 16 characters, one of them a letter, digit or underscore.
    "bearer_token": re.compile(
        r"\bBearer\s{1,256}(?=[.-]{0,15}[A-Za-z0-9_])[A-Za-z0-9._-]{16}", re.IGNORECASE
    ),
    # scheme://user:password@host
    "url_credentials": re.compile(
        r"\b[a-z][a-z0-9+.-]{0,31}://[^\s/:@]{0,256}:[^\s/@]{1,256}@", re.IGNORECASE
    ),
    # password=..., DB_PASSWORD: ..., "clientSecret": "...", SECRET_KEY = '...'. The name may
    # follow '_' or '-' (access_token, AWS_SECRET_ACCESS_KEY), where \b would not match. A
    # password needs 6 characters with a digit, any other secret 8 with a digit and a letter.
    "secret_assignment": re.compile(
        _NAME_START
        + rf"(?:{_PW_WORDS}{_ASSIGN}{_NOT_A_DESCRIPTION}{_WITH_DIGIT}[^\s\"']{{6}}"
        + rf"|{_KEY_WORDS}{_ASSIGN}{_WITH_DIGIT}{_WITH_LETTER}[^\s\"']{{8}})",
        re.IGNORECASE,
    ),
    # "the password for the admin account is: Winter2026!", and the Vietnamese "mật khẩu (wifi)
    # là ..." or "mật khẩu đăng nhập: ...". Up to a few words may sit between name and value.
    "password_value": re.compile(
        _NAME_START
        + rf"{_PW_WORDS}(?:\s{{1,4}}[^\s:=]{{1,20}}){{0,4}}?\s{{1,4}}(?:is|was)(?:\s{{0,4}}:)?"
        + rf"\s{{1,4}}[\"']?{_NOT_A_DESCRIPTION}{_WITH_DIGIT}[^\s\"']{{6}}"
        + r"|\b(?:mật\s{1,4}khẩu|mk)(?:\s{1,4}[^\s:=]{1,20}){0,3}?\s{0,4}(?:là|[:=])\s{0,4}[\"']?"
        + rf"{_WITH_DIGIT}[^\s\"']{{6}}",
        re.IGNORECASE,
    ),
    # The base64 must decode to user:password (see _is_basic_auth), or "Basic understanding"
    # would match.
    "basic_auth": re.compile(r"\bBasic\s{1,4}([A-Za-z0-9+/]{4,512}={0,2})", re.IGNORECASE),
    # curl -u user:password, --user=user:password, also after a "\" line continuation; "-u
    # user" alone makes curl prompt instead. Anchored on the option, which is rare, rather
    # than on "curl"; _after_curl() then checks the command it belongs to.
    "curl_user": re.compile(r"\s(?:-u|--user)(?:\s{1,4}|=)?[\"']?[^\s:\"']{1,64}:(?!\$)[^\s\"']"),
    "npm_token": re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),
    # Azure storage/service-bus connection strings and SAS URL signatures.
    "azure_key": re.compile(
        r"(?:AccountKey|SharedAccessKey)=[A-Za-z0-9+/]{20}|[?&;]sig=[A-Za-z0-9%+/=]{20}",
        re.IGNORECASE,
    ),
    # Checked against the country's length and the mod-97 checksum (see _is_iban). A lookahead,
    # so every start is a candidate and an invalid IBAN cannot swallow a valid one after it.
    "iban": re.compile(r"(?=\b([A-Z]{2}\d{2}(?:[ -]{0,2}[A-Z0-9]){11,30})\b)", re.IGNORECASE),
    # A classification label, not the word in prose ("Output restricted to 5 bullets", "an
    # NDA defines confidential information", "bảo mật" = security, "mật độ" = density):
    # upper case, at the start of a line followed by ':' or a dash, in brackets, or a phrase
    # used only as a label. Vietnamese: "tài liệu mật" (secret document), "tối mật" (top
    # secret), "lưu hành nội bộ" (internal circulation); not "mật khẩu" (password).
    "confidential_label": re.compile(
        # Upper case anywhere.
        r"(?-i:\b(?:CONFIDENTIAL|RESTRICTED|INTERNAL(?: USE)? ONLY|DO NOT DISTRIBUTE"
        r"|TỐI MẬT|TUYỆT MẬT|MẬT|NỘI BỘ)\b)"
        # Alone on a line ("# Confidential", "Nội bộ"), or opening one before ':' or a dash
        # ("**Confidential**: Q3", "Restricted - board only"; not "Restricted-access").
        rf"|^[^\w\n]{{0,6}}{_LABEL}[^\w\n]{{0,6}}$"
        rf"|^[^\w\n]{{0,6}}{_LABEL}[*_\"]{{0,3}}\s{{0,2}}(?::|\s-|-(?!\w)|[\]\)\u2013\u2014])"
        r"|\[(?:strictly confidential|confidential|restricted|internal(?: use)? only)\]"
        r"|\b(?:internal(?: use)? only|(?:strictly|highly|company) confidential"
        r"|proprietary and confidential|do not distribute|not for distribution)\b"
        # A classification field: "Classification: Restricted", "Độ mật: Mật".
        r"|\b(?:classification|sensitivity|độ mật)[*_\"]{0,3}\s{0,2}:\s{0,2}[*_\"]{0,3}"
        r"(?:highly confidential|confidential|restricted|internal|secret|tối mật|tuyệt mật|mật)\b"
        r"|\b(?:tài liệu|văn bản|thông tin)\s{1,2}(?:tối mật|tuyệt mật|mật)\b"
        r"(?!\s{1,2}(?:khẩu|độ|ong|mã|thiết|thư|mía))"
        r"|\b(?:tối mật|tuyệt mật|lưu hành nội bộ)\b",
        re.IGNORECASE | re.MULTILINE,
    ),
    # An email next to a password-like word: "jane@example.com:Summer2024!", "login
    # jane@example.com / Summer2024". Hard, unlike a bare email.
    "credential_pair": re.compile(
        r"\b[\w.+-]{1,64}@[\w-]{1,63}\.[\w.-]{1,253}(?:\s{0,4}[:/,|]\s{0,4}|\s{1,4})"
        rf"{_NOT_A_DESCRIPTION}{_WITH_DIGIT}{_WITH_LETTER}[^\s\"']{{6}}"
    ),
}


def _luhn_prefixes(digits: str) -> tuple[list[int], list[int]]:
    """Prefix sums of each digit's Luhn value, weighted by its position from the right of
    `digits`; the second list is for windows whose right edge is an odd number of digits
    in from the end, which flips every weight."""
    sums: tuple[list[int], list[int]] = ([0], [0])
    for t, ch in enumerate(digits):
        for flip, prefix in enumerate(sums):
            d = int(ch) * (2 if (len(digits) - 1 - t + flip) % 2 else 1)
            prefix.append(prefix[-1] + (d - 9 if d > 9 else d))
    return sums


def _is_card(match: re.Match[str]) -> bool:
    """Whether 13-19 digits in consecutive digit groups pass the Luhn checksum. The regex
    is greedy, so a card followed by "\n2." or next to a short number is one match;
    checking each group window finds the card inside it. A card wraps over one line at
    most. A match holds at most 19 digits, so only a few windows are long enough."""
    digits, starts, ends, lines = "", [], [], []
    line = 0
    for part in re.split(r"(\D+)", match.group()):
        if part.isdigit():
            starts.append(len(digits))
            digits += part
            ends.append(len(digits))
            lines.append(line)
        else:
            line += part.count("\n")
    sums = _luhn_prefixes(digits)
    # A card's groups have 4+ digits except maybe the last (4-4-4-4, 4-6-5, 4-4-4-4-3); a window
    # of shorter groups is a phone list or dates, where some window passes Luhn by chance.
    next_short = [len(ends)] * (len(ends) + 1)
    for k in range(len(ends) - 1, -1, -1):
        next_short[k] = k if ends[k] - starts[k] < 4 else next_short[k + 1]
    for i, start in enumerate(starts):
        first = bisect.bisect_left(ends, start + 13, i)
        last = min(bisect.bisect_right(ends, start + 19, i), next_short[i] + 1)
        for j in range(first, last):
            prefix = sums[(len(digits) - ends[j]) % 2]
            if lines[j] - lines[i] <= 1 and (prefix[ends[j]] - prefix[start]) % 10 == 0:
                return True
    return False


def _after_curl(match: re.Match[str]) -> bool:
    """Whether the -u option belongs to a curl command: "curl" earlier in the same logical
    line (lines joined by a trailing backslash), within 256 characters."""
    before = match.string[max(0, match.start() - 256) : match.start()]
    if "curl" not in before.lower():
        return False
    command = re.split(r"(?<!\\)\r?\n", before)[-1]
    return re.search(r"\bcurl\b", command, re.IGNORECASE) is not None


def _is_basic_auth(match: re.Match[str]) -> bool:
    """Whether the Basic credentials decode to printable UTF-8 user:password."""
    encoded = match.group(1)
    try:
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
        text = decoded.decode("utf-8")
    except (binascii.Error, ValueError):  # UnicodeDecodeError is a ValueError
        return False
    return text.isprintable() and ":" in text


# Text before a match that puts it inside an AWS ARN, with no whitespace in between.
_INSIDE_ARN = re.compile(r"\barn:aws[\w-]{0,20}:\S{0,256}$", re.IGNORECASE)

# CCCD province codes (the first three digits; Circular 07/2016/TT-BCA), still valid for
# cards issued before the 2025 province merger.
_CCCD_PROVINCES = frozenset(
    f"{code:03}"
    for code in (
        1, 2, 4, 6, 8, 10, 11, 12, 14, 15, 17, 19, 20, 22, 24, 25, 26, 27, 30, 31, 33, 34, 35,
        36, 37, 38, 40, 42, 44, 45, 46, 48, 49, 51, 52, 54, 56, 58, 60, 62, 64, 66, 67, 68, 70,
        72, 74, 75, 77, 79, 80, 82, 83, 84, 86, 87, 89, 91, 92, 93, 94, 95, 96,
    )
)  # fmt: skip


def _is_cccd(match: re.Match[str]) -> bool:
    """Whether 12 digits are shaped like a CCCD: a province code, then a sex/century digit
    (0-3 for births in 1900-2099). Digits inside an AWS ARN (arn:aws:iam::<account>:role/x)
    are not one, whatever they are."""
    digits = match.group()
    if digits[:3] not in _CCCD_PROVINCES or digits[3] not in "0123":
        return False
    before = match.string[max(0, match.start() - 300) : match.start()]
    return not _INSIDE_ARN.search(before)


# IBAN length per country (SWIFT IBAN registry).
_IBAN_LENGTHS = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22, "BH": 22,
    "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DK": 18, "DO": 28,
    "EE": 20, "EG": 29, "ES": 24, "FI": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23,
    "GL": 18, "GR": 27, "GT": 28, "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23, "IS": 26,
    "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28, "LC": 32, "LI": 21, "LT": 20, "LU": 20,
    "LV": 21, "LY": 25, "MC": 27, "MD": 24, "ME": 22, "MK": 19, "MR": 27, "MT": 31, "MU": 30,
    "NL": 18, "NO": 15, "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22,
    "SA": 24, "SC": 31, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "ST": 25, "SV": 28, "TL": 23,
    "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20, "BI": 27, "DJ": 27, "FK": 18,
    "HN": 28, "MN": 20, "NI": 28, "OM": 23, "RU": 33, "SD": 18, "SO": 23, "YE": 30,
}  # fmt: skip


# IBAN letters as the numbers mod-97 reads them: A=10 ... Z=35.
_IBAN_LETTERS = {ord(c): str(ord(c) - 55) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}


def _is_iban(match: re.Match[str]) -> bool:
    """Whether the match starts with an IBAN of its country's length and a valid mod-97
    checksum. The regex is greedy, so a following word is cut off by the length."""
    candidate = match.group(1)
    length = _IBAN_LENGTHS.get(candidate[:2].upper())
    if length is None:
        return False
    iban = candidate.replace(" ", "").replace("-", "")[:length].upper()
    if len(iban) < length:
        return False
    return int((iban[4:] + iban[:4]).translate(_IBAN_LETTERS)) % 97 == 1


# Patterns whose raw regex match is too broad: a finding needs one match that also
# passes the validator. Every match is checked, so a failing candidate cannot hide a
# later valid one.
_VALIDATORS: dict[str, Callable[[re.Match[str]], bool]] = {
    "payment_card": _is_card,
    "vietnam_id_12": _is_cccd,
    "basic_auth": _is_basic_auth,
    "curl_user": _after_curl,
    "iban": _is_iban,
}


# Separator between user-defined regexes in PROMPT_EXTRA_PATTERNS. A literal ';' inside a
# pattern can be written as \x3b.
EXTRA_SEPARATOR = ";"


class InvalidExtraPattern(ValueError):
    """A PROMPT_EXTRA_PATTERNS entry that is not a valid regex. It names the position, never
    the pattern, which may itself describe sensitive data: counted from 1 over the non-empty
    entries, as scan() numbers the custom_N findings."""

    def __init__(self, entry: int) -> None:
        super().__init__(
            f"PROMPT_EXTRA_PATTERNS entry {entry} (custom_{entry}) is not a valid regex"
        )
        self.entry = entry


def compile_extra(spec: str) -> tuple[re.Pattern[str], ...]:
    """Compile PROMPT_EXTRA_PATTERNS (`;`-separated regexes, case-insensitive). Settings
    already rejects an invalid one (config._regexes); this check stays for a Settings built
    without its parsers."""
    patterns = []
    for i, raw in enumerate((p.strip() for p in spec.split(EXTRA_SEPARATOR) if p.strip()), 1):
        try:
            patterns.append(re.compile(raw, re.IGNORECASE))
        # A repeat count past the engine's limit raises OverflowError, deep nesting
        # RecursionError: invalid entries too.
        except (re.error, OverflowError, RecursionError) as exc:
            raise InvalidExtraPattern(i) from exc
    return tuple(patterns)


# Unicode's Default_Ignorable_Code_Point property (DerivedCoreProperties 16.0): code points
# that render as nothing, assigned or not. Format characters (Cf) are only part of it; it also
# holds the combining grapheme joiner, Hangul fillers, Mongolian and other variation
# selectors, and unassigned blocks such as U+E0080-E0FFF. A character class body, shared
# with cli.clean().
DEFAULT_IGNORABLE = (
    "\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e"
    "\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8"
    "\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff"
)
# Plus the braille blank, which looks like a space and can split a number as well.
_INVISIBLE = re.compile(f"[{DEFAULT_IGNORABLE}\u2800]")


def _normalize(text: str) -> str:
    """Fold look-alike characters so they cannot split a pattern: NFKC turns no-break and
    thin spaces into spaces and fullwidth digits/letters into ASCII, and invisible characters
    (zero-width space/joiner, soft hyphen, variation selectors, Hangul fillers, any other
    format character) are dropped."""
    folded = _INVISIBLE.sub("", unicodedata.normalize("NFKC", text))
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")


# Longest raw value an error message repeats back as-is.
SHOWN_MAX_CHARS = 40

# The user's PROMPT_EXTRA_PATTERNS, compiled, from the settings resolved last
# (config.ConfigLayers.resolve(), before any other value is parsed), so error messages hide a
# value they match too. Replaced
# by one assignment, so a reader sees the old or the new tuple, never a mix.
_user_patterns: tuple[re.Pattern[str], ...] = ()


def set_user_patterns(patterns: tuple[re.Pattern[str], ...]) -> None:
    global _user_patterns
    _user_patterns = patterns


def safe_repr(raw: str, *, user_patterns: bool = True) -> str:
    """How an error message quotes a value it rejects. Errors are pasted into whatever app
    has focus, so a value that may hold a secret is described instead of repeated: a long
    one, one that scan() flags (with the user's patterns, see set_user_patterns, unless
    ``user_patterns`` is false: for showing PROMPT_EXTRA_PATTERNS itself, which may match
    its own text), or one containing '=' (two .env lines run together)."""
    extra = _user_patterns if user_patterns else ()
    if len(raw) <= SHOWN_MAX_CHARS and "=" not in raw and not scan(raw, extra):
        return repr(raw)
    merged = "; two .env lines may have run together" if "=" in raw else ""
    return f"<redacted, {len(raw)} chars{merged}>"


_WORD = re.compile(r"[^\s'\"(),\[\]]+")


def redact_words(text: str) -> str:
    """``text`` with every word scan() flags described instead of repeated: for messages
    that quote what was typed (a usage error naming a stray argument), which may be a key."""
    return _WORD.sub(
        lambda m: (
            f"<redacted, {len(m.group())} chars>" if scan(m.group(), _user_patterns) else m.group()
        ),
        text,
    )


def redact_message(text: str) -> str:
    """redact_words(), and the whole ``text`` described when what is left still holds a
    secret spread over several words (``Bearer …``, ``password: …``, a spaced card number)."""
    text = redact_words(text)
    return f"<redacted, {len(text)} chars>" if scan(text, _user_patterns) else text


# Single tokens that are not secrets: a URL, an email, a path, a UUID, a lower-case slug
# (owner/model), host or file name (example.com:8080, notes2026.md), a version, a date or a
# hex hash. Case-sensitive, so a mixed-case secret that merely contains '/' or '.' (half of
# all base64 keys) is not excused.
_NOT_A_TOKEN = re.compile(
    r"[a-z][a-z0-9+.-]{0,31}://\S{0,200}"
    r"|[\w.+-]{1,64}@[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63}){0,8}\.[a-z]{2,24}"
    r"|(?:~|\.{1,2})?/[\w.-]{1,64}(?:/[\w.-]{1,64}){0,30}/?"
    r"|[A-Za-z]:\\[\w.-]{0,64}(?:\\[\w.-]{1,64}){0,30}"
    r"|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
    r"|[a-z0-9._-]{1,100}/[a-z0-9./_-]{1,100}"
    r"|[a-z0-9_-]{1,63}(?:\.[a-z0-9_-]{1,63}){0,8}\.[a-z]{2,10}(?::\d{1,5})?"
    r"|v?\d{1,9}(?:\.\d{1,9}){1,3}(?:[-+][\w.-]{1,64})?"
    r"|\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
    r"|[0-9a-f]{8,128}"
)
# Characters an identifier uses; anything else non-alphanumeric is a symbol.
_IDENTIFIER_PUNCTUATION = frozenset("_-.")


def _char_class(ch: str) -> str:
    if ch.isdigit():
        return "digit"
    if ch.islower():
        return "lower"
    if ch.isupper():
        return "upper"
    return "" if ch in _IDENTIFIER_PUNCTUATION else "symbol"


def is_bare_token(text: str) -> bool:
    """Whether the whole draft is one password- or token-like word, such as a vault password
    left on the clipboard: no whitespace, 8-200 characters, a digit and at least three
    character classes (lower, upper, digit, symbol). A single word is never a prompt, so a
    mixed-case identifier with a digit (getUser2FA) is flagged too; URLs, paths, emails,
    UUIDs, hashes, versions, dates and lower-case slugs and file names are not."""
    token = text.strip()
    if not 8 <= len(token) <= 200 or any(ch.isspace() for ch in token):
        return False
    if _NOT_A_TOKEN.fullmatch(token):
        return False
    classes = {c for c in map(_char_class, token) if c}
    return "digit" in classes and len(classes) >= 3


def scan(text: str, extra: tuple[re.Pattern[str], ...] = ()) -> list[str]:
    """Return the names of any sensitive patterns found in text.

    Both the raw and the normalized text are scanned, so normalization can only add
    findings. User-defined ``extra`` patterns are reported as custom_1, custom_2, ... so
    the pattern text never appears in the output.
    """
    texts = dict.fromkeys((text, _normalize(text)))

    def found(pattern: re.Pattern[str], validate: Callable[[re.Match[str]], bool] | None) -> bool:
        if validate is None:
            return any(pattern.search(t) for t in texts)
        return any(validate(m) for t in texts for m in pattern.finditer(t))

    findings = [name for name, p in _PATTERNS.items() if found(p, _VALIDATORS.get(name))]
    findings += [f"custom_{i}" for i, p in enumerate(extra, 1) if found(p, None)]
    return findings


# Findings a user may send once with --allow-flagged (-iok-): labels and identifiers that are
# often harmless in context. Everything else (keys, tokens, passwords, cards, private keys,
# bare_token and the user's own patterns) is never sent that way.
SOFT_FINDINGS = frozenset({"confidential_label", "vietnam_id_12", "vietnam_id_9", "email", "iban"})


def scan_draft(text: str, extra: tuple[re.Pattern[str], ...] = ()) -> list[str]:
    """scan(), plus `bare_token` when the whole draft is one secret-like word. Only the gate
    uses it: safe_repr() must still quote a short single-word setting value as-is."""
    return scan(text, extra) + (["bare_token"] if is_bare_token(text) else [])
