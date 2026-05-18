"""Generic→specific resolution pipeline.

Steps implemented:
  A. Alias hit (generic_aliases) — return immediately if fresh and SKU available.
  B. History scoring — see resolver.history_score.
  C. Site search — see resolver.search_step (requires a Playwright Page).
  D. Claude fallback — TODO.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from playwright.sync_api import Page

from tivtaam.browser.search import search_with_cache
from tivtaam.config import load_config
from tivtaam.db.models import GenericAlias, Product
from tivtaam.db.repo import (
    find_synthetic_with_norm,
    get_alias,
    get_product,
    migrate_synthetic_to_real,
    upsert_alias,
    upsert_product,
)
from tivtaam.resolver.llm import resolve_with_llm
from tivtaam.resolver.history_score import score_history
from tivtaam.resolver.search_step import SearchCandidate, score_search
from tivtaam.util.hebrew import normalize
from tivtaam.util.timing import now_iso

ResolutionSource = Literal["alias", "history", "search", "claude", "ask_user"]


@dataclass
class ResolvedAlternative:
    sku: str
    name: str
    score: float


@dataclass
class Resolution:
    generic: str
    sku: str | None
    name: str | None
    confidence: float
    source: ResolutionSource
    reason: str
    needs_user_confirmation: bool = False
    alternatives: list[ResolvedAlternative] = field(default_factory=list)


def _alias_is_fresh(a: GenericAlias) -> bool:
    cfg = load_config()
    try:
        dt = datetime.strptime(a.last_confirmed_at[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=UTC
        )
    except Exception:
        return False
    return (datetime.now(UTC) - dt) < timedelta(days=cfg.thresholds.alias_max_age_days)


def _try_alias(conn: sqlite3.Connection, generic: str) -> Resolution | None:
    a = get_alias(conn, normalize(generic))
    if a is None:
        return None
    if not _alias_is_fresh(a):
        return None
    p = get_product(conn, a.preferred_sku)
    if p is None or p.is_available != 1:
        return None
    return Resolution(
        generic=generic,
        sku=p.sku,
        name=p.name,
        confidence=max(a.confidence, 0.95),
        source="alias",
        reason=f"alias hit (source={a.source}, used {a.times_used}× before)",
    )


def _from_history(conn: sqlite3.Connection, generic: str) -> Resolution | None:
    cfg = load_config()
    candidates = score_history(conn, generic)
    if not candidates:
        return None
    top = candidates[0]
    runner_up = candidates[1].composite if len(candidates) > 1 else 0.0
    margin = top.composite - runner_up

    alts = [
        ResolvedAlternative(sku=c.sku, name=c.name, score=round(c.composite, 3))
        for c in candidates[1:4]
    ]

    # A strong, actually-purchased top wins outright — the core purpose of this
    # project is "pick what I usually buy". A close runner-up that is *also* a
    # purchased item is not ambiguity the user cares about (they buy both), so
    # the margin gate must not defer here. composite already encodes frequency
    # and recency, so the top is the best ("most recent / most bought") match.
    strong_purchased = (
        top.is_available
        and top.composite >= cfg.thresholds.history_high
        and top.n_purchases >= 1
    )
    if strong_purchased or (
        top.is_available
        and top.composite >= cfg.thresholds.history_high
        and margin >= cfg.thresholds.history_margin
    ):
        return Resolution(
            generic=generic,
            sku=top.sku,
            name=top.name,
            confidence=min(0.99, top.composite),
            source="history",
            reason=(
                f"history match: {top.n_purchases}× purchased, "
                f"fuzzy={top.fuzzy}, composite={top.composite:.2f}, margin={margin:.2f}"
            ),
            alternatives=alts,
        )

    # Top-of-history is plausible but not auto-acceptable → ask user.
    return Resolution(
        generic=generic,
        sku=top.sku,
        name=top.name,
        confidence=top.composite,
        source="ask_user",
        reason=(
            f"low confidence from history alone (composite={top.composite:.2f}, "
            f"margin={margin:.2f}). top {len(candidates)} candidates ranked."
        ),
        needs_user_confirmation=True,
        alternatives=alts,
    )


def _upsert_search_candidate(conn: sqlite3.Connection, c: SearchCandidate) -> None:
    """Persist a search candidate as a product row (with its real catalog SKU).

    If we already have a synthetic `name:` SKU with matching normalized name,
    migrate purchases + aliases over to the real SKU and drop the synthetic.
    This makes live cart writes work for items that came from history-only sync.
    """
    norm = normalize(c.name)
    synthetic = find_synthetic_with_norm(conn, norm)
    if synthetic and synthetic != c.sku:
        migrate_synthetic_to_real(conn, synthetic, c.sku)
    upsert_product(conn, Product(
        sku=c.sku,
        name=c.name,
        name_norm=norm,
        brand=c.brand,
        last_price=c.price,
        last_seen_at=now_iso(),
        is_available=1,
    ))


def _history_hint(conn: sqlite3.Connection, generic: str) -> str:
    """Compact prose describing what this user usually buys near `generic`."""
    from tivtaam.db.repo import list_purchase_history_for_norm
    rows = list_purchase_history_for_norm(conn)
    if not rows:
        return ""
    hits = []
    g_norm = normalize(generic)
    for r in rows:
        if not r["name"]:
            continue
        n_norm = normalize(r["name"])
        if g_norm and any(tok in n_norm for tok in g_norm.split() if len(tok) > 2):
            hits.append((r["name"], int(r["n_purchases"] or 0)))
    hits.sort(key=lambda x: x[1], reverse=True)
    if not hits:
        return ""
    return "; ".join(f"{name} ({n}×)" for name, n in hits[:5])


def _claude_step(
    conn: sqlite3.Connection,
    generic: str,
    cands: list[SearchCandidate],
) -> Resolution | None:
    cfg = load_config()
    if not (cfg.secrets.anthropic_api_key or cfg.secrets.gemini_api_key):
        return None
    hint = _history_hint(conn, generic)
    decision = resolve_with_llm(generic, cands, history_hint=hint, escalate=False)
    if decision.confidence < 0.6 and decision.sku is not None:
        decision = resolve_with_llm(generic, cands, history_hint=hint, escalate=True)
    if decision.sku is None:
        return None
    pick = next((c for c in cands if c.sku == decision.sku), None)
    if pick is None:
        return None
    auto = decision.confidence >= cfg.thresholds.claude_high
    return Resolution(
        generic=generic,
        sku=pick.sku,
        name=pick.name,
        confidence=decision.confidence,
        source="claude" if auto else "ask_user",
        reason=f"llm: {decision.reasoning}",
        needs_user_confirmation=not auto,
        alternatives=[
            ResolvedAlternative(sku=c.sku, name=c.name, score=round(c.composite_float, 3))
            for c in cands[:4] if c.sku != pick.sku
        ][:3],
    )


def _from_search(
    conn: sqlite3.Connection,
    page: Page,
    generic: str,
) -> Resolution | None:
    cfg = load_config()
    results = search_with_cache(conn, page, generic, limit=10)
    if not results:
        return None
    cands = score_search(conn, generic, results)
    if not cands:
        return None
    top = cands[0]
    runner_up = cands[1].composite if len(cands) > 1 else 0
    margin = top.composite - runner_up

    # Persist all candidates as products so the user (and later the cart writer)
    # can reference them by real SKU.
    for c in cands:
        _upsert_search_candidate(conn, c)

    alts = [
        ResolvedAlternative(sku=c.sku, name=c.name, score=round(c.composite_float, 3))
        for c in cands[1:4]
    ]

    if (
        top.composite >= cfg.thresholds.search_high
        and margin >= cfg.thresholds.search_margin
    ):
        return Resolution(
            generic=generic,
            sku=top.sku,
            name=top.name,
            confidence=top.composite_float,
            source="search",
            reason=(
                f"site search: fuzzy={top.fuzzy}, history_bonus={top.history_bonus}, "
                f"composite={top.composite}, margin={margin}"
            ),
            alternatives=alts,
        )

    # Search was inconclusive — try Claude.
    claude_res = _claude_step(conn, generic, cands)
    if claude_res is not None:
        return claude_res

    return Resolution(
        generic=generic,
        sku=top.sku,
        name=top.name,
        confidence=top.composite_float,
        source="ask_user",
        reason=(
            f"search top is plausible but not auto-acceptable "
            f"(composite={top.composite}, margin={margin}). top candidates ranked."
        ),
        needs_user_confirmation=True,
        alternatives=alts,
    )


def resolve(
    conn: sqlite3.Connection,
    generic: str,
    page: Page | None = None,
) -> Resolution:
    """Resolve a generic name to a SKU. If a Page is provided, runs site search."""
    if not generic.strip():
        return Resolution(
            generic=generic,
            sku=None,
            name=None,
            confidence=0.0,
            source="ask_user",
            reason="empty input",
            needs_user_confirmation=True,
        )

    hit = _try_alias(conn, generic)
    if hit:
        return hit

    h = _from_history(conn, generic)
    if h and not h.needs_user_confirmation:
        return h

    if page is not None:
        s = _from_search(conn, page, generic)
        if s and not s.needs_user_confirmation:
            return s
        # If both history and search ask the user, prefer search's candidates
        # (real SKUs, broader coverage).
        if s is not None:
            return s

    if h is not None:
        return h

    return Resolution(
        generic=generic,
        sku=None,
        name=None,
        confidence=0.0,
        source="ask_user",
        reason="no history match and no site-search results (Claude fallback TODO)",
        needs_user_confirmation=True,
    )


def confirm_user_choice(conn: sqlite3.Connection, generic: str, sku: str) -> None:
    """Persist a user confirmation as a generic_alias with confidence=1.0."""
    upsert_alias(
        conn,
        GenericAlias(
            generic_name=generic,
            generic_name_norm=normalize(generic),
            preferred_sku=sku,
            confidence=1.0,
            source="user_confirmed",
            last_confirmed_at=now_iso(),
            times_used=1,
        ),
    )


def record_auto_resolution(conn: sqlite3.Connection, res: Resolution) -> None:
    """Record an automatic resolution as an alias with its computed confidence."""
    if not res.sku or res.needs_user_confirmation:
        return
    upsert_alias(
        conn,
        GenericAlias(
            generic_name=res.generic,
            generic_name_norm=normalize(res.generic),
            preferred_sku=res.sku,
            confidence=res.confidence,
            source=res.source if res.source in ("history", "claude") else "history",
            last_confirmed_at=now_iso(),
            times_used=1,
        ),
    )
