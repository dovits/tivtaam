"""Basket typical/forgotten — pure logic, no browser."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tivtaam.basket.forgotten import find_forgotten
from tivtaam.basket.typical import compute_typical
from tivtaam.db.migrations import ensure_db
from tivtaam.db.models import Product, Purchase
from tivtaam.db.repo import insert_purchase, upsert_product
from tivtaam.resolver.pipeline import Resolution


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def conn(tmp_path: Path, monkeypatch) -> sqlite3.Connection:
    db_file = tmp_path / "test.db"
    monkeypatch.setattr("tivtaam.db.migrations.db_path", lambda: db_file)
    return ensure_db()


def _seed_recurring(conn: sqlite3.Connection, sku: str, name: str, n_weeks: int) -> None:
    upsert_product(conn, Product(
        sku=sku, name=name, name_norm=name.lower(),
        last_seen_at=_iso(datetime.now(UTC)),
    ))
    base = datetime.now(UTC)
    for w in range(n_weeks):
        ts = base - timedelta(weeks=w)
        insert_purchase(conn, Purchase(
            order_id=f"o-{sku}-{w}",
            sku=sku,
            qty=1.0,
            purchased_at=_iso(ts),
        ))


def test_typical_includes_frequent_and_excludes_rare(conn):
    _seed_recurring(conn, "milk-1", "חלב 3%", n_weeks=10)   # 10 weeks
    _seed_recurring(conn, "rare-1", "מוצר נדיר", n_weeks=3)  # only 3
    items = compute_typical(conn)
    skus = {i.sku for i in items}
    assert "milk-1" in skus
    assert "rare-1" not in skus  # below the min_weeks=6 default


def test_forgotten_excludes_items_in_current_plan(conn):
    _seed_recurring(conn, "milk-1", "חלב 3%", n_weeks=8)
    _seed_recurring(conn, "bread-1", "לחם פרוס", n_weeks=8)
    plan = [Resolution(
        generic="חלב",
        sku="milk-1",
        name="חלב 3%",
        confidence=1.0,
        source="alias",
        reason="user-selected",
    )]
    missing = find_forgotten(conn, plan)
    skus = {m.sku for m in missing}
    assert "bread-1" in skus
    assert "milk-1" not in skus
