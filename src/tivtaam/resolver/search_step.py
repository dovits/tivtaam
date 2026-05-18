"""Score live-site search results to produce a Step C candidate set.

Composite is on a 0-100 scale:
  composite = length_aware_fuzzy + history_bonus

`length_aware_fuzzy` starts from `token_set_ratio` (handles word-order and the
query being a subset of the candidate) but subtracts `EXTRA_TOKEN_PENALTY` per
extra word the candidate has beyond the query. Without this penalty all
candidates that contain every query token tie at 100 — including bloated
variants like "X מעודנת Y" vs the user's actual target "X Y".

`history_bonus` is +HISTORY_BONUS when the result name closely matches any
product the user has already bought, so familiar items break ties.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from rapidfuzz import fuzz

from tivtaam.browser.search import SearchResult
from tivtaam.db.repo import list_purchase_history_for_norm
from tivtaam.resolver.fuzzy import score as fuzzy_score
from tivtaam.util.hebrew import normalize

HISTORY_OVERLAP_THRESHOLD = 80  # char-level ratio — 24% vs 5% should NOT trip this
HISTORY_BONUS = 10
EXTRA_TOKEN_PENALTY = 12   # per extra word in candidate beyond query
LENGTH_PENALTY_FLOOR = 80  # only apply when raw fuzzy is already strong


@dataclass
class SearchCandidate:
    sku: str
    name: str
    brand: str | None
    price: float | None
    fuzzy: int
    history_bonus: int
    composite: int

    @property
    def composite_float(self) -> float:
        return min(1.0, self.composite / 100.0)


def _length_aware_fuzzy(generic: str, candidate: str) -> int:
    base = fuzzy_score(generic, candidate)
    if base < LENGTH_PENALTY_FLOOR:
        return base
    g_tokens = normalize(generic).split()
    c_tokens = normalize(candidate).split()
    extra = max(0, len(c_tokens) - len(g_tokens))
    return max(0, base - extra * EXTRA_TOKEN_PENALTY)


def _history_name_norms(conn: sqlite3.Connection) -> list[str]:
    rows = list_purchase_history_for_norm(conn)
    return [normalize(r["name"]) for r in rows if r["name"]]


def score_search(
    conn: sqlite3.Connection,
    generic: str,
    results: list[SearchResult],
) -> list[SearchCandidate]:
    history_norms = _history_name_norms(conn)

    cands: list[SearchCandidate] = []
    for r in results:
        f = _length_aware_fuzzy(generic, r.name)
        bonus = 0
        if history_norms:
            r_norm = normalize(r.name)
            best_overlap = max(
                (int(fuzz.ratio(r_norm, h)) for h in history_norms),
                default=0,
            )
            if best_overlap >= HISTORY_OVERLAP_THRESHOLD:
                bonus = HISTORY_BONUS
        composite = f + bonus  # uncapped — exceeding 100 lets perfect matches win on margin
        cands.append(SearchCandidate(
            sku=r.sku,
            name=r.name,
            brand=r.brand,
            price=r.price,
            fuzzy=f,
            history_bonus=bonus,
            composite=composite,
        ))

    cands.sort(key=lambda c: c.composite, reverse=True)
    return cands
