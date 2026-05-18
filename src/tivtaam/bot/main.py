"""Bot entry point (long-polling).

Run as: `python -m tivtaam.bot.main` (or via NSSM as a service).
Reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USER_ID` from `.env`.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher

from tivtaam.bot.handlers import router
from tivtaam.config import load_config
from tivtaam.util.logging import configure, get_logger

log = get_logger(__name__)


async def main() -> None:
    cfg = load_config()
    configure(level=cfg.secrets.log_level)
    if not cfg.secrets.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing from .env")
    if not cfg.secrets.telegram_allowed_user_id:
        raise SystemExit("TELEGRAM_ALLOWED_USER_ID missing from .env")
    bot = Bot(cfg.secrets.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    log.info("bot starting (allowed_user_id=%s)", cfg.secrets.telegram_allowed_user_id)
    try:
        await dp.start_polling(bot, polling_timeout=cfg.telegram.poll_interval_s)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
