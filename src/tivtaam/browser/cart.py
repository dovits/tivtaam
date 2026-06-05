"""Cart writer.

In **dry-run** mode (default), `add_to_cart` only logs what *would* happen — no
navigation, no clicks. This lets Phase 2 exercise the full plan→review→write
loop without touching the live cart.

Live mode is intentionally minimal in Phase 2 — Phase 3 layers on quantity
verification, OOS handling, cart-total drift checks. Either way, **the runner
never clicks `תשלום` / `שלם` / `שלח הזמנה`** — that's a hard invariant enforced
in `browser.checkout_park`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from playwright.sync_api import Page

from tivtaam.browser import selectors as sel
from tivtaam.browser.checkout_park import assert_no_checkout_action
from tivtaam.config import load_config
from tivtaam.util.logging import get_logger
from tivtaam.util.timing import jitter_sleep

log = get_logger(__name__)


@dataclass
class CartLine:
    sku: str
    name: str
    qty: float = 1.0


@dataclass
class CartWriteResult:
    added: list[str]      # SKUs successfully added (or "would add" in dry-run)
    failed: list[str]     # SKUs we couldn't add
    dry_run: bool


def _product_url(sku: str) -> str:
    cfg = load_config()
    return f"{cfg.browser.base_url}/?catalogProduct={sku}"


def _resolve_real_sku(page: Page, line: CartLine) -> str | None:
    """History-resolved items carry a synthetic ``name:<norm>`` SKU that is not
    a real catalog id. Search the live site by name and take the top hit."""
    if not line.sku.startswith("name:"):
        return line.sku
    from tivtaam.browser.search import search_site

    query = line.name or line.sku[len("name:") :]
    try:
        results = search_site(page, query, limit=1)
    except Exception as e:
        log.warning("search fallback failed for %r: %s", query, e)
        return None
    if not results:
        log.warning("no search results for synthetic sku, query=%r", query)
        return None
    log.info("synthetic %s -> real sku=%s (%s)", line.sku, results[0].sku, results[0].name)
    return results[0].sku


def _cart_total_text(page: Page) -> str:
    """Best-effort read of the cart summary price; '' if not present."""
    loc = sel.resolve(page, "cart_total")
    if loc is None or loc.count() == 0:
        return ""
    try:
        return (loc.first.inner_text() or "").strip()
    except Exception:
        return ""


# Fruit/veg need an origin choice before they can be added, a kg/unit toggle,
# and a per-kg quantity. Per the user: origin = "prefer Israel", unit = kg,
# quantity = 1 kg, always.
PREFER_ISRAEL = "מעדיף ישראל"
PRODUCT_DATA = ".dialog.product .product-data"


def _is_produce(page: Page) -> bool:
    """Produce shows a kg/unit toggle in the product dialog; packaged goods don't."""
    try:
        return page.locator(f"{PRODUCT_DATA} .weight-option, {PRODUCT_DATA} .unit-option").count() > 0
    except Exception:
        return False


def _ensure_kg(page: Page, scope: str) -> None:
    """Click the kg toggle within `scope` unless it's already active."""
    kg = page.locator(f"{scope} .weight-option")
    if kg.count() == 0:
        return
    cls = kg.first.get_attribute("class") or ""
    if "active" not in cls:
        try:
            kg.first.click(timeout=4_000)
            page.wait_for_timeout(600)
        except Exception as e:
            log.warning("could not switch to kg in %s: %s", scope, e)


def _select_prefer_israel(page: Page) -> bool:
    """Open the origin dropdown(s) in the product dialog and pick 'prefer Israel'.

    There are two .sp-dropdown-text controls (origin + save-to-list); only the
    origin one exposes a 'מעדיף ישראל' option, so we probe each."""
    dds = page.locator(f"{PRODUCT_DATA} .sp-dropdown-text")
    for i in range(dds.count()):
        dd = dds.nth(i)
        try:
            dd.click(timeout=4_000)
            page.wait_for_timeout(900)
        except Exception:
            continue
        opt = page.locator(f"{PRODUCT_DATA} .sp-option").filter(has_text=PREFER_ISRAEL)
        if opt.count() > 0:
            # The Angular ng-click lives on the inner span.option-link, not the
            # <li>.sp-option — clicking the <li> is a no-op and leaves the add
            # button gated. Prefer the link; fall back to the option itself.
            link = opt.first.locator(".option-link")
            target = link.first if link.count() > 0 else opt.first
            try:
                # ng-click just needs the event; the span can fail Playwright
                # actionability (zero-area / li overlay), so force it.
                target.click(timeout=4_000, force=True)
                page.wait_for_timeout(1_200)
                return True
            except Exception:
                try:
                    target.dispatch_event("click")
                    page.wait_for_timeout(1_200)
                    return True
                except Exception as e:
                    log.warning("found origin option but click failed: %s", e)
                    return False
        # Not the origin dropdown — close it before trying the next.
        try:
            dd.click(timeout=2_000)
        except Exception:
            pass
    log.warning("origin 'prefer Israel' option not found")
    return False


