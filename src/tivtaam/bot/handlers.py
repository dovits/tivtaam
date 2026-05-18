"""Bot command handlers.

UX goals:
- `/shop` enters "list entry" state — next non-command message is parsed as
  newline-separated generics, then queued as a job and a runner subprocess is
  spawned.
- `/status` shows the latest job's state + a summary if it has a plan_json.
- `/approve <job>` flips state to `approved` (the runner will pick this up
  next pass; in Phase 4 we run the runner synchronously, so this is reserved
  for later).
- `/help` lists commands.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from tivtaam.bot.auth_guard import ensure_allowed, is_allowed
from tivtaam.bot.ipc import get_job, latest_job, new_job, update_state
from tivtaam.config import repo_root
from tivtaam.db.migrations import ensure_db
from tivtaam.db.repo import (
    answer_question,
    get_question,
    open_questions_for_job,
    set_question_selection,
    upsert_alias,
)
from tivtaam.db.models import GenericAlias
from tivtaam.util.hebrew import normalize
from tivtaam.util.logging import get_logger
from tivtaam.util.timing import now_iso

log = get_logger(__name__)
router = Router()

# Per-user "awaiting list" state. Kept in-memory because we only serve one user.
_awaiting_list_from: set[int] = set()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not await ensure_allowed(message):
        return
    await message.reply(
        "Tivtaam shopping bot.\n"
        "Send /shop, then paste your shopping list (one item per line)."
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not await ensure_allowed(message):
        return
    await message.reply(
        "/shop — start a new shopping run\n"
        "/status — show last job state\n"
        "/approve [job] — dry-run the cart write\n"
        "/approve [job] live — write the REAL cart, then park at /cart (pay manually)\n"
        "/cancel — drop pending list-entry state"
    )


async def _queue_and_spawn(message: Message, items: list[str]) -> None:
    """Queue a job and spawn runner.py. Replies to the user with the job id."""
    conn = ensure_db()
    job_id = new_job(conn, source="telegram", items=items)
    await message.reply(
        f"Queued job `{job_id}` with {len(items)} items. Resolving…",
        parse_mode="Markdown",
    )
    runner = repo_root() / "src" / "tivtaam" / "runner.py"
    log.info("spawning runner for job=%s", job_id)
    try:
        await asyncio.to_thread(
            lambda: subprocess.Popen(
                [sys.executable, str(runner), "--job-id", job_id, "--from", "telegram"],
                cwd=str(repo_root()),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
        )
    except Exception as e:
        update_state(conn, job_id, "failed")
        await message.reply(f"Failed to spawn runner: {e}")


@router.message(Command("shop"))
async def cmd_shop(message: Message) -> None:
    if not await ensure_allowed(message):
        return
    uid = message.from_user.id if message.from_user else 0
    # If /shop was sent with an inline list (newline-separated after the command),
    # queue it immediately instead of waiting for a follow-up message.
    lines = (message.text or "").split("\n")
    inline = [ln.strip() for ln in lines[1:] if ln.strip()]
    if inline:
        _awaiting_list_from.discard(uid)
        await _queue_and_spawn(message, inline)
        return
    _awaiting_list_from.add(uid)
    await message.reply("Send your shopping list (one item per line).")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    if not await ensure_allowed(message):
        return
    uid = message.from_user.id if message.from_user else 0
    _awaiting_list_from.discard(uid)
    await message.reply("Cancelled.")


@router.callback_query(lambda c: c.data and c.data.startswith("ans|"))
async def cb_answer(query: CallbackQuery) -> None:
    if not is_allowed(query.from_user.id if query.from_user else None):
        await query.answer("Unauthorized", show_alert=True)
        return
    try:
        _, qid_str, sku = (query.data or "").split("|", 2)
        qid = int(qid_str)
    except Exception:
        await query.answer("malformed callback", show_alert=True)
        return

    conn = ensure_db()
    q = get_question(conn, qid)
    if q is None:
        await query.answer("question not found", show_alert=True)
        return

    if sku == "skip":
        answer_question(conn, qid, None, state="skipped")
        await query.answer("skipped")
        if query.message:
            await query.message.edit_text(f"*{q['generic_name']}* — skipped.", parse_mode="Markdown")
        return

    # Lookup the chosen candidate's name from the question's saved candidates.
    cands = json.loads(q["candidates_json"])
    chosen = next((c for c in cands if c["sku"] == sku), None)
    name = chosen["name"] if chosen else sku

    answer_question(conn, qid, sku, state="answered")
    upsert_alias(conn, GenericAlias(
        generic_name=q["generic_name"],
        generic_name_norm=normalize(q["generic_name"]),
        preferred_sku=sku,
        confidence=1.0,
        source="user_confirmed",
        last_confirmed_at=now_iso(),
        times_used=1,
    ))

    await query.answer(f"✅ {name}")
    if query.message:
        await query.message.edit_text(
            f"*{q['generic_name']}* → ✅ {name}",
            parse_mode="Markdown",
        )

    # If this was the last open question for the job, hint the user to /approve.
    remaining = open_questions_for_job(conn, q["job_id"])
    if not remaining and query.message:
        await query.message.answer(
            f"All picks recorded. /approve `{q['job_id']}` to write the cart.",
            parse_mode="Markdown",
        )


def _msel_keyboard(qid: int, cands: list[dict], selected: set[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, c in enumerate(cands):
        mark = "☑️ " if c["sku"] in selected else "▫️ "
        n = c.get("n")
        suffix = f" ({n}×)" if n else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{c['name']}{suffix}"[:64],
            callback_data=f"msel|{qid}|{i}",
        )])
    rows.append([
        InlineKeyboardButton(text="✅ Done", callback_data=f"msel|{qid}|done"),
        InlineKeyboardButton(text="❌ Skip", callback_data=f"msel|{qid}|skip"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _selected_skus(q: dict) -> list[str]:
    try:
        v = json.loads(q["chosen_sku"] or "[]")
        return list(v) if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


@router.callback_query(lambda c: c.data and c.data.startswith("msel|"))
async def cb_multiselect(query: CallbackQuery) -> None:
    if not is_allowed(query.from_user.id if query.from_user else None):
        await query.answer("Unauthorized", show_alert=True)
        return
    try:
        _, qid_str, tok = (query.data or "").split("|", 2)
        qid = int(qid_str)
    except Exception:
        await query.answer("malformed callback", show_alert=True)
        return

    conn = ensure_db()
    q = get_question(conn, qid)
    if q is None:
        await query.answer("question not found", show_alert=True)
        return
    cands = json.loads(q["candidates_json"] or "[]")
    selected = _selected_skus(q)

    if tok == "skip":
        answer_question(conn, qid, None, state="skipped")
        await query.answer("skipped")
        if query.message:
            await query.message.edit_text(
                f"*{q['generic_name']}* — skipped.", parse_mode="Markdown"
            )
        return

    if tok == "done":
        if not selected:
            answer_question(conn, qid, None, state="skipped")
            await query.answer("nothing picked — skipped")
            if query.message:
                await query.message.edit_text(
                    f"*{q['generic_name']}* — skipped (nothing picked).",
                    parse_mode="Markdown",
                )
            return
        answer_question(conn, qid, json.dumps(selected, ensure_ascii=False),
                        state="answered")
        names = [c["name"] for c in cands if c["sku"] in selected]
        await query.answer(f"✅ {len(selected)} picked")
        if query.message:
            await query.message.edit_text(
                f"*{q['generic_name']}* → ✅ " + ", ".join(names),
                parse_mode="Markdown",
            )
        remaining = open_questions_for_job(conn, q["job_id"])
        if not remaining and query.message:
            await query.message.answer(
                f"All picks recorded. /approve `{q['job_id']}` live to write the cart.",
                parse_mode="Markdown",
            )
        return

    # Toggle a candidate by index.
    try:
        idx = int(tok)
        sku = cands[idx]["sku"]
    except (ValueError, IndexError):
        await query.answer("bad index", show_alert=True)
        return
    if sku in selected:
        selected.remove(sku)
        toast = "removed"
    else:
        selected.append(sku)
        toast = "added"
    set_question_selection(conn, qid, json.dumps(selected, ensure_ascii=False))
    await query.answer(toast)
    if query.message:
        await query.message.edit_reply_markup(
            reply_markup=_msel_keyboard(qid, cands, set(selected))
        )


@router.message(Command("approve"))
async def cmd_approve(message: Message, command: CommandObject) -> None:
    if not await ensure_allowed(message):
        return
    # Args: "[job_id] [live]". A trailing "live" token writes the real cart
    # (still parks at /cart — never pays). Anything else is a dry-run.
    tokens = (command.args or "").split()
    live = bool(tokens) and tokens[-1].lower() == "live"
    if live:
        tokens = tokens[:-1]
    job_id = tokens[0] if tokens else None
    if not job_id:
        conn = ensure_db()
        job = latest_job(conn)
        if job is None:
            await message.reply("No job to approve.")
            return
        job_id = job.job_id

    cmd = [sys.executable, str(repo_root() / "src" / "tivtaam" / "runner.py"),
           "--job-id", job_id, "--approve"]
    if live:
        cmd.append("--live")
        await message.reply(
            f"⚠️ *LIVE* cart write for `{job_id}` — will add items to the real "
            f"cart, then park at /cart. *Payment stays manual.*",
            parse_mode="Markdown",
        )
    else:
        await message.reply(
            f"Spawning cart writer for `{job_id}` (dry-run)…", parse_mode="Markdown"
        )
    try:
        await asyncio.to_thread(
            lambda: subprocess.Popen(
                cmd,
                cwd=str(repo_root()),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
        )
    except Exception as e:
        await message.reply(f"Failed to spawn: {e}")


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not await ensure_allowed(message):
        return
    conn = ensure_db()
    job = latest_job(conn)
    if job is None:
        await message.reply("No jobs yet.")
        return
    txt = [f"Job `{job.job_id}` — state: *{job.state}*"]
    if job.plan_json:
        try:
            plan = json.loads(job.plan_json)
            if isinstance(plan, list):
                txt.append(f"{len(plan)} items in plan.")
                for item in plan[:8]:
                    txt.append(f"• {item.get('generic')} → {item.get('name') or '—'} ({item.get('sku') or '—'})")
                if len(plan) > 8:
                    txt.append(f"… and {len(plan) - 8} more")
        except Exception:
            pass
    await message.reply("\n".join(txt), parse_mode="Markdown")


@router.message()
async def free_text(message: Message) -> None:
    """Treat free text as a shopping list iff /shop was just sent."""
    if not await ensure_allowed(message):
        return
    uid = message.from_user.id if message.from_user else 0
    if uid not in _awaiting_list_from:
        await message.reply("Send /shop first.")
        return
    _awaiting_list_from.discard(uid)

    items = [ln.strip() for ln in (message.text or "").splitlines() if ln.strip()]
    if not items:
        await message.reply("Empty list — nothing to do.")
        return

    await _queue_and_spawn(message, items)
