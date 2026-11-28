"""The gate's locale-specific pattern packs (PROMPT_GATE_PACKS, #231): the default finds
exactly what the built-in patterns found before they were a pack, and turning a pack off
removes only its own patterns."""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import pytest
from conftest import resolved_settings
from test_redaction import MUST_DETECT, MUST_PASS

from promptmend import doctor, redaction, settings_checks
from promptmend.config import Settings
from promptmend.factory import make_provider
from promptmend.gate import GateBlocked, GatedProvider
from promptmend.providers.base import Provider
from promptmend.redaction import GATE_PACKS, SOFT_FINDINGS, scan, scan_draft

# confidential_label as it was before the vn pack split it in two (0.26.0), verbatim: the
# parity tests check the two rules that replace it against it.
_OLD_LABEL = (
    r"(?:(?:strictly|highly|company|proprietary and)\s)?"
    r"(?:confidential|restricted|internal(?: use)? only|tối mật|tuyệt mật|mật|nội bộ)"
)
_OLD_CONFIDENTIAL_LABEL = re.compile(
    r"(?-i:\b(?:CONFIDENTIAL|RESTRICTED|INTERNAL(?: USE)? ONLY|DO NOT DISTRIBUTE"
    r"|TỐI MẬT|TUYỆT MẬT|MẬT|NỘI BỘ)\b)"
    rf"|^[^\w\n]{{0,6}}{_OLD_LABEL}[^\w\n]{{0,6}}$"
    rf"|^[^\w\n]{{0,6}}{_OLD_LABEL}[*_\"]{{0,3}}\s{{0,2}}(?::|\s-|-(?!\w)|[\]\)\u2013\u2014])"
    r"|\[(?:strictly confidential|confidential|restricted|internal(?: use)? only)\]"
    r"|\b(?:internal(?: use)? only|(?:strictly|highly|company) confidential"
    r"|proprietary and confidential|do not distribute|not for distribution)\b"
    r"|\b(?:classification|sensitivity|độ mật)[*_\"]{0,3}\s{0,2}:\s{0,2}[*_\"]{0,3}"
    r"(?:highly confidential|confidential|restricted|internal|secret|tối mật|tuyệt mật|mật)\b"
    r"|\b(?:tài liệu|văn bản|thông tin)\s{1,2}(?:tối mật|tuyệt mật|mật)\b"
    r"(?!\s{1,2}(?:khẩu|độ|ong|mã|thiết|thư|mía))"
    r"|\b(?:tối mật|tuyệt mật|lưu hành nội bộ)\b",
    re.IGNORECASE | re.MULTILINE,
)

_WORDS = [
    "confidential",
    "restricted",
    "internal only",
    "internal use only",
    "do not distribute",
    "not for distribution",
    "secret",
    "internal",
    "highly confidential",
    "strictly confidential",
    "company confidential",
    "proprietary and confidential",
    "mật",
    "tối mật",
    "tuyệt mật",
    "nội bộ",
    "lưu hành nội bộ",
    "tài liệu mật",
    "văn bản tối mật",
    "thông tin mật",
    "thông tin mật khẩu",
    "bảo mật",
    "mật độ",
    "mật khẩu",
    "classification: mật",
    "sensitivity: restricted",
    "độ mật: confidential",
    "độ mật: tối mật",
    "**Độ mật**: Mật",
    "classification: secret",
]
# Line breaks (LF, CRLF, a label mid-document), a no-break space and look-alikes that
# _normalize() folds (zero-width space) around each word.
_BEFORE = [
    "",
    "# ",
    "**",
    "> ",
    "[",
    "Tiêu đề: ",
    "the ",
    "(",
    "x\n",
    "Intro\r\n",
    "Q3 plan\n\nSection 2\n",
    "\u00a0",
    "\u200b",
    "Strictly ",
    "Company ",
]
_AFTER = [
    "",
    ":",
    " - board",
    "**:",
    "\nKế hoạch",
    ")",
    "]",
    " ong",
    " information",
    "-access",
    "\r\nBody",
    "\nBody\nmore",
    "\u00a0:",
    "\u200b:",
]
# Labels split or disguised inside the word (zero-width, fullwidth, no-break space), and
# multi-line documents with a label mid-way.
_SPECIAL = [
    "T\u200bỐI MẬT",
    "\uff2dẬ\uff34",
    "Nội\u00a0bộ",
    "tài liệu\u00a0mật",
    "\uff23\uff2f\uff2e\uff26\uff29\uff24\uff25\uff2e\uff34\uff29\uff21\uff2c",
    "Strictly mật: kế hoạch",
    "Highly nội bộ",
    "Report\nNội bộ\nBody",
    "Report\r\n# Confidential\r\nBody",
    "Report\n**Độ mật**: Tối mật\nend",
    "Intro\n\nMẬT - Kế hoạch\n\nEnd",
    "line one\r\nThông tin mật: x\r\nline three",
]