def _set_dialog_qty_1kg(page: Page, name: str) -> None:
    """Once produce is in the cart, the dialog shows a .sp-quantity stepper with
    a kg input. Force kg + set the weight to exactly 1."""
    qty = page.locator(f"{PRODUCT_DATA} .sp-quantity input")
    if qty.count() == 0:
        log.warning("no in-dialog quantity input for %r — left at default", name)
        return
    _ensure_kg(page, PRODUCT_DATA)

    def _val() -> float | None:
        try:
            return float((qty.first.input_value() or "").strip())
        except Exception:
            return None

    cur = _val()
    if cur is not None and abs(cur - 1.0) < 0.001:
        log.info("%r already at 1 kg", name)
        return

    # Drive the Angular +/- stepper rather than the text input: clearing the
    # input is read as 0 and removes the line. The stepper commits cleanly.
    plus = page.locator(f"{PRODUCT_DATA} .sp-quantity .plus, {PRODUCT_DATA} .sp-quantity [ng-click*='lus']")
    minus = page.locator(f"{PRODUCT_DATA} .sp-quantity .minus, {PRODUCT_DATA} .sp-quantity [ng-click*='inus']")
    if plus.count() == 0 or minus.count() == 0:
        log.warning("stepper buttons not found for %r — left at %s", name, cur)
        return

    last = None
    for _ in range(20):
        cur = _val()
        if cur is None:
            log.warning("lost quantity input for %r", name)
            return
        if abs(cur - 1.0) < 0.001:
            log.info("set %r to 1 kg", name)
            return
        if cur == last:  # stepper not moving the value — bail rather than spin
            log.warning("stepper stuck at %s for %r — left as-is", cur, name)
            return
        last = cur
        try:
            (minus if cur > 1.0 else plus).first.click(timeout=4_000)
            page.wait_for_timeout(900)
        except Exception as e:
            log.warning("stepper click failed for %r: %s", name, e)
            return
    log.warning("could not reach 1 kg for %r (at %s)", name, _val())


def _add_one_live(page: Page, line: CartLine) -> bool:
    """Open the product dialog and click *its* add-to-cart button.

    The product opens in a fullscreen dialog over the homepage; only the button
    under ``.dialog.product .product-data`` targets this product (the homepage
    and the dialog's similar-products carousel have their own). We confirm the
    add by checking the cart total changed. We never click any pay control.
    """
    sku = _resolve_real_sku(page, line)
    if sku is None:
        log.warning("could not resolve a real sku for %r (was %s)", line.name, line.sku)
        return False

    page.goto(_product_url(sku), wait_until="domcontentloaded")
    jitter_sleep()
    try:
        page.locator(".dialog.product").first.wait_for(state="visible", timeout=10_000)
    except Exception:
        log.warning("product dialog did not open for sku=%s", sku)
        return False

    produce = _is_produce(page)
    if produce:
        # Produce can't be added until origin is chosen; also force kg.
        _ensure_kg(page, PRODUCT_DATA)
        _select_prefer_israel(page)

    # For produce the main button is gated until origin is chosen and may
    # render a beat later — poll instead of a single immediate lookup.
    btn = None
    for _ in range(16):
        btn = sel.resolve(page, "product_add_to_cart_button")
        if btn is not None and btn.count() > 0:
            break
        page.wait_for_timeout(500)
    if btn is None or btn.count() == 0:
        # Produce already in the cart shows a .sp-quantity stepper instead of an
        # add button — that's "already added", not a failure.
        if produce and page.locator(f"{PRODUCT_DATA} .sp-quantity input").count() > 0:
            log.info("sku=%s already in cart — ensuring 1 kg", sku)
            _set_dialog_qty_1kg(page, line.name)
            return True
        log.warning("No add-to-cart button found for sku=%s", sku)
        return False
    try:
        btn.first.wait_for(state="visible", timeout=8_000)
    except Exception:
        log.warning("Add-to-cart button not visible for sku=%s", sku)
        return False
    assert_no_checkout_action(btn.first)

    before = _cart_total_text(page)
    btn.first.click(timeout=8_000)
    # The Angular cart recomputes asynchronously — poll for the total to move.
    after = before
    for _ in range(12):
        page.wait_for_timeout(500)
        after = _cart_total_text(page)
        if after != before:
            break
    if before == after:
        log.warning(
            "cart total unchanged (%s) after add for sku=%s — treating as failed",
            after, sku,
        )
        return False
    log.info("added sku=%s (cart %s -> %s)", sku, before or "?", after)
    if produce:
        _set_dialog_qty_1kg(page, line.name)
    return True


def execute_plan(
    page: Page | None,
    lines: list[CartLine],
    dry_run: bool = True,
    on_item: Callable[[int, int, CartLine, bool], None] | None = None,
) -> CartWriteResult:
    added: list[str] = []
    failed: list[str] = []
    total = len(lines)
    for i, line in enumerate(lines, start=1):
        if dry_run:
            log.info("[dry-run] would add: sku=%s qty=%s name=%r",
                     line.sku, line.qty, line.name)
            added.append(line.sku)
            if on_item is not None:
                on_item(i, total, line, True)
            continue
        if page is None:
            raise RuntimeError("execute_plan(dry_run=False) requires a Page")
        try:
            ok = _add_one_live(page, line)
        except Exception as e:
            log.warning("add failed for sku=%s: %s", line.sku, e)
            ok = False
        (added if ok else failed).append(line.sku)
        if on_item is not None:
            on_item(i, total, line, ok)
    return CartWriteResult(added=added, failed=failed, dry_run=dry_run)
