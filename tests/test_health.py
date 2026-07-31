"""Health snapshot — pure logic, no browser and no live service."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from tivtaam.bot.health import (
    Check,
    HealthReport,
    age_seconds,
    check_db,
    check_history,
    check_last_job,
    check_open_questions,
    format_report,
    human_duration,
    parse_iso,
)
from tivtaam.db.migrations import apply_schema
from tivtaam.util.timing import now_iso


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_schema(c)
    return c


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (47, "47s"),
        (131, "2m 11s"),
        (3600 * 2 + 660, "2h 11m"),
        (86400 * 3 + 3600 * 4, "3d 4h"),
        (-5, "0s"),
    ],
)
def test_human_duration(seconds: float, expected: str) -> None:
    assert human_duration(seconds) == expected


def test_parse_iso_roundtrips_now_iso() -> None:
    dt = parse_iso(now_iso())
    assert dt is not None and dt.tzinfo is not None


def test_parse_iso_tolerates_garbage() -> None:
    assert parse_iso(None) is None
    assert parse_iso("not a date") is None
    assert age_seconds("not a date") is None


def test_age_seconds_is_positive_for_the_past() -> None:
    past = _iso(datetime.now(UTC) - timedelta(hours=3))
    age = age_seconds(past)
    assert age is not None
    assert 3 * 3600 - 60 < age < 3 * 3600 + 60


def test_db_check_flags_empty_history(conn: sqlite3.Connection) -> None:
    check = check_db(conn)
    assert not check.ok
    assert "0 purchases" in check.value


def test_history_check_flags_never_synced(conn: sqlite3.Connection) -> None:
    check = check_history(conn)
    assert not check.ok
    assert "never run" in check.value


def test_history_check_ok_when_recent(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('last_history_sync_at', ?)",
            (_iso(datetime.now(UTC) - timedelta(hours=2)),),
        )
    check = check_history(conn)
    assert check.ok
    assert "2h" in check.value


def test_history_check_flags_stale(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('last_history_sync_at', ?)",
            (_iso(datetime.now(UTC) - timedelta(days=45)),),
        )
    assert not check_history(conn).ok


def test_last_job_check_handles_no_jobs(conn: sqlite3.Connection) -> None:
    check = check_last_job(conn)
    assert check.ok
    assert check.value == "none yet"


def _insert_job(conn: sqlite3.Connection, state: str, updated: datetime) -> None:
    with conn:
        conn.execute(
            "INSERT INTO jobs (job_id, source, state, raw_list_json, created_at, updated_at) "
            "VALUES ('abc123', 'telegram', ?, '[]', ?, ?)",
            (state, _iso(updated), _iso(updated)),
        )


def test_last_job_check_flags_stalled_runner(conn: sqlite3.Connection) -> None:
    # `running` well past run.timeout_minutes means the subprocess died without
    # writing a terminal state — the exact silent failure /health exists for.
    _insert_job(conn, "running", datetime.now(UTC) - timedelta(hours=6))
    check = check_last_job(conn)
    assert not check.ok
    assert "stalled" in check.value


def test_last_job_check_ok_when_running_briefly(conn: sqlite3.Connection) -> None:
    _insert_job(conn, "running", datetime.now(UTC) - timedelta(minutes=2))
    assert check_last_job(conn).ok


def test_last_job_check_ok_when_parked(conn: sqlite3.Connection) -> None:
    _insert_job(conn, "parked", datetime.now(UTC) - timedelta(days=3))
    check = check_last_job(conn)
    assert check.ok
    assert "parked" in check.value


def test_open_questions_check(conn: sqlite3.Connection) -> None:
    assert check_open_questions(conn).value == "none"
    with conn:
        conn.execute(
            "INSERT INTO pending_questions "
            "(job_id, generic_name, candidates_json, state, created_at) "
            "VALUES ('abc123', 'גבינה בולגרית', '[]', 'open', ?)",
            (now_iso(),),
        )
    assert "1 awaiting" in check_open_questions(conn).value


def test_report_is_unhealthy_if_any_check_fails() -> None:
    assert HealthReport([Check("a", "fine")]).healthy
    assert not HealthReport([Check("a", "fine"), Check("b", "bad", ok=False)]).healthy


def test_format_report_marks_failures() -> None:
    text = format_report(HealthReport([Check("Disk", "0.1 GB free", ok=False)]))
    assert "needs attention" in text
    assert "⚠️ Disk: 0.1 GB free" in text


def test_format_report_ok_header() -> None:
    text = format_report(HealthReport([Check("Uptime", "3d 4h")]))
    assert "*Health: OK*" in text
    assert "✅ Uptime: 3d 4h" in text
