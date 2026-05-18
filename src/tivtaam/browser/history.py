"""Scrape /orders-history into the products + purchases tables.

Past orders on this site don't expose real SKUs — line items are plain
`<td class="product-details">name</td>` cells on a per-order detail page at
`/orders-history/{order_id}`. We synthesize a SKU as `name:<normalized name>`
so the same product across orders maps to the same row in `products`.
A later resolver pass can re-key these to real catalog SKUs via search.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from playwright.sync_api import Locator, Page

from tivtaam.browser import selectors as sel
from tivtaam.config import load_config
from tivtaam.db.models import Product, Purchase
from tivtaam.db.repo import (
    get_meta,
    insert_purchase,
    touch_history_sync,
    upsert_product,
)
from tivtaam.util.hebrew import normalize
from tivtaam.util.logging import get_logger
from tivtaam.util.timing import jitter_sleep, now_iso

log = get_logger(__name__)


@dataclass
class ScrapedLine:
    sku: str
    name: str
    qty: float
    unit_price: float | None
    raw_url: str | None


@dataclass
class ScrapedOrder:
    order_id: str
    purchased_at: str  # ISO date
    lines: list[ScrapedLine]


def _synthetic_sku(name: str) -> str:
    return "name:" + normalize(name)


def _parse_order_datetime(text: str) -> str:
    """Row date cell looks like '09/05/2026 19:12'. Falls back to now() on miss."""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", text or "")
    if not m:
        return now_iso()
    d, mo, y, h, mi = m.groups()
    hh = int(h) if h else 0
    mm = int(mi) if mi else 0
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}T{hh:02d}:{mm:02d}:00Z"


def _parse_price_text(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", text)
    return float(m.group(1).replace(",", ".")) if m else None


def should_skip_sync(conn: sqlite3.Connection) -> bool:
    cfg = load_config()
    last = get_meta(conn, "last_history_sync_at")
    if not last:
        return False
    last_dt = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (datetime.now(UTC) - last_dt) < timedelta(hours=cfg.history.sync_min_age_hours)


def navigate_to_orders(page: Page) -> None:
    cfg = load_config()
    page.goto(f"{cfg.browser.base_url}/orders-history", wait_until="domcontentloaded")
    jitter_sleep()
    # AngularJS populates the table after DOM-ready; wait for at least one row.
    try:
        page.locator("tr.clickable").first.wait_for(state="visible", timeout=10_000)
    except Exception:
        log.warning("Timed out waiting for order rows to render on /orders-history")


@dataclass
class _OrderMeta:
    order_id: str
    purchased_at: str
    total: float | None


def _collect_row_metadata(page: Page) -> list[_OrderMeta]:
    rows = sel.resolve(page, "order_row")
    if rows is None:
        log.warning("No order rows found via selectors. Update data/selectors.yaml.")
        return []
    n = rows.count()
    log.info("Found %d order rows on current page", n)
    metas: list[_OrderMeta] = []
    for i in range(n):
        row = rows.nth(i)
        try:
            tds = row.locator("td")
            order_id = (tds.nth(0).text_content() or "").strip()
            date_text = (tds.nth(1).text_content() or "").strip()
            total_text = ""
            total_loc = row.locator("td.total-sum")
            if total_loc.count():
                total_text = (total_loc.first.text_content() or "").strip()
            if not order_id:
                log.warning("Row %d has no order id, skipping", i)
                continue
            metas.append(_OrderMeta(
                order_id=order_id,
                purchased_at=_parse_order_datetime(date_text),
                total=_parse_price_text(total_text),
            ))
        except Exception as e:
            log.warning("Skipping row %d metadata: %s", i, e)
    return metas


def _wait_for_stable_count(
    locator: Locator,
    settle_ms: int = 1500,
    total_timeout_ms: int = 20_000,
    poll_ms: int = 250,
) -> int:
    """Poll until locator.count() stops changing for settle_ms. Returns final count."""
    deadline = time.time() + total_timeout_ms / 1000
    last_count = -1
    stable_since: float | None = None
    while time.time() < deadline:
        c = locator.count()
        if c > 0 and c == last_count:
            if stable_since is None:
                stable_since = time.time()
            elif (time.time() - stable_since) * 1000 >= settle_ms:
                return c
        else:
            last_count = c
            stable_since = None
        time.sleep(poll_ms / 1000)
    return max(last_count, 0)


def _scrape_order_detail(page: Page, meta: _OrderMeta) -> ScrapedOrder:
    cfg = load_config()
    detail_url = f"{cfg.browser.base_url}/orders-history/{meta.order_id}"
    page.goto(detail_url, wait_until="domcontentloaded")
    name_cells = page.locator("td.product-details")
    try:
        name_cells.first.wait_for(state="visible", timeout=15_000)
    except Exception:
        log.warning("Timed out waiting for product rows on order %s", meta.order_id)
        return ScrapedOrder(
            order_id=meta.order_id,
            purchased_at=meta.purchased_at,
            lines=[],
        )
    final_count = _wait_for_stable_count(name_cells)
    log.info("Order %s: %d product rows", meta.order_id, final_count)
    seen: set[str] = set()
    lines: list[ScrapedLine] = []
    for j in range(name_cells.count()):
        name = (name_cells.nth(j).text_content() or "").strip()
        if not name:
            continue
        sku = _synthetic_sku(name)
        if sku in seen:
            continue  # purchases table is unique on (order_id, sku)
        seen.add(sku)
        lines.append(ScrapedLine(
            sku=sku,
            name=name,
            qty=1.0,
            unit_price=None,
            raw_url=detail_url,
        ))
    return ScrapedOrder(
        order_id=meta.order_id,
        purchased_at=meta.purchased_at,
        lines=lines,
    )


def scrape_orders(page: Page) -> Iterable[ScrapedOrder]:
    """Yield orders by reading the list, then visiting each detail page.

    Pagination handling is delegated to the caller; v1 walks visible rows once.
    """
    metas = _collect_row_metadata(page)
    for meta in metas:
        try:
            yield _scrape_order_detail(page, meta)
        except Exception as e:
            log.warning("Skipping order %s: %s", meta.order_id, e)


def persist_order(conn: sqlite3.Connection, order: ScrapedOrder) -> tuple[int, int]:
    """Upsert products + insert purchases. Returns (n_products_seen, n_purchases_inserted)."""
    n_p = 0
    n_pu = 0
    for line in order.lines:
        product = Product(
            sku=line.sku,
            name=line.name,
            name_norm=normalize(line.name),
            last_price=line.unit_price,
            last_seen_at=now_iso(),
            raw_url=line.raw_url,
            is_available=1,
        )
        upsert_product(conn, product)
        n_p += 1
        inserted = insert_purchase(conn, Purchase(
            order_id=order.order_id,
            sku=line.sku,
            qty=line.qty,
            unit_price=line.unit_price,
            purchased_at=order.purchased_at,
        ))
        if inserted:
            n_pu += 1
    return n_p, n_pu


def sync_history(conn: sqlite3.Connection, page: Page, force: bool = False) -> dict:
    if not force and should_skip_sync(conn):
        log.info("History sync skipped (synced recently). Use --resync to override.")
        return {"skipped": True, "products": 0, "purchases": 0}

    navigate_to_orders(page)
    n_products = 0
    n_purchases = 0
    n_orders = 0
    for order in scrape_orders(page):
        n_orders += 1
        np, npu = persist_order(conn, order)
        n_products += np
        n_purchases += npu

    touch_history_sync(conn)
    log.info(
        "History sync done: %d orders, %d product-rows, %d new purchases",
        n_orders, n_products, n_purchases,
    )
    return {"skipped": False, "orders": n_orders, "products": n_products, "purchases": n_purchases}
