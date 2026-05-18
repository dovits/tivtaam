"""LLM dispatcher with provider fallback.

Tries providers in order based on which API keys are present. Once a provider
returns a credit/quota error in this process, it's disabled for the rest of the
session so subsequent items don't pay the latency to fail again.
"""

from __future__ import annotations

from tivtaam.config import load_config
from tivtaam.resolver.claude import ClaudeResolution, resolve_with_claude
from tivtaam.resolver.gemini import resolve_with_gemini
from tivtaam.resolver.search_step import SearchCandidate
from tivtaam.util.logging import get_logger

log = get_logger(__name__)

_DISABLED: set[str] = set()
_CREDIT_KEYWORDS = ("credit", "balance", "quota", "exceeded", "rate", "429")


def _looks_like_quota(reasoning: str) -> bool:
    low = (reasoning or "").lower()
    return any(k in low for k in _CREDIT_KEYWORDS)


def reset_disabled() -> None:
    """Useful in tests."""
    _DISABLED.clear()


def resolve_with_llm(
    generic: str,
    candidates: list[SearchCandidate],
    history_hint: str = "",
    escalate: bool = False,
) -> ClaudeResolution:
    cfg = load_config()
    providers: list[str] = []
    if cfg.secrets.anthropic_api_key and "anthropic" not in _DISABLED:
        providers.append("anthropic")
    if cfg.secrets.gemini_api_key and "gemini" not in _DISABLED:
        providers.append("gemini")

    last: ClaudeResolution | None = None
    for p in providers:
        if p == "anthropic":
            res = resolve_with_claude(generic, candidates, history_hint, escalate=escalate)
        else:
            res = resolve_with_gemini(generic, candidates, history_hint)
        last = res
        if res.sku is not None:
            return res
        if _looks_like_quota(res.reasoning):
            log.warning("disabling %s for this run: %s", p, res.reasoning)
            _DISABLED.add(p)
            continue
        # Other failure — try next provider but keep this one for retry next time.
    return last or ClaudeResolution(
        sku=None, confidence=0.0, reasoning="no LLM provider configured"
    )
