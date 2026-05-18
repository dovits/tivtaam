"""Multi-strategy selector resolver.

Selectors are recorded in `data/selectors.yaml` with a list of candidate
strategies (testid, role, css, text). At runtime we try them in order and
return the first locator that resolves to ≥1 element.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

import yaml
from playwright.sync_api import Locator, Page

from tivtaam.config import repo_root

SELECTORS_PATH = repo_root() / "data" / "selectors.yaml"


@dataclass
class Strategy:
    kind: str   # 'testid' | 'role' | 'css' | 'text'
    value: str


@lru_cache(maxsize=1)
def _load() -> dict[str, list[Strategy]]:
    if not SELECTORS_PATH.exists():
        return {}
    raw = yaml.safe_load(SELECTORS_PATH.read_text(encoding="utf-8")) or {}
    out: dict[str, list[Strategy]] = {}
    for key, entries in raw.items():
        strategies: list[Strategy] = []
        for e in entries or []:
            if not isinstance(e, dict):
                continue
            kind, value = next(iter(e.items()))
            strategies.append(Strategy(kind=kind, value=str(value)))
        out[key] = strategies
    return out


def reload() -> None:
    _load.cache_clear()


def _build_locator(page: Page, s: Strategy) -> Locator:
    if s.kind == "testid":
        return page.get_by_test_id(s.value)
    if s.kind == "role":
        if ":" in s.value:
            role, name = s.value.split(":", 1)
            return page.get_by_role(role, name=re.compile(name))
        return page.get_by_role(s.value)
    if s.kind == "css":
        return page.locator(s.value)
    if s.kind == "text":
        return page.get_by_text(re.compile(s.value))
    raise ValueError(f"Unknown selector strategy: {s.kind}")


def resolve(page: Page, key: str) -> Locator | None:
    """Return the first locator strategy that resolves to ≥1 element, else None."""
    strategies = _load().get(key, [])
    for s in strategies:
        try:
            loc = _build_locator(page, s)
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def must_resolve(page: Page, key: str) -> Locator:
    loc = resolve(page, key)
    if loc is None:
        raise SelectorMissingError(
            f"None of the selectors for '{key}' resolved on the current page. "
            "Re-run scripts/explore.py to refresh data/selectors.yaml."
        )
    return loc


class SelectorMissingError(RuntimeError):
    pass
