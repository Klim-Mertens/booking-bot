"""Запуск Telegram-бота.

Переменные окружения (см. .env.example):
  TG_TOKEN, PII_ENC_KEY, PII_HMAC_KEY, ADMIN_IDS, CONFIG_PATH, DB_PATH
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from adapters.telegram.bot import build, reminder_loop
from core.booking import BookingService
from core.config import admin_ids, load_config, require_env
from core.crypto import PiiCipher
from core.storage import Storage


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    biz, catalog = load_config(os.environ.get("CONFIG_PATH", "config.toml"))
    cipher = PiiCipher(require_env("PII_ENC_KEY").encode(),
                       base64.urlsafe_b64decode(require_env("PII_HMAC_KEY")))
    svc = BookingService(catalog, Storage(os.environ.get("DB_PATH", "bookings.db")), cipher, biz.tz)
    bot = Bot(require_env("TG_TOKEN"), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build(svc, biz, admin_ids())
    asyncio.create_task(reminder_loop(bot, svc, biz))
    # widget_query нужно перечислить явно, иначе Telegram его не пришлёт.
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "widget_query"])


if __name__ == "__main__":
    asyncio.run(main())
