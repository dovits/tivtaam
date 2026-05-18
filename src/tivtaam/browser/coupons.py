"""Coupon discovery + safe auto-clip.

On Tiv Taam the "My Coupons" page (`/user/coupons`) shows coupons the user
already has access to. Each unclipped coupon has a button with class
`add-to-cart` whose ng-click triggers an `addLine` call with
`CART_LINE_TYPES.COUPON`. Clicking it adds the coupon to the cart.

Auto-clip policy (per plan doc): only clip zero-friction coupons — those that
don't require points and aren't already clipped. Surface anything else for the
user to decide.
"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Locator, Page

from tivtaam.browser.checkout_park import assert_no_checkout_action
from tivtaam.config import load_config
from tivtaam.util.logging import get_logger
from tivtaam.util.timing import jitter_sleep

log = get_logger(__name__)


@dataclass
class Coupon:
    title: str
    has_point_cost: bool
    is_clipped: bool


@dataclass
class ClipResult:
    clipped_titles: list[str]
    surfaced_titles: list[str]


def _read_coupon(card: Locator) -> Coupon:
    title = ""
    try:
        title = (card.text_content() or "").strip().split("\n", 1)[0]
    except Exception:
        pass
    clipped = False
    has_cost = False
    try:
        btn = card.locator('button[ng-if*="!item.isclipped"]')
        if btn.count() == 0:
            clipped = True
        else:
            label = (btn.first.text_content() or "")
            has_cost = "points" in label.lower() or "נקוד" in label
    except Exception:
        pass
    return Coupon(title=title, has_point_cost=has_cost, is_clipped=clipped)


def auto_clip(page: Page) -> ClipResult:
    cfg = load_config()
    page.goto(f"{cfg.browser.base_url}/user/coupons", wait_until="domcontentloaded")
    jitter_sleep()
    cards = page.locator(".coupon, [class*='coupon-card']")
    # Fallback: any container holding the coupon clip button.
    if cards.count() == 0:
        cards = page.locator('button[ng-click*="CART_LINE_TYPES.COUPON"]').locator("xpath=ancestor::*[3]")

    result = ClipResult(clipped_titles=[], surfaced_titles=[])
    n = cards.count()
    log.info("found %d coupon cards", n)
    for i in range(n):
        c = cards.nth(i)
        info = _read_coupon(c)
        if info.is_clipped:
            continue
        if info.has_point_cost or not cfg.coupons.auto_clip:
            result.surfaced_titles.append(info.title)
            continue
        try:
            btn = c.locator('button[ng-click*="CART_LINE_TYPES.COUPON"]').first
            if btn.count() == 0:
                continue
            assert_no_checkout_action(btn)
            btn.click()
            jitter_sleep()
            result.clipped_titles.append(info.title)
        except Exception as e:
            log.warning("failed to clip coupon %r: %s", info.title, e)
    return result
