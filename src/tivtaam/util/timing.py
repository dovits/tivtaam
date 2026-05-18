from __future__ import annotations

import random
import time
from datetime import UTC

from tivtaam.config import load_config


def jitter_sleep() -> None:
    lo, hi = load_config().browser.navigation_jitter_ms
    time.sleep(random.uniform(lo, hi) / 1000.0)


def now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
