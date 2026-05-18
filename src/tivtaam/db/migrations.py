from __future__ import annotations

import sqlite3
from pathlib import Path

from tivtaam.config import repo_root

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def db_path() -> Path:
    p = repo_root() / "data" / "tivtaam.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with conn:
        conn.executescript(sql)


def ensure_db() -> sqlite3.Connection:
    conn = connect()
    apply_schema(conn)
    return conn
