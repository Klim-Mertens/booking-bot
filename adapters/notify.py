"""Уведомления владельцу «в другой мессенджер».

Telegram-бот и MAX-бот — разные процессы с общей базой. Владельцу удобно видеть
все записи в одном месте, поэтому каждый процесс может продублировать уведомление
админам второй платформы, если в .env заданы её токен и ID админов.
Текст уведомлений — только служебный (без телефонов), разметка — HTML.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from core.config import admin_ids

log = logging.getLogger("booking.notify")
Notifier = Callable[[str], Awaitable[None]]


def telegram_notifier(token: str | None, admins: frozenset[int]) -> Notifier | None:
    """Для MAX-процесса: слать копию уведомлений админам в Telegram."""
    if not token or not admins:
        return None
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.exceptions import TelegramAPIError

    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    async def notify(text: str) -> None:
        for admin_id in admins:
            try:
                await bot.send_message(admin_id, text)
            except TelegramAPIError as e:
                log.warning("telegram admin notify failed: %s", e)
    return notify


def max_notifier(token: str | None, admins: frozenset[int]) -> Notifier | None:
    """Для Telegram-процесса: слать копию уведомлений админам в MAX."""
    if not token or not admins:
        return None
    from adapters.max.client import MaxApiError, MaxClient

    client = MaxClient(token)

    async def notify(text: str) -> None:
        for admin_id in admins:
            try:
                await client.send(admin_id, text)
            except (MaxApiError, OSError) as e:
                # Причина без токена и без текста уведомления. Частая: неверный MAX_ADMIN_IDS
                # или админ ещё не писал боту в MAX.
                log.warning("max admin notify failed: %s", e)
    return notify


def from_env(env: dict[str, str], *, for_platform: str) -> Notifier | None:
    if for_platform == "max":
        return telegram_notifier(env.get("TG_TOKEN"), admin_ids("ADMIN_IDS"))
    return max_notifier(env.get("MAX_TOKEN"), admin_ids("MAX_ADMIN_IDS"))
