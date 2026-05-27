"""Script-aware text quality checks for phase handoffs (encoding, placeholders)."""

import re
import unicodedata

# Classic UTF-8 bytes misread as Latin-1/CP1252 (e.g. "ä¸­" instead of 中).
_MOJIBAKE_STRONG_MARKERS = (
    "\ufffd",
    "\u00e2\u20ac",
    "\u00e3\u20ac",
    "\u00ef\u00bc",
)
_MOJIBAKE_LATIN_MARKER_RE = re.compile(
    r"[\u00c2\u00c3][\u0080-\u00bf]|[\u00c3\u00a2\u20ac][\u00a3\u20ac]"
)
_MOJIBAKE_LEGACY_LATIN_RE = re.compile(
    r"(?:\u00c3|\u00c2|\u00d0|\u00d1)"
)

_CJK_LETTER_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
)
_HANDOFF_PLACEHOLDER_RE = re.compile(
    r"(?:"
    r"\b("
    r"pending completion|preparing palace writes|placeholder|todo|tbd|fix later|"
    r"research in progress|continuing to gather evidence|"
    r"need(?:s|ed)? minimum \d+ findings? before completion|"
    r"need(?:s|ed)? at least \d+ findings? before completion|"
    r"currently \d+ findings? persisted"
    r")\b|"
    r"(?:待完成|未完成|占位|研究中|继续收集|进度更新|稍后补充|待补充|"
    r"研究进行中|资料不足|待办)"
    r")",
    re.IGNORECASE,
)


def _letter_chars(text: str) -> list[str]:
    return [ch for ch in text if ch.isalpha()]


def _cjk_letter_ratio(text: str) -> float:
    letters = _letter_chars(text)
    if not letters:
        return 0.0
    cjk = sum(1 for ch in letters if _CJK_LETTER_RE.match(ch))
    return cjk / len(letters)


def _looks_like_mojibake(text: str) -> bool:
    """Detect encoding corruption without rejecting legitimate CJK handoffs."""

    value = str(text or "")
    if not value:
        return False
    if any(marker in value for marker in _MOJIBAKE_STRONG_MARKERS):
        return True
    if _MOJIBAKE_LATIN_MARKER_RE.search(value):
        return True
    cjk_ratio = _cjk_letter_ratio(value)
    if cjk_ratio >= 0.2:
        return False
    legacy = _MOJIBAKE_LEGACY_LATIN_RE.findall(value)
    if not legacy:
        return False
    joined = "".join(legacy)
    return len(legacy) >= 3 and len(joined) / max(1, len(value)) > 0.02


def _handoff_script_profile(text: str) -> str:
    """Return dominant script bucket for policy routing: latin, cjk, mixed."""

    letters = _letter_chars(text)
    if not letters:
        return "empty"
    cjk_ratio = _cjk_letter_ratio(text)
    if cjk_ratio >= 0.5:
        return "cjk"
    if cjk_ratio >= 0.15:
        return "mixed"
    return "latin"


def _normalize_handoff_text(text: str) -> str:
    return unicodedata.normalize("NFC", str(text or ""))


__all__ = [
    "_handoff_script_profile",
    "_looks_like_mojibake",
    "_normalize_handoff_text",
    "_HANDOFF_PLACEHOLDER_RE",
]