def _corpus() -> list[str]:
    texts = [p.values[0] for p in (*MUST_DETECT, *MUST_PASS)] + _SPECIAL
    texts.extend(
        f"{before}{case}{after}"
        for word, before, after in itertools.product(_WORDS, _BEFORE, _AFTER)
        for case in (word, word.upper(), word.title())
    )
    return [t for t in texts if isinstance(t, str)]


_CORPUS = _corpus()


def _old_label(text: str) -> bool:
    return any(_OLD_CONFIDENTIAL_LABEL.search(t) for t in {text, redaction._normalize(text)})


# The vn pack's ID rules are the 0.26.0 patterns, verbatim (pattern text and flags).
def test_vn_id_rules_unchanged() -> None:
    rules = {r.name: r for r in redaction._RULES if r.name.startswith("vietnam_id")}
    old_12 = re.compile(r"\b\d{12}\b")
    old_9 = re.compile(
        r"\b(?:CMND|CCCD|(?-i:CMT)|chứng minh (?:nhân dân|thư)|căn cước(?: công dân)?|hộ chiếu"
        r"|passport|national ID|ID card)\D{0,20}\d{9}\b",
        re.IGNORECASE,
    )
    for name, old in (("vietnam_id_12", old_12), ("vietnam_id_9", old_9)):
        assert rules[name].pattern.pattern == old.pattern
        assert rules[name].pattern.flags == old.flags
        assert rules[name].pack == "vn"
    assert redaction._VALIDATORS["vietnam_id_12"] is redaction._is_cccd


# With every pack on (the default, PROMPT_GATE_PACKS=vn) the split confidential_label finds
# exactly what the single pattern did, over the redaction tests' drafts and every label word
# in every case and position; vietnam_id_* kept their patterns unchanged.
def test_default_packs_flag_labels_as_before() -> None:
    assert resolved_settings().gate_packs == GATE_PACKS
    differ = [t for t in _CORPUS if _old_label(t) != ("confidential_label" in scan(t))]
    assert differ == []
    # The corpus holds both outcomes, and labels only the vn pack's rule matches.
    flagged = [t for t in _CORPUS if _old_label(t)]
    assert 0 < len(flagged) < len(_CORPUS)
    assert any("confidential_label" not in scan(t, packs=()) for t in flagged)


# The default is the whole pack list, so scan() without packs, the gate's default and the
# setting's default agree; finding names and their order are unchanged.
def test_default_packs_are_every_pack() -> None:
    for text in _CORPUS:
        assert scan(text) == scan(text, packs=GATE_PACKS) == scan(text, packs=("vn",))
    assert scan("CONFIDENTIAL: CCCD 001099012345, jane@example.com") == [
        "vietnam_id_12",
        "email",
        "confidential_label",
    ]


# Turning the packs off removes only their findings: with packs=() a draft is flagged for
# everything it was flagged for before except vietnam_id_* and a label only Vietnamese
# wording matched. A Vietnamese password stays flagged, and so does an English label.
def test_no_packs_removes_only_pack_findings() -> None:
    for text in _CORPUS:
        on, off = scan(text), scan(text, packs=())
        assert set(off) <= set(on)
        assert set(on) - set(off) <= {"vietnam_id_12", "vietnam_id_9", "confidential_label"}
    assert scan("CCCD 001099012345", packs=()) == []
    assert scan("Số CMND 123456789 của anh ấy", packs=()) == []
    assert scan("Đây là tài liệu tối mật", packs=()) == []
    assert scan("MẬT\nKế hoạch", packs=()) == []
    assert scan("Độ mật: confidential", packs=()) == []
    assert scan("CONFIDENTIAL: Q3", packs=()) == ["confidential_label"]
    assert scan("Classification: Restricted", packs=()) == ["confidential_label"]
    assert "password_value" in scan("mật khẩu wifi là Abc12345", packs=())


