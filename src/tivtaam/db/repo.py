from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from tivtaam.db.models import GenericAlias, Product, Purchase
from tivtaam.util.timing import now_iso


def upsert_product(conn: sqlite3.Connection, p: Product) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO products (sku, name, name_norm, brand, size_text, last_price,
                                  last_seen_at, image_url, raw_url, is_available)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sku) DO UPDATE SET
              name = excluded.name,
              name_norm = excluded.name_norm,
              brand = COALESCE(excluded.brand, products.brand),
              size_text = COALESCE(excluded.size_text, products.size_text),
              last_price = COALESCE(excluded.last_price, products.last_price),
              last_seen_at = excluded.last_seen_at,
              image_url = COALESCE(excluded.image_url, products.image_url),
              raw_url = COALESCE(excluded.raw_url, products.raw_url),
              is_available = excluded.is_available
            """,
            (
                p.sku, p.name, p.name_norm, p.brand, p.size_text, p.last_price,
                p.last_seen_at, p.image_url, p.raw_url, p.is_available,
            ),
        )


def upsert_products(conn: sqlite3.Connection, products: Iterable[Product]) -> int:
    n = 0
    for p in products:
        upsert_product(conn, p)
        n += 1
    return n


def insert_purchase(conn: sqlite3.Connection, pu: Purchase) -> bool:
    """Returns True if a new row was inserted, False if (order_id, sku) already exists."""
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO purchases (order_id, sku, qty, unit_price, purchased_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (pu.order_id, pu.sku, pu.qty, pu.unit_price, pu.purchased_at),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def get_alias(conn: sqlite3.Connection, generic_name_norm: str) -> GenericAlias | None:
    row = conn.execute(
        "SELECT * FROM generic_aliases WHERE generic_name_norm = ?",
        (generic_name_norm,),
    ).fetchone()
    return GenericAlias(**dict(row)) if row else None


def upsert_alias(conn: sqlite3.Connection, a: GenericAlias) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO generic_aliases (generic_name, generic_name_norm, preferred_sku,
                                         confidence, source, last_confirmed_at, times_used)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(generic_name) DO UPDATE SET
              generic_name_norm = excluded.generic_name_norm,
              preferred_sku = excluded.preferred_sku,
              confidence = excluded.confidence,
              source = excluded.source,
              last_confirmed_at = excluded.last_confirmed_at,
              times_used = generic_aliases.times_used + 1
            """,
            (a.generic_name, a.generic_name_norm, a.preferred_sku, a.confidence,
             a.source, a.last_confirmed_at, a.times_used),
        )


def get_product(conn: sqlite3.Connection, sku: str) -> Product | None:
    row = conn.execute("SELECT * FROM products WHERE sku = ?", (sku,)).fetchone()
    return Product(**dict(row)) if row else None


def list_purchase_history_for_norm(conn: sqlite3.Connection) -> list[dict]:
    """Return all (sku, name, name_norm, purchases_count, last_purchased_at, brand)."""
    rows = conn.execute(
        """
        SELECT p.sku, p.name, p.name_norm, p.brand, p.is_available,
               COUNT(pu.id) AS n_purchases,
               MAX(pu.purchased_at) AS last_purchased_at,
               SUM(pu.qty) AS total_qty
        FROM products p
        LEFT JOIN purchases pu ON pu.sku = p.sku
        GROUP BY p.sku
        """
    ).fetchall()
    return [dict(r) for r in rows]


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def touch_history_sync(conn: sqlite3.Connection) -> None:
    set_meta(conn, "last_history_sync_at", now_iso())


