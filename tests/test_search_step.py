"""Scoring sanity for resolver.search_step — runs entirely offline.

Uses an in-memory SQLite seeded with a few products, and constructs SearchResult
objects directly without hitting the browser.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tivtaam.browser.search import SearchResult
from tivtaam.db.migrations import ensure_db
from tivtaam.db.models import Product, Purchase
from tivtaam.db.repo import insert_purchase, upsert_product
from tivtaam.resolver.search_step import score_search


@pytest.fixture
def conn(tmp_path: Path, monkeypatch) -> sqlite3.Connection:
    db_file = tmp_path / "test.db"
    monkeypatch.setattr("tivtaam.db.migrations.db_path", lambda: db_file)
    c = ensure_db()
    # seed history: user buys "גבינה בולגרית קוביות 5%" 8 times
    upsert_product(c, Product(
        sku="name:גבינה בולגרית קוביות 5%",
        name="גבינה בולגרית קוביות 5%",
        name_norm="גבינה בולגרית קוביות 5%",
        last_seen_at="2026-04-01T00:00:00Z",
    ))
    for i in range(8):
        insert_purchase(c, Purchase(
            order_id=f"order-{i}",
            sku="name:גבינה בולגרית קוביות 5%",
            qty=1.0,
            purchased_at="2026-04-01T00:00:00Z",
        ))
    return c


def _mk_result(sku: str, name: str) -> SearchResult:
    return SearchResult(sku=sku, name=name, brand=None, size_text=None,
                       price=None, image_url=None, raw_url=f"?catalogProduct={sku}")


def test_exact_match_beats_variant(conn):
    results = [
        _mk_result("11804", "גבינה בולגרית קוביות 5%"),
        _mk_result("6486", "גבינה בולגרית מעודנת קוביות 5%"),
    ]
    cands = score_search(conn, "גבינה בולגרית 5% קוביות", results)
    assert cands[0].sku == "11804"
    # Composite is uncapped — perfect match + history bonus should exceed 100.
    assert cands[0].composite > cands[1].composite


def test_history_bonus_does_not_apply_to_unrelated_fat_percentage(conn):
    """User only buys 5%; a 24% variant should not get the history bonus."""
    results = [
        _mk_result("7002", "גבינה בולגרית 5%"),
        _mk_result("64737", "גבינה בולגרית 24%"),
    ]
    cands = score_search(conn, "גבינה בולגרית", results)
    by_sku = {c.sku: c for c in cands}
    # 5% should have a history bonus (similar to purchased 5% cube cheese)
    assert by_sku["7002"].history_bonus > 0
    # 24% should NOT — character-level ratio against "...קוביות 5%" is below threshold
    assert by_sku["64737"].history_bonus == 0


def test_extra_token_penalty_orders_results(conn):
    """A candidate with extra qualifiers loses points vs an exact-token-count match."""
    results = [
        _mk_result("A", "גבינה בולגרית קוביות 5%"),                  # 4 tokens
        _mk_result("B", "גבינה בולגרית מעודנת אורגנית קוביות 5%"),    # 6 tokens
    ]
    cands = score_search(conn, "גבינה בולגרית 5% קוביות", results)
    assert cands[0].sku == "A"
