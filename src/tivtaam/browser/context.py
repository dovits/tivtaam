"""Playwright persistent-context manager (sync API)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from playwright.sync_api import BrowserContext, Page, sync_playwright

from tivtaam.config import load_config, repo_root


@contextmanager
def open_context(headless: bool | None = None) -> Iterator[BrowserContext]:
    cfg = load_config()
    if headless is None:
        headless = cfg.secrets.run_headless
    user_data_dir = (repo_root() / cfg.browser.user_data_dir).resolve()
    user_data_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            channel="chrome",
            headless=headless,
            locale="he-IL",
            timezone_id="Asia/Jerusalem",
            viewport={"width": 1366, "height": 900},
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        ctx.set_default_timeout(cfg.browser.default_timeout_ms)
        ctx.set_default_navigation_timeout(cfg.browser.navigation_timeout_ms)
        try:
            yield ctx
        finally:
            ctx.close()


def open_page(ctx: BrowserContext) -> Page:
    if ctx.pages:
        return ctx.pages[0]
    return ctx.new_page()
