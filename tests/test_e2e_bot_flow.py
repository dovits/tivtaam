"""End-to-end test of the Telegram list-entry flow, fully offline.

Drives the real path — `/shop` -> list message -> job row -> `runner.run_job`
-> inline-keyboard callbacks -> `runner.approve_job` — with only the two
external edges stubbed: Playwright (context/login/site search) and the
Telegram HTTP API. No network, no credentials, no live cart write.

Complements the unit tests, which cover the scoring functions in isolation:
this is the wiring between the bot, the job table and the cart writer.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from tivtaam.browser.search import SearchResult
from tivtaam.db.models import Product, Purchase
from tivtaam.db.repo import insert_purchase, upsert_product
from tivtaam.util.hebrew import normalize

ALLOWED_UID = 424242

# Mirrors what `history-sync` writes: past orders expose no SKU, so the scraper
# synthesizes `name:<normalized name>`.
HISTORY = [
    ("חלב תנובה 3% בקרטון 1 ליטר", 10, 3),
    ("גבינה בולגרית 5% טרה 250 גרם", 6, 7),
    ("גבינה בולגרית 16% גד 200 גרם", 5, 10),
    ("דוריטוס אקסטרים 90 גרם", 4, 5),
    ("במבה אסם 80 גרם", 7, 4),
    ("ביסלי גריל 70 גרם", 3, 12),
    ("עוגיות שוקולד צ'יפס 200 גרם", 2, 20),
]

SEARCH_FIXTURES = {
    "קפה נמס": [
        SearchResult(sku="55501", name="קפה נמס עלית 200 גרם", brand="עלית",
                     size_text="200 גרם", price=32.9, image_url=None,
                     raw_url="?catalogProduct=55501"),
        SearchResult(sku="55502", name="קפה נמס נס קפה גולד 100 גרם", brand="נסטלה",
                     size_text="100 גרם", price=28.5, image_url=None,
                     raw_url="?catalogProduct=55502"),
    ],
}


def _iso(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------- aiogram fakes
class _FakeUser:
    def __init__(self, uid: int) -> None:
        self.id = uid


class FakeMessage:
    """Stands in for aiogram's Message — records what the bot said back."""

    def __init__(self, text: str, uid: int = ALLOWED_UID) -> None:
        self.text = text
        self.from_user = _FakeUser(uid)
        self.replies: list[str] = []
        self.answers: list[str] = []
        self.edits: list[str] = []
        self.markup_edits = 0

    async def reply(self, text: str, **kw) -> None:
        self.replies.append(text)

    async def answer(self, text: str, **kw) -> None:
        self.answers.append(text)

    async def edit_text(self, text: str, **kw) -> None:
        self.edits.append(text)

    async def edit_reply_markup(self, **kw) -> None:
        self.markup_edits += 1


class FakeCommand:
    def __init__(self, args: str | None = None) -> None:
        self.args = args


class FakeQuery:
    """Stands in for aiogram's CallbackQuery (an inline-keyboard tap)."""

    def __init__(self, data: str, uid: int = ALLOWED_UID) -> None:
        self.data = data
        self.from_user = _FakeUser(uid)
        self.message = FakeMessage("")
        self.toasts: list[str] = []

    async def answer(self, text: str = "", **kw) -> None:
        self.toasts.append(text)


