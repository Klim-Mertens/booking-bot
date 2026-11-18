"""Запуск обоих ботов (Telegram и MAX) одной командой, в одном окне терминала.

Если для мессенджера нет токена в .env, он просто пропускается. Если один бот
упал (например, нет связи с MAX), второй продолжает работать.
"""
from __future__ import annotations

import asyncio
import logging
import os

import run_max
import run_telegram

log = logging.getLogger("booking")


async def guarded(name: str, coro) -> None:
    try:
        await coro
    except (Exception, SystemExit) as e:  # noqa: BLE001 — один бот не должен ронять другой
        log.error("%s остановлен: %s", name, e)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    tasks = []
    if os.environ.get("TG_TOKEN"):
        tasks.append(guarded("Telegram-бот", run_telegram.main()))
    if os.environ.get("MAX_TOKEN"):
        tasks.append(guarded("MAX-бот", run_max.main()))
    if not tasks:
        raise SystemExit("В .env нет ни TG_TOKEN, ни MAX_TOKEN")
    log.info("Запускаю ботов: %d. Остановить: Ctrl + C", len(tasks))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
