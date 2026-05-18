"""Category generics — broad words like 'חטיפים' that should surface ALL
matching past purchases for the user to multi-pick, rather than resolving to
one SKU.

There's no category data in the DB, so membership is keyword-matched on the
product name (curated list + exclusions live in config.yaml).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from tivtaam.config import CategoryCfg, load_config
from tivtaam.db.repo import list_purchase_history_for_norm
from tivtaam.util.hebrew import normalize

MAX_CANDIDATES = 20


@dataclass
class CategoryCandidate:
    sku: str
    name: str
    n_purchases: int
    last_purchased_at: str | None


def match_category(generic: str) -> str | None:
    """Return the configured category name this generic maps to, or None."""
    g = normalize(generic)
    for name in load_config().categories:
        if normalize(name) == g:
            return name
    return None


def _in_category(name: str, cat: CategoryCfg) -> bool:
    if any(x and x in name for x in cat.exclude):
        return False
    return any(k and k in name for k in cat.keywords)


def category_candidates(conn: sqlite3.Connection, cat_name: str) -> list[CategoryCandidate]:
    """All previously-purchased products that fall in `cat_name`, ranked by
    how often / how recently they were bought."""
    cat = load_config().categories.get(cat_name)
    if cat is None:
        return []
    out: list[CategoryCandidate] = []
    for r in list_purchase_history_for_norm(conn):
        name = r["name"] or ""
        if int(r["n_purchases"] or 0) < 1 or not _in_category(name, cat):
            continue
        out.append(CategoryCandidate(
            sku=r["sku"],
            name=name,
            n_purchases=int(r["n_purchases"] or 0),
            last_purchased_at=r["last_purchased_at"],
        ))
    out.sort(key=lambda c: (c.n_purchases, c.last_purchased_at or ""), reverse=True)
    return out[:MAX_CANDIDATES]
