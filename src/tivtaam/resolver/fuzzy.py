"""Fuzzy matching helpers (Hebrew-aware, via rapidfuzz)."""

from __future__ import annotations

from rapidfuzz import fuzz

from tivtaam.util.hebrew import normalize


def score(generic: str, candidate: str) -> int:
    """Return a 0-100 fuzzy similarity score between a generic name and a product name.

    We use token_set_ratio so word ordering and extra qualifiers (size, %, brand)
    don't dominate the score.
    """
    return int(fuzz.token_set_ratio(normalize(generic), normalize(candidate)))


def best_match(generic: str, candidates: list[str]) -> tuple[int, int]:
    """Return (best_idx, best_score). Empty list → (-1, 0)."""
    if not candidates:
        return -1, 0
    best_i, best_s = 0, score(generic, candidates[0])
    for i, c in enumerate(candidates[1:], start=1):
        s = score(generic, c)
        if s > best_s:
            best_i, best_s = i, s
    return best_i, best_s
