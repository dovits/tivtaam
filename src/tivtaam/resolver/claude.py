"""Step D: Claude fallback for ambiguous resolutions.

Per the plan doc:
- Default model: claude-haiku-4-5-20251001. Escalate to sonnet-4-6 on low
  confidence (handled by the caller — this module is single-call).
- System prompt is cached (ephemeral) so subsequent calls in the same session
  pay only for the per-item delta. Cache-hit rate matters because we may call
  this many times per shopping run.
- The model returns strict JSON: {sku, confidence (0-1), reasoning}.
- Each call is appended to logs/claude-YYYY-MM-DD.jsonl for audit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from anthropic import Anthropic

from tivtaam.config import load_config, repo_root
from tivtaam.resolver.search_step import SearchCandidate
from tivtaam.util.logging import get_logger

log = get_logger(__name__)

SYSTEM_PROMPT = """You are a Hebrew grocery shopping assistant. The user has a generic shopping list (e.g. "גבינה בולגרית") and you must pick the specific product they most likely want from a small list of candidates.

You receive:
- GENERIC: the user's input.
- CANDIDATES: a JSON array of products available on the site. Each has name, brand, size_text, price, and (when applicable) n_buys (how many times the user has bought this exact SKU before).
- HISTORY_HINT: a short summary of brand/size preferences inferred from past orders.

Decision rules, in order:
1. Strongly prefer a candidate with n_buys > 0 unless it clearly doesn't match the generic.
2. Match the generic's qualifiers exactly (fat %, weight, "organic", "kosher"). If the generic is silent on a qualifier, prefer the brand/variant in HISTORY_HINT.
3. Never substitute a different category (e.g. "גבינה לבנה" is not a substitute for "גבינה בולגרית").
4. If candidates are tied or none match well, return confidence ≤ 0.5 so the user can confirm.

Respond ONLY with a single JSON object, no commentary:
{"sku": "<sku string>", "confidence": <0.0-1.0>, "reasoning": "<one short Hebrew or English sentence>"}"""


@dataclass
class ClaudeResolution:
    sku: str | None
    confidence: float
    reasoning: str


def _client() -> Anthropic:
    cfg = load_config()
    return Anthropic(api_key=cfg.secrets.anthropic_api_key)


def _audit_path() -> Path:
    d = repo_root() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"claude-{datetime.now(UTC).strftime('%Y-%m-%d')}.jsonl"


def _append_audit(record: dict) -> None:
    try:
        with _audit_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("failed to append Claude audit: %s", e)


def _candidates_payload(cands: list[SearchCandidate]) -> list[dict]:
    return [
        {
            "sku": c.sku,
            "name": c.name,
            "brand": c.brand,
            "price": c.price,
            "fuzzy": c.fuzzy,
            "history_bonus": c.history_bonus,
        }
        for c in cands[:8]
    ]


def resolve_with_claude(
    generic: str,
    candidates: list[SearchCandidate],
    history_hint: str = "",
    escalate: bool = False,
) -> ClaudeResolution:
    """Call Claude to pick the best candidate. Caller decides whether to escalate."""
    cfg = load_config()
    if not cfg.secrets.anthropic_api_key:
        return ClaudeResolution(sku=None, confidence=0.0, reasoning="no anthropic api key")

    if not candidates:
        return ClaudeResolution(sku=None, confidence=0.0, reasoning="no candidates")

    model = cfg.claude.model_escalate if escalate else cfg.claude.model_default
    user_message = json.dumps({
        "GENERIC": generic,
        "CANDIDATES": _candidates_payload(candidates),
        "HISTORY_HINT": history_hint,
    }, ensure_ascii=False, indent=2)

    system_blocks: list[dict] = [{"type": "text", "text": SYSTEM_PROMPT}]
    if cfg.claude.cache_system:
        system_blocks[0]["cache_control"] = {"type": "ephemeral"}

    try:
        resp = _client().messages.create(
            model=model,
            max_tokens=cfg.claude.max_tokens,
            temperature=cfg.claude.temperature,
            system=system_blocks,
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as e:
        log.warning("Claude call failed: %s", e)
        return ClaudeResolution(sku=None, confidence=0.0, reasoning=f"api error: {e}")

    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    parsed = _parse_response(raw)
    valid_skus = {c.sku for c in candidates}
    if parsed.sku not in valid_skus:
        parsed = ClaudeResolution(
            sku=None,
            confidence=0.0,
            reasoning=f"model returned unknown sku: {parsed.sku!r}",
        )

    _append_audit({
        "ts": datetime.now(UTC).isoformat(),
        "generic": generic,
        "model": model,
        "request": user_message,
        "response_raw": raw,
        "parsed": parsed.__dict__,
        "usage": getattr(resp, "usage", None).__dict__ if getattr(resp, "usage", None) else None,
    })

    return parsed


def _parse_response(text: str) -> ClaudeResolution:
    text = text.strip()
    # Strip optional code fences.
    if text.startswith("```"):
        text = text.strip("`")
        # If there's a language tag like 'json\n', drop it.
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        obj = json.loads(text)
    except Exception:
        return ClaudeResolution(sku=None, confidence=0.0, reasoning="non-JSON response")
    return ClaudeResolution(
        sku=str(obj.get("sku")) if obj.get("sku") is not None else None,
        confidence=float(obj.get("confidence", 0.0)),
        reasoning=str(obj.get("reasoning", "")),
    )
