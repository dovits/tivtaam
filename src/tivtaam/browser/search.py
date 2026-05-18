r"""Site search: turn a query into a ranked list of candidate (sku, name, …).

The site exposes `/search/{url-encoded query}` (path-based, not query string).
Each result is rendered as an `<sp-product>` element with structured metadata in
`<meta itemprop="...">` tags — far more reliable than parsing the visible DOM.

We extract the catalog SKU from `meta[itemprop="url"]` (regex `catalogProduct=\d+`),
the name from `meta[itemprop="name"]`, and price from `meta[itemprop="price"]`.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import quote

from playwright.sync_api import Page

from tivtaam.config import load_config
from tivtaam.db.repo import cache_search, get_cached_search
from tivtaam.util.logging import get_logger
from tivtaam.util.timing import jitter_sleep

log = get_logger(__name__)

_CATALOG_RE = re.compile(r"catalogProduct=(\d+)")


@dataclass
class SearchResult:
    sku: str
    name: str
    brand: str | None
    size_text: str | None
    price: float | None
    image_url: str | None
    raw_url: str

    def to_dict(self) -> dict:
        return {
            "sku": self.sku,
            "name": self.name,
            "brand": self.brand,
            "size_text": self.size_text,
            "price": self.price,
            "image_url": self.image_url,
            "raw_url": self.raw_url,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SearchResult":
        return cls(**d)


def _extract_sku(url: str | None) -> str | None:
    if not url:
        return None
    m = _CATALOG_RE.search(url)
    return m.group(1) if m else None


def _parse_card(card) -> SearchResult | None:
    """Pull fields out of one <sp-product> element. Returns None on missing SKU."""
    url = ""
    name = ""
    price: float | None = None
    try:
        url_meta = card.locator('meta[itemprop="url"]').first
        if url_meta.count():
            url = url_meta.get_attribute("content") or ""
        name_meta = card.locator('meta[itemprop="name"]').first
        if name_meta.count():
            name = (name_meta.get_attribute("content") or "").strip()
        price_meta = card.locator('meta[itemprop="price"]').first
        if price_meta.count():
            raw = price_meta.get_attribute("content")
            if raw:
                try:
                    price = float(raw)
                except ValueError:
                    price = None
    except Exception:
        return None

    sku = _extract_sku(url)
    if not sku:
        return None

    if not name:
        # Fallback: visible .name title
        try:
            name = (card.locator(".name").first.get_attribute("title") or "").strip()
        except Exception:
            name = ""

    brand: str | None = None
    try:
        b = card.locator(".brand").first
        if b.count():
            brand = (b.text_content() or "").strip() or None
    except Exception:
        pass

    size_text: str | None = None
    try:
        w = card.locator(".weight").first
        if w.count():
            size_text = (w.text_content() or "").strip().lstrip("|").strip() or None
    except Exception:
        pass

    image_url: str | None = None
    try:
        img = card.locator("img.image").first
        if img.count():
            image_url = img.get_attribute("src") or img.get_attribute("ng-src")
    except Exception:
        pass

    return SearchResult(
        sku=sku,
        name=name,
        brand=brand,
        size_text=size_text,
        price=price,
        image_url=image_url,
        raw_url=url,
    )


def search_site(page: Page, query: str, limit: int = 10) -> list[SearchResult]:
    """Navigate to /search/<query> and scrape up to `limit` result cards.

    Caller is responsible for being logged in and having a page handle. This does
    NOT consult the cache — use `search_with_cache` for that.
    """
    cfg = load_config()
    target = f"{cfg.browser.base_url}/search/{quote(query, safe='')}"
    page.goto(target, wait_until="domcontentloaded")
    jitter_sleep()

    cards = page.locator("sp-product")
    try:
        cards.first.wait_for(state="visible", timeout=10_000)
    except Exception:
        log.info("No search results visible for query=%r", query)
        return []

    n = min(cards.count(), limit)
    results: list[SearchResult] = []
    for i in range(n):
        r = _parse_card(cards.nth(i))
        if r is not None:
            results.append(r)
    return results


def search_with_cache(
    conn: sqlite3.Connection,
    page: Page,
    query: str,
    limit: int = 10,
) -> list[SearchResult]:
    """Cache-aware search. Cache TTL is enforced loosely (whatever's in the row).

    On cache hit, no browser call is made. On miss, fetches and caches.
    """
    cached = get_cached_search(conn, query)
    if cached:
        return [SearchResult.from_dict(d) for d in cached]
    results = search_site(page, query, limit=limit)
    cache_search(conn, query, [r.to_dict() for r in results])
    return results
