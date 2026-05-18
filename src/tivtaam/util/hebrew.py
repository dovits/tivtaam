"""Hebrew text normalization helpers used by matching and DB indexing."""

from __future__ import annotations

import re
import unicodedata

_NIQQUD_RANGE = (0x0591, 0x05C7)
_BIDI_MARKS = {"‎", "‏", "‪", "‫", "‬", "‭", "‮", "⁦", "⁧", "⁨", "⁩"}
_GERESH_VARIANTS = {"׳": "'", "״": '"', "‘": "'", "’": "'", "“": '"', "”": '"'}
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_niqqud(s: str) -> str:
    return "".join(c for c in s if not (_NIQQUD_RANGE[0] <= ord(c) <= _NIQQUD_RANGE[1]))


def _strip_bidi(s: str) -> str:
    return "".join(c for c in s if c not in _BIDI_MARKS)


def _normalize_geresh(s: str) -> str:
    return "".join(_GERESH_VARIANTS.get(c, c) for c in s)


def normalize(text: str) -> str:
    """NFC, strip niqqud + bidi marks, normalize geresh, collapse whitespace, lowercase Latin."""
    if not text:
        return ""
    s = unicodedata.normalize("NFC", text)
    s = _strip_bidi(s)
    s = _strip_niqqud(s)
    s = _normalize_geresh(s)
    s = s.replace("\xa0", " ")
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s.lower()


_TOKEN_RE = re.compile(r"[֐-׿\w%]+", re.UNICODE)


def tokens(text: str) -> list[str]:
    """Split normalized text into tokens (Hebrew letters, alnum, %)."""
    return _TOKEN_RE.findall(normalize(text))
