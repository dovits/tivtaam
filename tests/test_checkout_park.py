"""Hard invariant: no checkout/payment vocab leaks into our actions."""

from __future__ import annotations

from pathlib import Path

import yaml

from tivtaam.browser.checkout_park import FORBIDDEN_TEXTS, text_is_forbidden


def test_text_is_forbidden_matches_hebrew():
    assert text_is_forbidden("תשלום")
    assert text_is_forbidden("שלם עכשיו")
    assert text_is_forbidden("שלח הזמנה")


def test_text_is_forbidden_matches_english_case_insensitive():
    assert text_is_forbidden("Place Order")
    assert text_is_forbidden("CHECKOUT")
    assert text_is_forbidden("Submit Order Now")


def test_text_is_forbidden_ignores_safe_strings():
    assert not text_is_forbidden("הוספה לסל")
    assert not text_is_forbidden("Add to cart")
    assert not text_is_forbidden("")
    assert not text_is_forbidden(None)


def test_selectors_yaml_never_targets_checkout_vocab():
    """If a selector resolves to text containing checkout vocab, we have a bug."""
    repo_root = Path(__file__).resolve().parents[1]
    path = repo_root / "data" / "selectors.yaml"
    if not path.exists():
        return
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    flat = yaml.dump(raw, allow_unicode=True)
    for forbidden in FORBIDDEN_TEXTS:
        # Allow partial substrings only inside *_park comments — there are none.
        assert forbidden not in flat, f"selectors.yaml mentions forbidden text {forbidden!r}"
