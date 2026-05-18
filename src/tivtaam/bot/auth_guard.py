"""Restrict the bot to a single configured user id."""

from __future__ import annotations

from aiogram.types import Message

from tivtaam.config import load_config


def is_allowed(user_id: int | None) -> bool:
    if user_id is None:
        return False
    cfg = load_config()
    expected = cfg.secrets.telegram_allowed_user_id
    return expected != 0 and user_id == expected


async def ensure_allowed(message: Message) -> bool:
    uid = message.from_user.id if message.from_user else None
    if not is_allowed(uid):
        await message.reply("Unauthorized. This bot only talks to its owner.")
        return False
    return True
