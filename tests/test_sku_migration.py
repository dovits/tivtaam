"""Synthetic→real SKU migration: search-found real SKUs replace `name:` placeholders."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tivtaam.db.migrations import ensure_db
from tivtaam.db.models import GenericAlias, Product, Purchase
from tivtaam.db.repo import (
    find_synthetic_with_norm,
    get_alias,
    get_product,
    insert_purchase,
    migrate_synthetic_to_real,
    upsert_alias,
    upsert_product,
)


@pytest.fixture
def conn(tmp_path: Path, monkeypatch) -> sqlite3.Connection:
    db_file = tmp_path / "test.db"
    monkeypatch.setattr("tivtaam.db.migrations.db_path", lambda: db_file)
    return ensure_db()


def _seed_synthetic(conn: sqlite3.Connection, name: str, n_purchases: int) -> str:
    sku = "name:" + name.lower()
    upsert_product(conn, Product(
        sku=sku, name=name, name_norm=name.lower(),
        last_seen_at="2026-04-01T00:00:00Z",
    ))
    for i in range(n_purchases):
        insert_purchase(conn, Purchase(
            order_id=f"order-{i}",
            sku=sku,
            qty=1.0,
            purchased_at=f"2026-04-{(i % 28) + 1:02d}T00:00:00Z",
        ))
    return sku


def test_migrate_replaces_synthetic_and_moves_purchases(conn):
    syn = _seed_synthetic(conn, "חלב 3% קרטון", 5)
    # Insert the real product
    upsert_product(conn, Product(
        sku="11017", name="חלב 3% קרטון", name_norm="חלב 3% קרטון",
        last_seen_at="2026-05-01T00:00:00Z",
    ))
    migrated = migrate_synthetic_to_real(conn, syn, "11017")
    assert migrated == 5
    # Synthetic product gone, all purchases now under the real SKU
    assert get_product(conn, syn) is None
    n_after = conn.execute(
        "SELECT COUNT(*) AS n FROM purchases WHERE sku = ?", ("11017",),
    ).fetchone()["n"]
    assert n_after == 5


def test_migrate_handles_collision_in_same_order(conn):
    syn = _seed_synthetic(conn, "ביצים", 1)  # one purchase under order-0
    upsert_product(conn, Product(
        sku="999", name="ביצים", name_norm="ביצים",
        last_seen_at="2026-05-01T00:00:00Z",
    ))
    # Also insert a real-SKU purchase in the SAME order-0 so the migration must
    # collide with the UNIQUE(order_id, sku) constraint.
    insert_purchase(conn, Purchase(order_id="order-0", sku="999", qty=1.0,
                                   purchased_at="2026-05-01T00:00:00Z"))
    migrate_synthetic_to_real(conn, syn, "999")
    # No leftover synthetic rows anywhere
    assert get_product(conn, syn) is None
    n = conn.execute("SELECT COUNT(*) AS n FROM purchases WHERE sku=?", (syn,)).fetchone()["n"]
    assert n == 0


def test_migrate_repoints_aliases(conn):
    syn = _seed_synthetic(conn, "מלפפון", 2)
    upsert_alias(conn, GenericAlias(
        generic_name="מלפפון",
        generic_name_norm="מלפפון",
        preferred_sku=syn,
        confidence=1.0,
        source="history",
        last_confirmed_at="2026-05-01T00:00:00Z",
    ))
    upsert_product(conn, Product(
        sku="555", name="מלפפון", name_norm="מלפפון",
        last_seen_at="2026-05-01T00:00:00Z",
    ))
    migrate_synthetic_to_real(conn, syn, "555")
    a = get_alias(conn, "מלפפון")
    assert a is not None
    assert a.preferred_sku == "555"


def test_find_synthetic_with_norm(conn):
    syn = _seed_synthetic(conn, "גזר ארוז", 1)
    assert find_synthetic_with_norm(conn, "גזר ארוז") == syn
    assert find_synthetic_with_norm(conn, "does-not-exist") is None
