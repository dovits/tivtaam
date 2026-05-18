"""Score candidate SKUs from the user's purchase history.

  score = 0.4 * fuzzy_similarity_norm
        + 0.4 * frequency_norm
        + 0.2 * recency_norm

where:
  fuzzy_similarity_norm: rapidfuzz score / 100
  frequency_norm:        n_purchases / max_n_purchases_in_set
  recency_norm:          exp(-days_since_last / 60)
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from tivtaam.config import load_config
from tivtaam.db.repo import list_purchase_history_for_norm
from tivtaam.resolver.fuzzy import score as fuzzy_score


@dataclass
class HistoryCandidate:
    sku: str
    name: str
    brand: str | None
    fuzzy: int
    n_purchases: int
    last_purchased_at: str | None
    composite: float
    is_available: int


def _days_since(iso: str | None) -> float:
    if not iso:
        return 365.0
    try:
        dt = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        return max(0.0, (datetime.now(UTC) - dt).total_seconds() / 86400)
    except Exception:
        return 365.0


def score_history(conn: sqlite3.Connection, generic: str) -> list[HistoryCandidate]:
    """Return all history-backed candidates above the fuzzy floor, sorted by composite desc."""
    cfg = load_config()
    rows = list_purchase_history_for_norm(conn)
    if not rows:
        return []

    raw: list[HistoryCandidate] = []
    for r in rows:
        if not r["name"]:
            continue
        s = fuzzy_score(generic, r["name"])
        if s < cfg.history.fuzzy_floor:
            continue
        raw.append(HistoryCandidate(
            sku=r["sku"],
            name=r["name"],
            brand=r["brand"],
            fuzzy=s,
            n_purchases=int(r["n_purchases"] or 0),
            last_purchased_at=r["last_purchased_at"],
            composite=0.0,
            is_available=int(r["is_available"] or 0),
        ))

    if not raw:
        return []

    max_n = max(c.n_purchases for c in raw) or 1
    for c in raw:
        fuzzy_norm = c.fuzzy / 100.0
        freq_norm = c.n_purchases / max_n
        recency_norm = math.exp(-_days_since(c.last_purchased_at) / 60.0)
        c.composite = 0.4 * fuzzy_norm + 0.4 * freq_norm + 0.2 * recency_norm

    raw.sort(key=lambda x: x.composite, reverse=True)
    return raw
