from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from tivtaam.db.migrations import apply_schema
from tivtaam.db.models import Product, Purchase
from tivtaam.db.repo import insert_purchase, upsert_product
from tivtaam.resolver.fuzzy import score
from tivtaam.resolver.history_score import score_history
from tivtaam.resolver.pipeline import resolve
from tivtaam.util.hebrew import normalize
from tivtaam.util.timing import now_iso


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_schema(c)
    return c


def _seed_history(conn: sqlite3.Connection):
    today = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    products = [
        ("11111", "גד גבינה בולגרית 5% קוביות 200 גרם", "גד"),
        ("22222", "תנובה גבינה בולגרית 16%", "תנובה"),
        ("33333", "גד גבינה צהובה עמק 28% פרוסות", "גד"),
        ("44444", "טרה חלב 3%", "טרה"),
    ]
    for sku, name, brand in products:
        upsert_product(conn, Product(
            sku=sku, name=name, name_norm=normalize(name),
            brand=brand, last_seen_at=now_iso(),
        ))
    # Heavy preference for the 5% Gad option (bought 6×); other items 1× or 0×.
    for i in range(6):
        insert_purchase(conn, Purchase(
            order_id=f"o-{i}", sku="11111", qty=1.0, unit_price=14.9, purchased_at=today,
        ))
    insert_purchase(conn, Purchase(
        order_id="o-99", sku="22222", qty=1.0, unit_price=16.5, purchased_at=today,
    ))
    insert_purchase(conn, Purchase(
        order_id="o-50", sku="33333", qty=1.0, unit_price=22.0, purchased_at=today,
    ))


def test_fuzzy_score_basic():
    s = score("גבינה בולגרית", "גד גבינה בולגרית 5% קוביות 200 גרם")
    assert s >= 70


def test_history_picks_frequently_bought_brand(conn):
    _seed_history(conn)
    cands = score_history(conn, "גבינה בולגרית")
    assert cands, "should find candidates"
    assert cands[0].sku == "11111", f"top should be Gad 5% (most-bought), got {cands[0].sku}"


def test_pipeline_resolves_via_history(conn):
    _seed_history(conn)
    res = resolve(conn, "גבינה בולגרית")
    assert res.sku == "11111"
    assert res.source == "history"
    assert res.needs_user_confirmation is False
    assert res.confidence >= 0.80


def test_pipeline_unknown_item_asks_user(conn):
    res = resolve(conn, "מוצר שלא קיים בהיסטוריה")
    assert res.sku is None
    assert res.needs_user_confirmation is True


def test_pipeline_alias_hit_is_used(conn):
    _seed_history(conn)
    # First resolve and persist as user_confirmed → alias should be used next time.
    from tivtaam.resolver.pipeline import confirm_user_choice

    confirm_user_choice(conn, "בולגרית", "11111")
    res = resolve(conn, "בולגרית")
    assert res.source == "alias"
    assert res.sku == "11111"
    assert res.confidence >= 0.95
