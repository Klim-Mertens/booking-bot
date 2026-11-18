"""Запуск MAX-бота. Переменные окружения: MAX_TOKEN, MAX_ADMIN_IDS, PII_ENC_KEY, PII_HMAC_KEY,
CONFIG_PATH, DB_PATH. База общая с Telegram-ботом — можно запускать оба процесса."""
from __future__ import annotations

import asyncio
import base64
import logging
import os

from adapters import notify
from adapters.max.bot import MaxBookingBot
from adapters.max.client import MaxClient
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
    bot = MaxBookingBot(MaxClient(require_env("MAX_TOKEN")), svc, biz, admin_ids("MAX_ADMIN_IDS"),
                        extra_notify=notify.from_env(dict(os.environ), for_platform="max"))
    await asyncio.gather(bot.poll(), bot.reminders())


if __name__ == "__main__":
    asyncio.run(main())
