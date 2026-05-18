"""Gemini backend for the LLM resolver step.

Uses Gemini 2.5 Flash via plain HTTP (no SDK dep). Free tier: 15 RPM / 1,500
RPD as of late 2025 — easily enough for personal shopping runs.

Set `GEMINI_API_KEY` in `.env`. Get a key from https://aistudio.google.com/app/apikey.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from tivtaam.config import load_config, repo_root
from tivtaam.resolver.claude import (
    SYSTEM_PROMPT,
    ClaudeResolution,
    _candidates_payload,
    _parse_response,
)
from tivtaam.resolver.search_step import SearchCandidate
from tivtaam.util.logging import get_logger

log = get_logger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_ENDPOINT_TMPL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
)


def _audit_path() -> Path:
    d = repo_root() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"gemini-{datetime.now(UTC).strftime('%Y-%m-%d')}.jsonl"


def _append_audit(record: dict) -> None:
    try:
        with _audit_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("failed to append Gemini audit: %s", e)


def resolve_with_gemini(
    generic: str,
    candidates: list[SearchCandidate],
    history_hint: str = "",
) -> ClaudeResolution:
    cfg = load_config()
    if not cfg.secrets.gemini_api_key:
        return ClaudeResolution(sku=None, confidence=0.0, reasoning="no gemini api key")
    if not candidates:
        return ClaudeResolution(sku=None, confidence=0.0, reasoning="no candidates")

    user_message = json.dumps({
        "GENERIC": generic,
        "CANDIDATES": _candidates_payload(candidates),
        "HISTORY_HINT": history_hint,
    }, ensure_ascii=False, indent=2)

    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
        "generationConfig": {
            "temperature": cfg.claude.temperature,
            "maxOutputTokens": cfg.claude.max_tokens,
            "responseMimeType": "application/json",
            # gemini-2.5-flash thinks by default and thinking tokens draw from
            # maxOutputTokens — leaving the JSON answer truncated. This is a
            # deterministic pick task, so disable thinking entirely.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    url = GEMINI_ENDPOINT_TMPL.format(model=GEMINI_MODEL, key=cfg.secrets.gemini_api_key)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        log.warning("gemini http error %s: %s", e.code, body_text[:400])
        return ClaudeResolution(sku=None, confidence=0.0, reasoning=f"gemini http {e.code}: {body_text[:200]}")
    except Exception as e:
        log.warning("gemini call failed: %s", e)
        return ClaudeResolution(sku=None, confidence=0.0, reasoning=f"gemini error: {e}")

    try:
        payload = json.loads(raw)
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        log.warning("gemini parse failed: %s — raw=%s", e, raw[:300])
        return ClaudeResolution(sku=None, confidence=0.0, reasoning=f"gemini parse error: {e}")

    parsed = _parse_response(text)
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
        "model": GEMINI_MODEL,
        "request": user_message,
        "response_raw": text,
        "parsed": parsed.__dict__,
    })
    return parsed
