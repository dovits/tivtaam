"""Manual exploration session — opens a headed browser at tivtaam.co.il for selector recording.

Workflow:
  1) Run this script. A Chromium window opens at the home page. The persistent context
     in `.pw-userdata/` will retain cookies for future runs.
  2) Log in manually if not already logged in (this also handles captchas/OTPs).
  3) Walk through the key pages: home → orders-history → an order's details → a product
     page → cart → coupons. For each, observe what selectors exist (data-testid, ARIA
     roles, accessible text).
  4) Edit `data/selectors.yaml` to fill / refine the selectors. The keys are pre-listed
     there; just supply correct strategies in priority order.
  5) Verify with `python -m tivtaam doctor` and then `python -m tivtaam history-sync`.

This script is intentionally read-only — it doesn't modify selectors.yaml automatically;
the user inspects and edits manually so we never overwrite working values.
"""

from __future__ import annotations

import os
import sys
import time

# Ensure 'src' is on the path when running as a plain script.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from playwright.sync_api import sync_playwright  # noqa: E402

from tivtaam.config import load_config, repo_root  # noqa: E402


def main() -> None:
    cfg = load_config()
    user_data_dir = (repo_root() / cfg.browser.user_data_dir).resolve()
    user_data_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            channel="chrome",
            headless=False,
            locale="he-IL",
            timezone_id="Asia/Jerusalem",
            viewport={"width": 1366, "height": 900},
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(cfg.browser.base_url)
        print()
        print("=" * 60)
        print(" Manual exploration session")
        print("=" * 60)
        print(" • The window stays open. Walk through these pages and")
        print(" inspect element selectors (data-testid > role > css > text):")
        print(" home → התחבר → orders-history → an order → product → cart → coupons")
        print(" • When done, edit data/selectors.yaml to match what you saw.")
        print(" • Press Ctrl+C in this terminal to close the browser.")
        print()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nClosing browser. Cookies are saved to .pw-userdata/.")
        finally:
            ctx.close()


if __name__ == "__main__":
    main()
