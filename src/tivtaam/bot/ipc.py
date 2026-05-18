"""Bot↔runner IPC via job files in data/jobs/ and the `jobs` table.

The bot writes a job to the table (state=queued) and spawns a `runner.py`
subprocess. The runner reads the job, runs the resolve pipeline, and writes the
plan back as `plan_json`, updating state. Bot polls the row OR receives a push
from the runner via the Telegram Bot API directly (same token).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Literal

from tivtaam.util.timing import now_iso

JobState = Literal["queued", "running", "awaiting_user", "approved", "parked", "failed"]


@dataclass
class Job:
    job_id: str
    source: str
    state: JobState
    raw_list_json: str
    plan_json: str | None
    created_at: str
    updated_at: str


def new_job(conn: sqlite3.Connection, source: str, items: list[str]) -> str:
    job_id = uuid.uuid4().hex[:12]
    ts = now_iso()
    with conn:
        conn.execute(
            "INSERT INTO jobs (job_id, source, state, raw_list_json, created_at, updated_at) "
            "VALUES (?, ?, 'queued', ?, ?, ?)",
            (job_id, source, json.dumps(items, ensure_ascii=False), ts, ts),
        )
    return job_id


def get_job(conn: sqlite3.Connection, job_id: str) -> Job | None:
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return Job(**dict(row)) if row else None


def update_state(
    conn: sqlite3.Connection,
    job_id: str,
    state: JobState,
    plan_json: str | None = None,
) -> None:
    with conn:
        if plan_json is not None:
            conn.execute(
                "UPDATE jobs SET state=?, plan_json=?, updated_at=? WHERE job_id=?",
                (state, plan_json, now_iso(), job_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET state=?, updated_at=? WHERE job_id=?",
                (state, now_iso(), job_id),
            )


def latest_job(conn: sqlite3.Connection) -> Job | None:
    row = conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return Job(**dict(row)) if row else None
