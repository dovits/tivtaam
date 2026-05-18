"""Login flow + logged-in detection."""

from __future__ import annotations

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PWTimeout

from tivtaam.browser import selectors as sel
from tivtaam.config import load_config
from tivtaam.util.logging import get_logger
from tivtaam.util.timing import jitter_sleep

log = get_logger(__name__)


class LoginRequiredError(RuntimeError):
    """Raised when the site demands an interactive login (captcha, OTP, ...)."""


def is_logged_in(page: Page) -> bool:
    return sel.resolve(page, "logged_in_indicator") is not None


def navigate_home(page: Page) -> None:
    cfg = load_config()
    page.goto(cfg.browser.base_url, wait_until="domcontentloaded")
    jitter_sleep()


def ensure_logged_in(page: Page) -> None:
    """If not logged in, attempt programmatic login. Raise if captcha/OTP appears."""
    cfg = load_config()
    navigate_home(page)
    if is_logged_in(page):
        log.info("Already logged in (cookies reused).")
        return

    log.info("Not logged in — attempting credential login.")
    login_link = sel.resolve(page, "login_link")
    if login_link is None:
        raise LoginRequiredError(
            "Could not find a login link from the home page. "
            "Re-run scripts/explore.py to refresh selectors."
        )
    login_link.first.click()
    jitter_sleep()

    username = sel.must_resolve(page, "login_username_field").first
    password = sel.must_resolve(page, "login_password_field").first
    submit = sel.must_resolve(page, "login_submit").first

    if not cfg.secrets.tivtaam_username or not cfg.secrets.tivtaam_password:
        raise LoginRequiredError(
            "TIVTAAM_USERNAME / TIVTAAM_PASSWORD missing from .env"
        )
    username.fill(cfg.secrets.tivtaam_username)
    password.fill(cfg.secrets.tivtaam_password)
    jitter_sleep()
    submit.click()

    try:
        page.wait_for_function(
            "() => !document.querySelector('input[type=\"password\"]')",
            timeout=cfg.browser.navigation_timeout_ms,
        )
    except PWTimeout as exc:
        raise LoginRequiredError(
            "Login form did not clear after submit — likely captcha/OTP. "
            "Run with RUN_HEADLESS=false and complete the login manually once; "
            "cookies will then persist in .pw-userdata/."
        ) from exc

    if not is_logged_in(page):
        raise LoginRequiredError(
            "Submitted credentials but logged-in indicator did not appear. "
            "Verify credentials, or complete an interactive login once."
        )
    log.info("Logged in successfully.")
