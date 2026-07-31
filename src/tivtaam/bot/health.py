"""Health snapshot for the always-on bot.

The bot runs unattended as a Windows service (see `scripts/install_service.ps1`),
so the only way to notice that something rotted — expired cookies, a stale
history sync, a full disk, a runner that died mid-job — is to ask it. `/health`
in Telegram renders this snapshot.

Every check is best-effort: a probe that raises degrades to `None`/"unknown"
rather than taking the command down, because a health report that can crash is
worse than no health report.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tivtaam.config import load_config, repo_root
from tivtaam.db.migrations import ensure_db
from tivtaam.db.repo import get_meta
from tivtaam.util.logging import get_logger

log = get_logger(__name__)

# Epoch seconds at process import. NSSM restarts the process on crash, so this
# is really "time since the last (re)start", which is exactly what we want to
# see — a low uptime on a service installed weeks ago means it is crash-looping.
PROCESS_STARTED_AT = time.time()

# A cookie jar older than this is likely to hit an interactive login.
SESSION_STALE_HOURS = 24 * 14
# The site's purchase history barely moves week to week, but a sync that has
# not run in this long usually means history-sync is failing silently.
HISTORY_STALE_DAYS = 30
DISK_LOW_GB = 2.0


@dataclass
class Check:
    """One line of the report: a label, a rendered value, and a verdict."""

    label: str
    value: str
    ok: bool = True

    @property
    def icon(self) -> str:
        return "✅" if self.ok else "⚠️"


@dataclass
class HealthReport:
    checks: list[Check]

    @property
    def healthy(self) -> bool:
        return all(c.ok for c in self.checks)


def human_duration(seconds: float) -> str:
    """Compact duration: '3d 4h', '2h 11m', '47s'."""
    seconds = int(max(seconds, 0))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def parse_iso(value: str | None) -> datetime | None:
    """Parse the `now_iso()` format ('...Z'), tolerating anything else."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def age_seconds(value: str | None) -> float | None:
    dt = parse_iso(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (datetime.now(UTC) - dt).total_seconds()


def _cookie_jar() -> Path | None:
    """Newest cookie DB inside the Playwright persistent context, if any.

    Chromium keeps it at `<user_data_dir>/Default/Network/Cookies`, but the
    exact path has moved between versions — glob for it instead of hardcoding.
    """
    cfg = load_config()
    root = (repo_root() / cfg.browser.user_data_dir).resolve()
    if not root.is_dir():
        return None
    candidates = [p for p in root.glob("*/Network/Cookies") if p.is_file()]
    candidates += [p for p in root.glob("*/Cookies") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def check_uptime() -> Check:
    return Check("Uptime", human_duration(time.time() - PROCESS_STARTED_AT))


def check_session() -> Check:
    """How long since the browser profile's cookies were last written."""
    jar = _cookie_jar()
    if jar is None:
        return Check("Session", "no browser profile yet — first run will log in", ok=False)
    age = time.time() - jar.stat().st_mtime
    ok = age < SESSION_STALE_HOURS * 3600
    suffix = "" if ok else " (may need interactive login)"
    return Check("Session", f"touched {human_duration(age)} ago{suffix}", ok=ok)


def check_history(conn: sqlite3.Connection) -> Check:
    age = age_seconds(get_meta(conn, "last_history_sync_at"))
    if age is None:
        return Check("History sync", "never run — `python -m tivtaam history-sync`", ok=False)
    ok = age < HISTORY_STALE_DAYS * 86400
    return Check("History sync", f"{human_duration(age)} ago", ok=ok)


def check_db(conn: sqlite3.Connection) -> Check:
    products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    purchases = conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0]
    aliases = conn.execute("SELECT COUNT(*) FROM generic_aliases").fetchone()[0]
    return Check(
        "Database",
        f"{products} products, {purchases} purchases, {aliases} aliases",
        ok=purchases > 0,
    )


def check_last_job(conn: sqlite3.Connection) -> Check:
    row = conn.execute(
        "SELECT job_id, state, updated_at FROM jobs ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return Check("Last job", "none yet")
    age = age_seconds(row["updated_at"])
    when = f"{human_duration(age)} ago" if age is not None else "unknown age"
    # A job stuck in `running` for longer than the configured run timeout means
    # the runner subprocess died without writing a terminal state.
    timeout_s = load_config().run.timeout_minutes * 60
    stalled = row["state"] == "running" and age is not None and age > timeout_s
    state = f"{row['state']} (stalled — runner likely died)" if stalled else row["state"]
    return Check("Last job", f"`{row['job_id']}` — {state}, {when}", ok=not stalled)


def check_open_questions(conn: sqlite3.Connection) -> Check:
    n = conn.execute(
        "SELECT COUNT(*) FROM pending_questions WHERE state = 'open'"
    ).fetchone()[0]
    detail = "none" if n == 0 else f"{n} awaiting your pick"
    return Check("Open questions", detail)


def check_disk() -> Check:
    try:
        free_gb = shutil.disk_usage(repo_root()).free / 1024**3
    except OSError as e:
        return Check("Disk", f"unreadable ({e})", ok=False)
    return Check("Disk", f"{free_gb:.1f} GB free", ok=free_gb >= DISK_LOW_GB)


def check_chrome() -> Check:
    """The runner launches `channel="chrome"` — a real Chrome, not the bundled
    Chromium — so a missing/moved Chrome breaks every run at browser launch."""
    found = shutil.which("chrome") or shutil.which("google-chrome")
    if not found and sys.platform == "win32":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
            / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
            / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Google/Chrome/Application/chrome.exe",
        ]
        found = next((str(p) for p in candidates if p.is_file()), None)
    if found:
        return Check("Chrome", "found")
    return Check("Chrome", "not found — Playwright `channel=chrome` will fail", ok=False)


def last_error_line() -> str | None:
    """Most recent ERROR line from today's runner log, trimmed for Telegram."""
    log_file = repo_root() / "logs" / f"runner-{datetime.now().strftime('%Y-%m-%d')}.log"
    if not log_file.is_file():
        return None
    try:
        # Logs stay small (one day, one bot), so a full read beats a seek dance.
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    errors = [ln for ln in lines if " ERROR " in ln]
    if not errors:
        return None
    # Backticks would break the Markdown code span we wrap this in.
    return errors[-1].replace("`", "'")[:300]


def snapshot() -> HealthReport:
    checks = [check_uptime()]
    try:
        # ensure_db() rather than a bare connect: sqlite3 happily opens a
        # missing file, and every check would then fail with "no such table"
        # instead of reporting the empty-but-valid state honestly.
        conn = ensure_db()
        try:
            checks += [
                check_db(conn),
                check_history(conn),
                check_last_job(conn),
                check_open_questions(conn),
            ]
        finally:
            conn.close()
    except sqlite3.Error as e:
        checks.append(Check("Database", f"unreachable ({e})", ok=False))
    checks += [check_session(), check_chrome(), check_disk()]
    return HealthReport(checks)


def format_report(report: HealthReport) -> str:
    """Render for Telegram with `parse_mode="Markdown"`."""
    header = "*Health: OK*" if report.healthy else "*Health: needs attention*"
    lines = [header]
    lines += [f"{c.icon} {c.label}: {c.value}" for c in report.checks]
    err = last_error_line()
    if err:
        lines += ["", "Last error today:", f"`{err}`"]
    return "\n".join(lines)
