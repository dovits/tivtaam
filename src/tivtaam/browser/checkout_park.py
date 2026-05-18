"""Hard invariant: the runner must NEVER trigger checkout/payment/order submission.

The plan doc states this in big letters. Implementation strategy:

1. `FORBIDDEN_TEXTS` is the exact set of Hebrew/English strings whose presence on
   a clickable element identifies it as a payment/order-submit control.
2. `assert_no_checkout_action(locator)` raises if a locator we're about to click
   carries such text.
3. A unit test asserts that `data/selectors.yaml` never maps any key to one of
   these strings.
"""

from __future__ import annotations

import re

from playwright.sync_api import Locator

FORBIDDEN_TEXTS = (
    "תשלום",
    "שלם",
    "שלח הזמנה",
    "סיים הזמנה",
    "place order",
    "checkout",
    "pay now",
    "submit order",
    "complete purchase",
)

_FORBIDDEN_RE = re.compile("|".join(re.escape(t) for t in FORBIDDEN_TEXTS), re.IGNORECASE)


class CheckoutAttemptError(RuntimeError):
    """Raised when code is about to click a checkout/payment control."""


def text_is_forbidden(text: str | None) -> bool:
    if not text:
        return False
    return bool(_FORBIDDEN_RE.search(text))


def assert_no_checkout_action(locator: Locator) -> None:
    """Inspect a locator we're about to click and refuse if it looks like checkout."""
    try:
        txt = (locator.text_content() or "").strip()
    except Exception:
        txt = ""
    if text_is_forbidden(txt):
        raise CheckoutAttemptError(
            f"refusing to click element with text={txt!r} — matches forbidden checkout vocab"
        )
    try:
        aria = (locator.get_attribute("aria-label") or "")
    except Exception:
        aria = ""
    if text_is_forbidden(aria):
        raise CheckoutAttemptError(
            f"refusing to click element with aria-label={aria!r}"
        )