def cache_search(conn: sqlite3.Connection, query: str, results: list[dict], ttl: int = 3600) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO search_cache (query, results_json, fetched_at, ttl_seconds)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(query) DO UPDATE SET
              results_json = excluded.results_json,
              fetched_at = excluded.fetched_at,
              ttl_seconds = excluded.ttl_seconds
            """,
            (query, json.dumps(results, ensure_ascii=False), now_iso(), ttl),
        )


def get_cached_search(conn: sqlite3.Connection, query: str) -> list[dict] | None:
    row = conn.execute("SELECT * FROM search_cache WHERE query = ?", (query,)).fetchone()
    if not row:
        return None
    # TTL check is left to the caller (we keep it simple in v1).
    return json.loads(row["results_json"])


def create_question(
    conn: sqlite3.Connection,
    job_id: str,
    generic_name: str,
    candidates: list[dict],
) -> int:
    with conn:
        cur = conn.execute(
            """
            INSERT INTO pending_questions (job_id, generic_name, candidates_json, state, created_at)
            VALUES (?, ?, ?, 'open', ?)
            """,
            (job_id, generic_name, json.dumps(candidates, ensure_ascii=False), now_iso()),
        )
        return int(cur.lastrowid)


def get_question(conn: sqlite3.Connection, question_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM pending_questions WHERE id = ?", (question_id,)
    ).fetchone()
    return dict(row) if row else None


def set_question_selection(
    conn: sqlite3.Connection, question_id: int, chosen_sku: str | None
) -> None:
    """Persist an in-progress multi-select choice without answering the
    question (state stays 'open' so /approve still waits for Done)."""
    with conn:
        conn.execute(
            "UPDATE pending_questions SET chosen_sku = ? WHERE id = ?",
            (chosen_sku, question_id),
        )


def answer_question(
    conn: sqlite3.Connection,
    question_id: int,
    chosen_sku: str | None,
    state: str = "answered",
) -> None:
    with conn:
        conn.execute(
            """
            UPDATE pending_questions
            SET chosen_sku = ?, state = ?, answered_at = ?
            WHERE id = ?
            """,
            (chosen_sku, state, now_iso(), question_id),
        )


def migrate_synthetic_to_real(
    conn: sqlite3.Connection,
    synthetic_sku: str,
    real_sku: str,
) -> int:
    """Re-key purchases + aliases from a `name:` synthetic SKU to a real catalog SKU.

    Returns the number of purchase rows migrated. Safe under the purchases
    UNIQUE(order_id, sku) constraint: rows that would collide are dropped.
    """
    if not synthetic_sku.startswith("name:") or synthetic_sku == real_sku:
        return 0
    with conn:
        # Move purchases to the real SKU; OR IGNORE handles the unique-constraint case
        # where the real SKU was *also* purchased in the same order.
        cur = conn.execute(
            "UPDATE OR IGNORE purchases SET sku = ? WHERE sku = ?",
            (real_sku, synthetic_sku),
        )
        migrated = cur.rowcount or 0
        # Drop any leftover rows that couldn't be migrated due to a collision.
        conn.execute("DELETE FROM purchases WHERE sku = ?", (synthetic_sku,))
        # Aliases that were pointing at the synthetic now point at the real.
        conn.execute(
            "UPDATE OR IGNORE generic_aliases SET preferred_sku = ? WHERE preferred_sku = ?",
            (real_sku, synthetic_sku),
        )
        conn.execute(
            "DELETE FROM generic_aliases WHERE preferred_sku = ?",
            (synthetic_sku,),
        )
        conn.execute("DELETE FROM products WHERE sku = ?", (synthetic_sku,))
    return migrated


def find_synthetic_with_norm(
    conn: sqlite3.Connection,
    name_norm: str,
) -> str | None:
    row = conn.execute(
        "SELECT sku FROM products WHERE sku LIKE 'name:%' AND name_norm = ? LIMIT 1",
        (name_norm,),
    ).fetchone()
    return row["sku"] if row else None


def open_questions_for_job(conn: sqlite3.Connection, job_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM pending_questions WHERE job_id = ? AND state = 'open'",
        (job_id,),
    ).fetchall()
    return [dict(r) for r in rows]
