"""Per-run worker: resolves a job's items, writes plan_json, posts to Telegram.

Spawned by the bot via `python -m tivtaam.runner --job-id <id>` (or as a script
through `src/tivtaam/runner.py`). Short-lived: opens a Playwright context,
resolves each item, persists the plan, and notifies the bot's user.

Doesn't interact with the Telegram update loop — uses the Bot HTTP API directly
to push a single status message back to the configured user.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Allow running as a plain script: python src/tivtaam/runner.py …
if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tivtaam.bot.ipc import get_job, update_state
from tivtaam.browser.auth import ensure_logged_in
from tivtaam.browser.context import open_context, open_page
from tivtaam.config import load_config
from tivtaam.db.migrations import ensure_db
from tivtaam.db.repo import create_question
from tivtaam.resolver.category import category_candidates, match_category
from tivtaam.resolver.pipeline import Resolution, resolve
from tivtaam.util.logging import configure, get_logger

log = get_logger(__name__)


def _telegram_send(
    token: str,
    chat_id: int,
    text: str,
    reply_markup: dict | None = None,
) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    data = urllib.parse.urlencode(payload).encode("utf-8")
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as resp:
            resp.read()
    except Exception as e:
        log.warning("telegram send failed: %s", e)


def _build_question_keyboard(qid: int, top: Resolution) -> dict:
    """Inline keyboard offering the top resolution + its top-3 alternatives + skip."""
    rows: list[list[dict]] = []
    if top.sku:
        rows.append([{
            "text": f"✅ {top.name or top.sku}",
            "callback_data": f"ans|{qid}|{top.sku}",
        }])
    for a in top.alternatives[:3]:
        rows.append([{
            "text": a.name or a.sku,
            "callback_data": f"ans|{qid}|{a.sku}",
        }])
    rows.append([{"text": "❌ Skip", "callback_data": f"ans|{qid}|skip"}])
    return {"inline_keyboard": rows}


def _build_multiselect_keyboard(
    qid: int, candidates: list[dict], selected: set[int] | None = None
) -> dict:
    """Toggle list for a category pick. callback_data carries the candidate
    *index* (not the SKU) to stay under Telegram's 64-byte limit — synthetic
    `name:` SKUs are far too long."""
    selected = selected or set()
    rows: list[list[dict]] = []
    for i, c in enumerate(candidates):
        mark = "☑️ " if i in selected else "▫️ "
        n = c.get("n")
        suffix = f" ({n}×)" if n else ""
        rows.append([{
            "text": f"{mark}{c['name']}{suffix}"[:64],
            "callback_data": f"msel|{qid}|{i}",
        }])
    rows.append([
        {"text": "✅ Done", "callback_data": f"msel|{qid}|done"},
        {"text": "❌ Skip", "callback_data": f"msel|{qid}|skip"},
    ])
    return {"inline_keyboard": rows}


def _serialize_plan(plan: list[Resolution]) -> str:
    return json.dumps([
        {
            "generic": r.generic,
            "sku": r.sku,
            "name": r.name,
            "confidence": r.confidence,
            "source": r.source,
            "needs_user_confirmation": r.needs_user_confirmation,
            "reason": r.reason,
        }
        for r in plan
    ], ensure_ascii=False)


def run_job(job_id: str) -> int:
    cfg = load_config()
    configure(level=cfg.secrets.log_level)
    conn = ensure_db()
    job = get_job(conn, job_id)
    if job is None:
        log.error("unknown job %s", job_id)
        return 2

    items = json.loads(job.raw_list_json)
    update_state(conn, job_id, "running")

    def _category_placeholder(g: str) -> Resolution:
        # Broad category (e.g. חטיפים): don't resolve to one SKU — defer to a
        # multi-pick question over past purchases. Needs no browser.
        return Resolution(
            generic=g, sku=None, name=None, confidence=0.0,
            source="ask_user", reason="category — multi-pick",
            needs_user_confirmation=True,
        )

    # Only spin up Playwright if something actually needs site search. A
    # category-only list needs no browser — and opening the singleton
    # .pw-userdata profile while another run holds it crashes the job.
    plan: list[Resolution] = []
    if any(not match_category(g) for g in items):
        with open_context() as ctx:
            page = open_page(ctx)
            ensure_logged_in(page)
            for g in items:
                plan.append(
                    _category_placeholder(g) if match_category(g)
                    else resolve(conn, g, page=page)
                )
    else:
        plan = [_category_placeholder(g) for g in items]

    plan_json = _serialize_plan(plan)
    update_state(conn, job_id, "awaiting_user", plan_json=plan_json)

    if cfg.secrets.telegram_bot_token and cfg.secrets.telegram_allowed_user_id:
        token = cfg.secrets.telegram_bot_token
        chat = cfg.secrets.telegram_allowed_user_id
        lines = [f"*Plan ready* (`{job_id}`):"]
        for r in plan[:12]:
            icon = "✅" if (r.sku and not r.needs_user_confirmation) else "❓"
            lines.append(f"{icon} {r.generic} → {r.name or '—'}")
        if len(plan) > 12:
            lines.append(f"…and {len(plan) - 12} more")
        _telegram_send(token, chat, "\n".join(lines))

        # For each ambiguous item, create a pending question and send an
        # inline keyboard so the user can tap a choice (which writes an alias).
        ambiguous = [r for r in plan if r.needs_user_confirmation]
        for r in ambiguous:
            cat = match_category(r.generic)
            if cat:
                cands = category_candidates(conn, cat)
                if not cands:
                    _telegram_send(
                        token, chat,
                        f"*{r.generic}*: no matching past purchases found — skipping.",
                    )
                    continue
                cand_dump = [
                    {"sku": c.sku, "name": c.name, "n": c.n_purchases} for c in cands
                ]
                qid = create_question(conn, job_id, r.generic, cand_dump)
                _telegram_send(
                    token, chat,
                    f"*{r.generic}* — tap all you want, then *Done*:",
                    reply_markup=_build_multiselect_keyboard(qid, cand_dump),
                )
                continue
            cand_dump = [
                {"sku": r.sku, "name": r.name, "score": r.confidence, "is_top": True},
            ] if r.sku else []
            for a in r.alternatives[:3]:
                cand_dump.append({"sku": a.sku, "name": a.name, "score": a.score})
            qid = create_question(conn, job_id, r.generic, cand_dump)
            text = f"*{r.generic}* — pick one:"
            _telegram_send(token, chat, text, reply_markup=_build_question_keyboard(qid, r))

        if ambiguous:
            _telegram_send(
                token, chat,
                f"{len(ambiguous)} item(s) need your pick. Tap a button on each, then /approve `{job_id}` to write the cart.",
            )
        else:
            _telegram_send(token, chat, f"Everything auto-resolved. /approve `{job_id}` to write the cart.")

    return 0


def approve_job(job_id: str, dry_run: bool = True) -> int:
    """Apply user's pending-question answers, then write the cart (dry-run by default)."""
    from tivtaam.browser.cart import CartLine, execute_plan
    from tivtaam.db.repo import (
        get_alias,
        get_product,
        open_questions_for_job,
        upsert_product,
    )
    from tivtaam.db.models import Product
    from tivtaam.util.hebrew import normalize
    from tivtaam.util.timing import now_iso

    cfg = load_config()
    configure(level=cfg.secrets.log_level)
    conn = ensure_db()
    job = get_job(conn, job_id)
    if job is None:
        log.error("unknown job %s", job_id)
        return 2

    plan = json.loads(job.plan_json or "[]")
    # Apply any answered questions to the plan
    open_q = open_questions_for_job(conn, job_id)
    if open_q:
        log.info("%d questions still open for job %s", len(open_q), job_id)
    answered = conn.execute(
        "SELECT * FROM pending_questions WHERE job_id = ? AND state = 'answered'",
        (job_id,),
    ).fetchall()
    answered_by_generic = {row["generic_name"]: row for row in answered}
    skipped = {
        row["generic_name"]
        for row in conn.execute(
            "SELECT generic_name FROM pending_questions "
            "WHERE job_id = ? AND state = 'skipped'",
            (job_id,),
        ).fetchall()
    }
    # A category multi-pick stores chosen_sku as a JSON list of SKUs; a normal
    # pick stores a single SKU string. Expand the former into one plan entry
    # per chosen product.
    category_expansions: dict[str, list[dict]] = {}
    for entry in plan:
        row = answered_by_generic.get(entry["generic"])
        if row is None:
            continue
        raw = row["chosen_sku"]
        if not raw or raw == "skip":
            continue
        picked = None
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                picked = parsed
        except (ValueError, TypeError):
            picked = None
        cand_by_sku = {
            c["sku"]: c for c in json.loads(row["candidates_json"] or "[]")
        }
        if picked is not None:
            category_expansions[entry["generic"]] = [
                {
                    "generic": entry["generic"],
                    "sku": s,
                    "name": (cand_by_sku.get(s) or {}).get("name") or s,
                    "confidence": 1.0,
                    "source": "user_confirmed",
                    "needs_user_confirmation": False,
                    "reason": "category multi-pick",
                }
                for s in picked
            ]
        else:
            entry["sku"] = raw
            if raw in cand_by_sku:
                entry["name"] = cand_by_sku[raw]["name"]
            entry["needs_user_confirmation"] = False

    if category_expansions:
        expanded: list[dict] = []
        for e in plan:
            repl = category_expansions.get(e["generic"])
            if repl is not None:
                expanded.extend(repl)
                category_expansions[e["generic"]] = []  # expand once
            else:
                expanded.append(e)
        plan = expanded

    # Write the cart (or print dry-run). A skipped item must NOT be added even
    # though its plan entry still carries the originally-guessed sku.
    lines = [
        CartLine(sku=e["sku"], name=e.get("name") or e["sku"])
        for e in plan
        if e.get("sku")
        and e["sku"] != "skip"
        and e["generic"] not in skipped
    ]
    if dry_run:
        result = execute_plan(None, lines, dry_run=True)
        update_state(conn, job_id, "approved", plan_json=json.dumps(plan, ensure_ascii=False))
        if cfg.secrets.telegram_bot_token and cfg.secrets.telegram_allowed_user_id:
            _telegram_send(
                cfg.secrets.telegram_bot_token,
                cfg.secrets.telegram_allowed_user_id,
                f"*Dry-run done* (`{job_id}`):\nwould add {len(result.added)} sku(s).",
            )
        return 0

    with open_context() as ctx:
        page = open_page(ctx)
        ensure_logged_in(page)
        result = execute_plan(page, lines, dry_run=False)

        clipped: list[str] = []
        surfaced: list[str] = []
        if cfg.coupons.auto_clip:
            try:
                from tivtaam.browser.coupons import auto_clip
                cres = auto_clip(page)
                clipped = cres.clipped_titles
                surfaced = cres.surfaced_titles
            except Exception as e:
                log.warning("coupon auto-clip failed: %s", e)

        # Park at /cart so the user can confirm + pay manually.
        try:
            page.goto(f"{cfg.browser.base_url}/cart", wait_until="domcontentloaded")
        except Exception as e:
            log.warning("could not navigate to /cart: %s", e)

        update_state(conn, job_id, "approved", plan_json=json.dumps(plan, ensure_ascii=False))

    if cfg.secrets.telegram_bot_token and cfg.secrets.telegram_allowed_user_id:
        parts = [
            f"*Cart written* (`{job_id}`):",
            f"added: {', '.join(result.added) or '—'}",
        ]
        if result.failed:
            parts.append(f"failed: {', '.join(result.failed)}")
        if clipped:
            parts.append(f"clipped coupons: {', '.join(clipped)}")
        if surfaced:
            parts.append(f"coupons to review: {', '.join(surfaced)}")
        parts.append("Browser parked at /cart — pay manually.")
        _telegram_send(
            cfg.secrets.telegram_bot_token,
            cfg.secrets.telegram_allowed_user_id,
            "\n".join(parts),
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--from", dest="source", default="cli")
    ap.add_argument("--approve", action="store_true", help="Apply answered questions and write cart")
    ap.add_argument("--live", action="store_true", help="With --approve, actually click add-to-cart")
    args = ap.parse_args()
    if args.approve:
        return approve_job(args.job_id, dry_run=not args.live)
    return run_job(args.job_id)


if __name__ == "__main__":
    sys.exit(main())
