"""Wipe Playwright persistent context (cookies/session) to force a fresh login next run."""

from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from tivtaam.config import load_config, repo_root  # noqa: E402


def main() -> None:
    cfg = load_config()
    target = (repo_root() / cfg.browser.user_data_dir).resolve()
    if target.exists():
        shutil.rmtree(target)
        print(f"Removed {target}")
    else:
        print(f"No persistent context at {target} (already clean).")


if __name__ == "__main__":
    main()