# scan() refuses a pack it does not know instead of silently scanning without it.
def test_scan_rejects_unknown_pack() -> None:
    with pytest.raises(ValueError, match="unknown gate pack: xx"):
        scan("text", packs=("vn", "xx"))


# Pack findings stay soft, as before: -iok- may send them once.
def test_pack_findings_stay_soft() -> None:
    assert {"vietnam_id_12", "vietnam_id_9", "confidential_label"} <= SOFT_FINDINGS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("vn", ("vn",)), ("", ()), (" VN , vn ", ("vn",)), (",", ())],
    ids=["vn", "empty", "spaced-duplicate", "commas"],
)
def test_setting_parses(tmp_path: Path, raw: str, expected: tuple[str, ...]) -> None:
    (tmp_path / ".env").write_text(f"PROMPT_GATE_PACKS={raw!r}\n".replace("'", '"'))
    assert Settings.load().gate_packs == expected


# An unknown pack name fails closed with an error naming the variable, never a pack left out.
def test_setting_rejects_unknown_pack(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("PROMPT_GATE_PACKS=vn,us\n")
    with pytest.raises(ValueError, match="PROMPT_GATE_PACKS") as caught:
        Settings.load()
    assert "comma-separated list of vn" in str(caught.value)


class _Stub:
    def generate(self, prompt: str, system_prompt: str) -> str:
        return "ok"


# The gate scans with the packs it is given; its default is every pack.
def test_gate_uses_its_packs() -> None:
    inner: Provider = _Stub()
    with pytest.raises(GateBlocked, match="a 12-digit number shaped like a Vietnamese ID"):
        GatedProvider(inner, allow_override=False).generate("CCCD 001099012345", "s")
    assert GatedProvider(inner, allow_override=False, packs=()).generate("CCCD 001099012345", "s")
    assert scan_draft("CCCD 001099012345", packs=()) == []


# make_provider() hands every gate the user's PROMPT_GATE_PACKS.
@pytest.mark.parametrize("packs", ["vn", ""])
def test_make_provider_passes_packs(monkeypatch: pytest.MonkeyPatch, packs: str) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("PROMPT_GATE_PACKS", packs)
    provider = make_provider("openrouter", Settings.load())
    assert isinstance(provider, GatedProvider)
    assert tuple(provider._packs) == Settings.load().gate_packs


# doctor, `config validate` and the interface scan the persona with the same packs.
def test_persona_findings_use_packs() -> None:
    persona = "HR lead, CCCD 001099012345"
    assert settings_checks.persona_findings(Settings(persona=persona)) == ["vietnam_id_12"]
    assert settings_checks.persona_findings(Settings(persona=persona, gate_packs=())) == []


# doctor's gate_packs check and `config validate` note a pack that is off (an empty value,
# or an empty ${VAR} it expanded from) as information; every pack on is ok.
def test_doctor_notes_a_pack_that_is_off() -> None:
    on = doctor._gate_packs_check(resolved_settings())
    assert (on.status, on.data) == (doctor.OK, {"packs": ["vn"], "off": []})
    off = doctor._gate_packs_check(Settings(gate_packs=()))
    assert (off.status, off.data) == (doctor.INFO, {"packs": [], "off": ["vn"]})
    assert off.message == (
        "gate pack vn is off (PROMPT_GATE_PACKS): the gate does not flag Vietnamese national "
        "ID numbers and confidentiality labels"
    )
    assert settings_checks.gate_packs_note(resolved_settings()) is None


@pytest.mark.parametrize("packs", ["", "vn"])
def test_config_validate_notes_a_pack_that_is_off(tmp_path: Path, packs: str) -> None:
    from typer.testing import CliRunner

    from promptmend.cli import app

    (tmp_path / ".env").write_text(f'PROMPT_GATE_PACKS="{packs}"\n')
    result = CliRunner().invoke(app, ["config", "validate"])
    assert result.exit_code == 0, result.output
    assert ("note: gate pack vn is off" in result.output) is (packs == "")
