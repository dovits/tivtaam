"""One-off probe: navigate to a product URL and report what the page looks like.

Why: as of 2026-05-29 the cart writer reports `product dialog did not open`
for every SKU at `/?catalogProduct=<sku>`. Confirm whether the URL still
triggers a dialog, and surface the current container/selector so we can
update browser/cart.py + data/selectors.yaml.

Usage:
    python scripts/probe_product_dialog.py [sku]

Default sku is 1060138 (תפוח עץ סמיט) — a produce item known to have worked
on 2026-05-18.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tivtaam.browser.auth import ensure_logged_in
from tivtaam.browser.context import open_context, open_page
from tivtaam.config import load_config


CANDIDATE_SELECTORS = [
    ".dialog.product",
    ".dialog.product .product-data",
    "[role=dialog]",
    ".modal",
    ".modal.product",
    ".product-dialog",
    ".product-modal",
    ".overlay .product",
    "div.product-page",
    "div.product-details",
    ".dialog",
    ".sp-dialog",
]


def main() -> int:
    sku = sys.argv[1] if len(sys.argv) > 1 else "1060138"
    cfg = load_config()
    url = f"{cfg.browser.base_url}/?catalogProduct={sku}"
    print(f"[probe] navigating to {url}")

    with open_context(headless=False) as ctx:
        page = open_page(ctx)
        ensure_logged_in(page)

        page.goto(url, wait_until="domcontentloaded")
        # Give the SPA a generous beat to render whatever it renders.
        page.wait_for_timeout(8_000)

        print(f"[probe] final URL: {page.url}")
        print(f"[probe] page title: {page.title()!r}")

        print("\n[probe] candidate-selector presence (count / visible-count):")
        for css in CANDIDATE_SELECTORS:
            try:
                loc = page.locator(css)
                count = loc.count()
                visible = sum(1 for i in range(count) if loc.nth(i).is_visible())
                print(f"  {css:40s} count={count} visible={visible}")
            except Exception as e:
                print(f"  {css:40s} ERR {e}")

        print("\n[probe] dialog-ish containers actually visible on page:")
        # Grab anything that looks like a popup/dialog/modal/overlay.
        sniff = (
            "[class*='dialog'], [class*='modal'], [class*='popup'], "
            "[class*='overlay'], [role='dialog']"
        )
        loc = page.locator(sniff)
        for i in range(min(loc.count(), 20)):
            el = loc.nth(i)
            try:
                if not el.is_visible():
                    continue
                tag = el.evaluate("e => e.tagName.toLowerCase()")
                cls = el.get_attribute("class") or ""
                outer = el.evaluate("e => e.outerHTML.slice(0, 220)")
                print(f"  <{tag} class={cls!r}>")
                print(f"    {outer}")
            except Exception as e:
                print(f"  [item {i}] ERR {e}")

        # Save full HTML for offline grep.
        out = Path(__file__).resolve().parents[1] / "logs" / f"probe_{sku}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page.content(), encoding="utf-8")
        print(f"\n[probe] full HTML dumped to {out}")

        # Pause so a human can eyeball the (non-headless) browser.
        page.wait_for_timeout(3_000)

    return 0


if __name__ == "__main__":
    sys.exit(main())
