"""Scrape the site's own "Smart List" (/smart-list — "products I usually buy").

This complements our internal history-based typical basket. Useful when the
user is missing products from past orders (e.g. before the first history-sync).
"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page

from tivtaam.browser.search import _extract_sku  # reuse the catalogProduct regex
from tivtaam.config import load_config
from tivtaam.util.logging import get_logger
from tivtaam.util.timing import jitter_sleep

log = get_logger(__name__)


@dataclass
class SmartListEntry:
    sku: str
    name: str
    price: float | None


def fetch_smart_list(page: Page, limit: int = 100) -> list[SmartListEntry]:
    cfg = load_config()
    page.goto(f"{cfg.browser.base_url}/smart-list", wait_until="domcontentloaded")
    jitter_sleep()
    cards = page.locator("sp-product")
    try:
        cards.first.wait_for(state="visible", timeout=10_000)
    except Exception:
        log.info("smart-list page rendered no products")
        return []
    n = min(cards.count(), limit)
    out: list[SmartListEntry] = []
    for i in range(n):
        c = cards.nth(i)
        try:
            url = c.locator('meta[itemprop="url"]').first.get_attribute("content") or ""
            sku = _extract_sku(url)
            if not sku:
                continue
            name_meta = c.locator('meta[itemprop="name"]').first
            name = (name_meta.get_attribute("content") or "").strip() if name_meta.count() else ""
            price: float | None = None
            pm = c.locator('meta[itemprop="price"]').first
            if pm.count():
                try:
                    price = float(pm.get_attribute("content") or "")
                except ValueError:
                    price = None
            out.append(SmartListEntry(sku=sku, name=name, price=price))
        except Exception as e:
            log.warning("smart-list row %d skipped: %s", i, e)
    return out
