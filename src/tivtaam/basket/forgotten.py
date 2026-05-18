"""Surface items the user usually buys but forgot to put on this week's list."""

from __future__ import annotations

import sqlite3

from tivtaam.basket.typical import TypicalItem, compute_typical
from tivtaam.config import load_config
from tivtaam.resolver.pipeline import Resolution


def find_forgotten(
    conn: sqlite3.Connection,
    current_plan: list[Resolution],
) -> list[TypicalItem]:
    """Return typical-basket items not already covered by current_plan.

    Coverage is checked by SKU (real or synthetic). Items the user removed from
    this plan still count as "covered" — if they're skipping yogurt this week,
    we won't nag.
    """
    cfg = load_config()
    typical = compute_typical(conn)
    covered_skus = {r.sku for r in current_plan if r.sku}
    missing = [t for t in typical if t.sku not in covered_skus]
    return missing[: cfg.basket.max_suggestions]
