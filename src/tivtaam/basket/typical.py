"""Compute the user's typical weekly basket.

Definition (from the plan doc): a product belongs in the typical basket if it
appears in `basket.min_weeks` or more of the last `basket.lookback_weeks` weeks
of purchases. The weekly bucket is the ISO calendar week of `purchased_at`.

This is pure SQL+Python — no browser, no Claude.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from tivtaam.config import load_config


@dataclass
class TypicalItem:
    sku: str
    name: str
    weeks_seen: int
    total_qty: float
    last_purchased_at: str | None


def _parse(iso: str) -> datetime | None:
    try:
        return datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    except Exception:
        return None


def compute_typical(conn: sqlite3.Connection) -> list[TypicalItem]:
    cfg = load_config()
    lookback = cfg.basket.lookback_weeks
    min_weeks = cfg.basket.min_weeks
    cutoff = datetime.now(UTC) - timedelta(weeks=lookback)

    rows = conn.execute(
        """
        SELECT p.sku, p.name, pu.purchased_at, pu.qty
        FROM purchases pu
        JOIN products p ON p.sku = pu.sku
        WHERE pu.purchased_at >= ?
        """,
        (cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    ).fetchall()

    by_sku: dict[str, dict] = {}
    for r in rows:
        dt = _parse(r["purchased_at"])
        if dt is None:
            continue
        year, week, _ = dt.isocalendar()
        bucket = f"{year}-W{week:02d}"
        rec = by_sku.setdefault(r["sku"], {
            "name": r["name"],
            "weeks": set(),
            "total_qty": 0.0,
            "last_dt": dt,
        })
        rec["weeks"].add(bucket)
        rec["total_qty"] += float(r["qty"] or 0)
        if dt > rec["last_dt"]:
            rec["last_dt"] = dt

    out: list[TypicalItem] = []
    for sku, rec in by_sku.items():
        if len(rec["weeks"]) >= min_weeks:
            out.append(TypicalItem(
                sku=sku,
                name=rec["name"],
                weeks_seen=len(rec["weeks"]),
                total_qty=rec["total_qty"],
                last_purchased_at=rec["last_dt"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            ))

    out.sort(key=lambda x: (-x.weeks_seen, -x.total_qty))
    return out