# -------------------------------------------------------------------- fixtures
@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A wired-up bot+runner with a temp DB, seeded history and stubbed edges."""
    import tivtaam.bot.handlers as handlers
    import tivtaam.config as config
    import tivtaam.db.migrations as migrations
    import tivtaam.resolver.pipeline as pipeline
    import tivtaam.runner as runner

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "0000:TEST")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", str(ALLOWED_UID))
    config.load_config.cache_clear()

    db = tmp_path / "e2e.db"
    monkeypatch.setattr(migrations, "db_path", lambda: db)

    # External edge 1: Playwright. Nothing here opens a browser.
    class _FakeCtx:
        def __enter__(self): return object()
        def __exit__(self, *a): return False

    monkeypatch.setattr(runner, "open_context", lambda *a, **k: _FakeCtx())
    monkeypatch.setattr(runner, "open_page", lambda ctx: object())
    monkeypatch.setattr(runner, "ensure_logged_in", lambda page: None)
    monkeypatch.setattr(
        pipeline, "search_with_cache",
        lambda conn, page, q, limit=10: SEARCH_FIXTURES.get(q, []),
    )

    # External edge 2: Telegram HTTP + the runner subprocess spawn.
    sent: list[dict] = []
    monkeypatch.setattr(
        runner, "_telegram_send",
        lambda token, chat, text, reply_markup=None:
            sent.append({"text": text, "reply_markup": reply_markup}),
    )
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        handlers.subprocess, "Popen",
        lambda cmd, **kw: spawned.append(list(cmd)),
    )
    handlers._awaiting_list_from.clear()

    conn = migrations.ensure_db()
    for name, n_orders, last in HISTORY:
        sku = f"name:{normalize(name)}"
        upsert_product(conn, Product(
            sku=sku, name=name, name_norm=normalize(name),
            last_seen_at=_iso(last), is_available=1,
        ))
        for i in range(n_orders):
            insert_purchase(conn, Purchase(
                order_id=f"{sku[-8:]}-{i}", sku=sku, qty=1.0,
                unit_price=10.0, purchased_at=_iso(last + i * 7),
            ))

    yield type("Env", (), {
        "conn": conn, "handlers": handlers, "runner": runner,
        "sent": sent, "spawned": spawned,
    })
    conn.close()
    config.load_config.cache_clear()


@pytest.fixture()
def job(env):
    """A queued job created the way the user creates one: /shop then a list."""
    asyncio.run(env.handlers.cmd_shop(FakeMessage("/shop")))
    msg = FakeMessage("\n".join(["חלב", "גבינה בולגרית", "חטיפים", "קפה נמס"]))
    asyncio.run(env.handlers.free_text(msg))
    cmd = env.spawned[0]
    return cmd[cmd.index("--job-id") + 1]


def _questions(env, job_id):
    return env.conn.execute(
        "SELECT * FROM pending_questions WHERE job_id = ? ORDER BY id", (job_id,)
    ).fetchall()


def _plan(env, job_id):
    row = env.conn.execute(
        "SELECT plan_json FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    return json.loads(row["plan_json"] or "[]")


# ------------------------------------------------------------------ list entry
def test_shop_then_list_queues_a_job(env, job):
    row = env.conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job,)).fetchone()
    assert row["state"] == "queued"
    assert json.loads(row["raw_list_json"]) == ["חלב", "גבינה בולגרית", "חטיפים", "קפה נמס"]
    assert "--job-id" in env.spawned[0]
    assert ALLOWED_UID not in env.handlers._awaiting_list_from


def test_shop_with_an_inline_list_skips_the_prompt(env):
    msg = FakeMessage("/shop\nחלב\nלחם")
    asyncio.run(env.handlers.cmd_shop(msg))
    assert any("Queued job" in r for r in msg.replies)
    row = env.conn.execute("SELECT raw_list_json FROM jobs").fetchone()
    assert json.loads(row["raw_list_json"]) == ["חלב", "לחם"]


def test_free_text_without_shop_is_not_a_list(env):
    msg = FakeMessage("חלב\nלחם")
    asyncio.run(env.handlers.free_text(msg))
    assert any("Send /shop first" in r for r in msg.replies)
    assert env.conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 0


def test_empty_list_message_creates_no_job(env):
    asyncio.run(env.handlers.cmd_shop(FakeMessage("/shop")))
    msg = FakeMessage("   \n  \n")
    asyncio.run(env.handlers.free_text(msg))
    assert any("Empty list" in r for r in msg.replies)
    assert env.conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 0


def test_only_the_owner_can_shop(env):
    msg = FakeMessage("/shop", uid=999)
    asyncio.run(env.handlers.cmd_shop(msg))
    assert any("Unauthorized" in r for r in msg.replies)
    assert env.conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 0


# --------------------------------------------------------------------- resolve
def test_run_job_resolves_the_whole_list(env, job):
    assert env.runner.run_job(job) == 0
    row = env.conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job,)).fetchone()
    assert row["state"] == "awaiting_user"

    plan = {e["generic"]: e for e in _plan(env, job)}
    assert len(plan) == 4

    # Bought 10× and recently -> auto-picked, no question.
    milk = plan["חלב"]
    assert milk["source"] == "history" and not milk["needs_user_confirmation"]
    assert "תנובה 3%" in milk["name"]

    # A configured category defers to a multi-pick and needs no browser.
    assert plan["חטיפים"]["sku"] is None
    assert plan["חטיפים"]["needs_user_confirmation"]

    # Not in history -> resolved off site search.
    assert plan["קפה נמס"]["sku"] in {"55501", "55502"}


def test_every_ambiguous_item_gets_a_question(env, job):
    env.runner.run_job(job)
    plan = _plan(env, job)
    qs = _questions(env, job)
    assert len(qs) == sum(1 for e in plan if e["needs_user_confirmation"])
    assert any("Plan ready" in s["text"] for s in env.sent)


def test_category_question_offers_past_snacks_and_excludes_cookies(env, job):
    env.runner.run_job(job)
    q = next(q for q in _questions(env, job) if q["generic_name"] == "חטיפים")
    names = [c["name"] for c in json.loads(q["candidates_json"])]
    assert {"במבה אסם 80 גרם", "דוריטוס אקסטרים 90 גרם", "ביסלי גריל 70 גרם"} <= set(names)
    assert not any("עוגיות" in n for n in names)


def test_callback_data_fits_telegrams_64_byte_limit(env, job):
    """Synthetic `name:` SKUs blow the limit — keyboards must carry indexes."""
    env.runner.run_job(job)
    for s in env.sent:
        if not s["reply_markup"]:
            continue
        for row in s["reply_markup"]["inline_keyboard"]:
            for btn in row:
                assert len(btn["callback_data"].encode()) <= 64, btn


# ----------------------------------------------------------- answering picks
def test_multiselect_toggles_and_done_keeps_only_selected(env, job):
    env.runner.run_job(job)
    q = next(q for q in _questions(env, job) if q["generic_name"] == "חטיפים")
    cands = json.loads(q["candidates_json"])

    taps = [FakeQuery(f"msel|{q['id']}|{tok}") for tok in (0, 1, 1, 2)]
    for t in taps:
        asyncio.run(env.handlers.cb_multiselect(t))
    assert [t.toasts for t in taps] == [["added"], ["added"], ["removed"], ["added"]]

    asyncio.run(env.handlers.cb_multiselect(FakeQuery(f"msel|{q['id']}|done")))
    row = env.conn.execute(
        "SELECT * FROM pending_questions WHERE id = ?", (q["id"],)
    ).fetchone()
    assert row["state"] == "answered"
    assert json.loads(row["chosen_sku"]) == [cands[0]["sku"], cands[2]["sku"]]


def test_multiselect_done_with_nothing_picked_is_a_skip(env, job):
    env.runner.run_job(job)
    q = next(q for q in _questions(env, job) if q["generic_name"] == "חטיפים")
    asyncio.run(env.handlers.cb_multiselect(FakeQuery(f"msel|{q['id']}|done")))
    row = env.conn.execute(
        "SELECT state FROM pending_questions WHERE id = ?", (q["id"],)
    ).fetchone()
    assert row["state"] == "skipped"


def test_single_pick_is_recorded_and_learned_as_an_alias(env, job):
    env.runner.run_job(job)
    q = next(q for q in _questions(env, job) if q["generic_name"] != "חטיפים")
    chosen = json.loads(q["candidates_json"])[0]
    asyncio.run(env.handlers.cb_answer(FakeQuery(f"ans|{q['id']}|{chosen['sku']}")))

    row = env.conn.execute(
        "SELECT * FROM pending_questions WHERE id = ?", (q["id"],)
    ).fetchone()
    assert row["state"] == "answered" and row["chosen_sku"] == chosen["sku"]
    alias = env.conn.execute(
        "SELECT * FROM generic_aliases WHERE generic_name = ?", (q["generic_name"],)
    ).fetchone()
    assert alias["preferred_sku"] == chosen["sku"]


def test_a_learned_alias_removes_the_question_next_time(env, job):
    from tivtaam.bot.ipc import new_job

    env.runner.run_job(job)
    q = next(q for q in _questions(env, job) if q["generic_name"] == "קפה נמס")
    chosen = json.loads(q["candidates_json"])[0]
    asyncio.run(env.handlers.cb_answer(FakeQuery(f"ans|{q['id']}|{chosen['sku']}")))

    job2 = new_job(env.conn, source="telegram", items=["קפה נמס"])
    env.runner.run_job(job2)
    assert _questions(env, job2) == []
    assert _plan(env, job2)[0]["source"] == "alias"


def test_a_stranger_cannot_answer_a_question(env, job):
    env.runner.run_job(job)
    q = _questions(env, job)[0]
    tap = FakeQuery(f"msel|{q['id']}|0", uid=999)
    asyncio.run(env.handlers.cb_multiselect(tap))
    assert tap.toasts == ["Unauthorized"]
    row = env.conn.execute(
        "SELECT * FROM pending_questions WHERE id = ?", (q["id"],)
    ).fetchone()
    assert row["chosen_sku"] is None


# ----------------------------------------------------------------- cart write
def test_approve_expands_multipicks_and_applies_answers(env, job):
    env.runner.run_job(job)
    cat_q = next(q for q in _questions(env, job) if q["generic_name"] == "חטיפים")
    for tok in (0, 2, "done"):
        asyncio.run(env.handlers.cb_multiselect(FakeQuery(f"msel|{cat_q['id']}|{tok}")))
    coffee_q = next(q for q in _questions(env, job) if q["generic_name"] == "קפה נמס")
    pick = json.loads(coffee_q["candidates_json"])[0]
    asyncio.run(env.handlers.cb_answer(FakeQuery(f"ans|{coffee_q['id']}|{pick['sku']}")))

    assert env.runner.approve_job(job, dry_run=True) == 0
    row = env.conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job,)).fetchone()
    assert row["state"] == "approved"

    final = json.loads(row["plan_json"])
    snacks = [e for e in final if e["generic"] == "חטיפים"]
    assert len(snacks) == 2 and all(e["sku"] for e in snacks)
    coffee = next(e for e in final if e["generic"] == "קפה נמס")
    assert coffee["sku"] == pick["sku"] and not coffee["needs_user_confirmation"]


def test_a_skipped_item_never_reaches_the_cart(env, job, monkeypatch):
    import tivtaam.browser.cart as cart

    env.runner.run_job(job)
    for q in _questions(env, job):
        token = "skip" if q["generic_name"] == "חטיפים" else None
        if token:
            asyncio.run(env.handlers.cb_multiselect(FakeQuery(f"msel|{q['id']}|skip")))
        else:
            asyncio.run(env.handlers.cb_answer(FakeQuery(f"ans|{q['id']}|skip")))

    written: list[str] = []
    real = cart.execute_plan

    def spy(page, lines, dry_run=True, on_item=None):
        written.extend(line.name for line in lines)
        return real(page, lines, dry_run=dry_run, on_item=on_item)

    monkeypatch.setattr(cart, "execute_plan", spy)
    env.runner.approve_job(job, dry_run=True)

    assert not any(
        s in w for w in written for s in ("במבה", "ביסלי", "דוריטוס", "קפה נמס")
    ), written
    # The auto-resolved items are untouched by the skips.
    assert any("תנובה 3%" in w for w in written), written


def test_approve_dry_run_never_opens_a_browser(env, job, monkeypatch):
    """`execute_plan(page=None)` is what keeps a dry run off the live site."""
    import tivtaam.browser.cart as cart

    env.runner.run_job(job)

    def explode(*a, **k):
        raise AssertionError("dry run must not open a browser")

    monkeypatch.setattr(env.runner, "open_context", explode)
    seen_pages: list[object] = []
    real = cart.execute_plan
    monkeypatch.setattr(
        cart, "execute_plan",
        lambda page, lines, dry_run=True, on_item=None: (
            seen_pages.append(page), real(page, lines, dry_run=dry_run, on_item=on_item)
        )[1],
    )
    assert env.runner.approve_job(job, dry_run=True) == 0
    assert seen_pages == [None]


def test_approve_command_requires_the_live_keyword_to_write(env, job):
    asyncio.run(env.handlers.cmd_approve(FakeMessage("/approve"), FakeCommand(job)))
    dry = env.spawned[-1]
    assert "--approve" in dry and "--live" not in dry

    msg = FakeMessage("/approve")
    asyncio.run(env.handlers.cmd_approve(msg, FakeCommand(f"{job} live")))
    live = env.spawned[-1]
    assert "--live" in live
    assert any("LIVE" in r for r in msg.replies), msg.replies


def test_status_reports_the_latest_job(env, job):
    env.runner.run_job(job)
    msg = FakeMessage("/status")
    asyncio.run(env.handlers.cmd_status(msg))
    assert any(job in r and "awaiting_user" in r for r in msg.replies)
