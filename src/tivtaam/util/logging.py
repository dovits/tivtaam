from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

from tivtaam.config import repo_root

_CONFIGURED = False


def _console_handler() -> logging.Handler:
    """Rich when attached to an interactive TTY; otherwise a UTF-8-safe plain
    stream handler. Under the NSSM service, stderr is a redirected file with a
    cp1252 codepage — RichHandler crashes there on Hebrew, so we avoid it."""
    stream = sys.stderr
    is_tty = bool(getattr(stream, "isatty", lambda: False)())
    if is_tty:
        from rich.logging import RichHandler

        return RichHandler(rich_tracebacks=True, show_path=False, markup=False)
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass
    h = logging.StreamHandler(stream)
    h.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
    )
    return h


def configure(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    log_dir = repo_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_path = log_dir / f"runner-{datetime.now().strftime('%Y-%m-%d')}.log"
    file_handler = logging.FileHandler(file_path, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
    )
    logging.basicConfig(
        level=level.upper(),
        handlers=[_console_handler(), file_handler],
        format="%(message)s",
        datefmt="[%X]",
        force=True,
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def claude_audit_path() -> Path:
    log_dir = repo_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"claude-{datetime.now().strftime('%Y-%m-%d')}.jsonl"
