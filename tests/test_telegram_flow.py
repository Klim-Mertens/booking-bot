"""Сквозной тест Telegram-сценария без сети: подменяем HTTP-сессию aiogram
и проверяем, какие методы API бот вызывает в ответ на апдейты."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import Update
from cryptography.fernet import Fernet

from adapters.telegram.bot import build
from core.booking import BookingService
from core.config import load_config
from core.crypto import PiiCipher
from core.storage import Storage

USER, ADMIN = 111, 999


class FakeSession(BaseSession):
    """Записывает все вызовы API и возвращает правдоподобные ответы."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.msg_id = 100

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        self.calls.append(method)
        name = method.__api_method__
        if name in ("sendMessage", "sendWidget", "editMessageText"):
            self.msg_id += 1
            raw = {"message_id": self.msg_id, "date": 0, "chat": {"id": USER, "type": "private"}, "text": "x"}
            return method.__returning__.model_validate(raw, context={"bot": bot}) if name != "editMessageText" else True
        return True

    async def close(self) -> None: ...

    async def stream_content(self, *a, **k):  # pragma: no cover
        yield b""

    def names(self) -> list[str]:
        return [c.__api_method__ for c in self.calls]


@pytest.fixture
def env(tmp_path):
    biz, catalog = load_config("config.example.toml")
    svc = BookingService(catalog, Storage(str(tmp_path / "b.db")), PiiCipher(Fernet.generate_key(), b"k" * 32), biz.tz)
    session = FakeSession()
    bot = Bot("42:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    dp = build(svc, biz, frozenset({ADMIN}))
    return svc, biz, session, bot, dp


_uid = iter(range(1, 10_000))


def msg(text=None, contact=None, user=USER):
    m = {"message_id": next(_uid), "date": 0, "chat": {"id": user, "type": "private"},
         "from": {"id": user, "is_bot": False, "first_name": "Ivan"}}
    if text is not None:
        m["text"] = text
    if contact:
        m["contact"] = contact
    return {"update_id": next(_uid), "message": m}


def cb(data, user=USER):
    return {"update_id": next(_uid), "callback_query": {
        "id": str(next(_uid)), "chat_instance": "1", "data": data,
        "from": {"id": user, "is_bot": False, "first_name": "Ivan"},
        "message": {"message_id": 5, "date": 0, "chat": {"id": user, "type": "private"}, "text": "x"}}}


def widget(data, user=USER):
    return {"update_id": next(_uid), "widget_query": {
        "id": str(next(_uid)), "from": {"id": user, "is_bot": False, "first_name": "Ivan"},
        "chat": {"id": user, "type": "private"}, "message_id": 101, "chat_instance": "1", "data": data}}


async def feed(bot, dp, raw):
    await dp.feed_update(bot, Update.model_validate(raw, context={"bot": bot}))


def first_free(svc, biz):
    now = datetime.now(biz.tz)
    for i in range(biz.days_ahead):
        slots = svc.available_slots("haircut", "anton", (now + timedelta(days=i)).date(), now)
        if slots:
            return i, slots[0]
    raise AssertionError("нет слотов")


def test_full_widget_booking_flow(env):
    svc, biz, session, bot, dp = env

    async def scenario():
        await feed(bot, dp, msg("📅 Записаться"))
        await feed(bot, dp, cb("svc:haircut"))
        await feed(bot, dp, cb("mst:anton"))
        assert "sendWidget" in session.names()
        state = json.loads(next(c for c in session.calls if c.__api_method__ == "sendWidget").chat_data)
        assert len(json.dumps(state, ensure_ascii=False).encode()) <= 1024

        # Подделанный запрос виджета отклоняется
        await feed(bot, dp, widget("s:0:7"))
        ans = [c for c in session.calls if c.__api_method__ == "answerWidgetQuery"][-1]
        assert "err" in json.loads(ans.data)

        idx, slot = first_free(svc, biz)
        await feed(bot, dp, widget(f"s:{idx}:{slot.hour * 60 + slot.minute}"))
        ans = [c for c in session.calls if c.__api_method__ == "answerWidgetQuery"][-1]
        assert json.loads(ans.data) == {"ok": 1}

        await feed(bot, dp, cb("consent"))
        await feed(bot, dp, msg("Иван"))
        # Чужой контакт не принимается
        await feed(bot, dp, msg(contact={"phone_number": "+79990000000", "first_name": "X", "user_id": 555}))
        assert any("свой номер" in getattr(c, "text", "") for c in session.calls[-1:])
        await feed(bot, dp, msg(contact={"phone_number": "+79991234567", "first_name": "Ivan", "user_id": USER}))
        await feed(bot, dp, cb("confirm"))

        mine = svc.client_bookings(f"tg:{USER}", datetime.now(biz.tz))
        assert len(mine) == 1 and mine[0].start_at == slot
        # Админ получил уведомление
        assert any(getattr(c, "chat_id", None) == ADMIN for c in session.calls)
        # Чужой пользователь не может отменить запись
        await feed(bot, dp, cb(f"cnl:{mine[0].id}", user=777))
        assert svc.client_bookings(f"tg:{USER}", datetime.now(biz.tz))
        # И не может посмотреть телефон через админскую кнопку
        before = len(session.calls)
        await feed(bot, dp, cb(f"aph:{mine[0].id}", user=777))
        assert all("+7999" not in str(getattr(c, "text", "")) for c in session.calls[before:])

    asyncio.run(scenario())


def test_widget_request_without_flow_is_rejected(env):
    svc, biz, session, bot, dp = env
    asyncio.run(feed(bot, dp, widget("s:0:600")))
    ans = [c for c in session.calls if c.__api_method__ == "answerWidgetQuery"][-1]
    assert "err" in json.loads(ans.data)


def test_booking_ui_uses_new_bot_api_features(env):
    svc, biz, session, bot, dp = env

    async def scenario():
        await feed(bot, dp, msg("📅 Записаться"))
        await feed(bot, dp, cb("svc:haircut"))
        await feed(bot, dp, cb("mst:anton"))
        idx, slot = first_free(svc, biz)
        await feed(bot, dp, widget(f"s:{idx}:{slot.hour * 60 + slot.minute}"))
        consent = [c for c in session.calls if "Вы выбрали" in str(getattr(c, "text", ""))][-1]
        assert f'<tg-time unix="{int(slot.timestamp())}">' in consent.text        # date_time
        styles = [b.style for row in consent.reply_markup.inline_keyboard for b in row]
        assert styles == ["success", "danger"]                                     # цветные кнопки
        await feed(bot, dp, cb("consent"))
        await feed(bot, dp, msg("Иван"))
        await feed(bot, dp, msg("перезвоните"))                                    # не телефон
        assert "Не похоже на номер" in session.calls[-1].text
        await feed(bot, dp, msg("+7 999 123-45-67"))
        await feed(bot, dp, cb("confirm"))
        done = [c for c in session.calls if "Вы записаны" in str(getattr(c, "text", ""))][-1]
        assert done.message_effect_id == "5046509860389126442"                      # конфетти

    asyncio.run(scenario())


def test_busy_days_are_disabled_buttons():
    from adapters.telegram.bot import ikb
    kb = ikb([[("Пн 1 — занято", None)], [("Вт 2", "day:1", "primary")]])
    raw = json.loads(kb.model_dump_json(exclude_none=True))
    assert raw["inline_keyboard"][0][0] == {"text": "Пн 1 — занято", "disabled": {}}
    assert raw["inline_keyboard"][1][0]["style"] == "primary"


def test_widget_answered_even_on_internal_error(env, monkeypatch):
    svc, biz, session, bot, dp = env

    def boom(*a, **k):
        raise RuntimeError("db down")

    async def scenario():
        await feed(bot, dp, msg("📅 Записаться"))
        await feed(bot, dp, cb("svc:haircut"))
        await feed(bot, dp, cb("mst:anton"))
        monkeypatch.setattr(svc, "available_slots", boom)
        try:
            await feed(bot, dp, widget("s:0:600"))
        except RuntimeError:
            pass
        assert session.names()[-1] == "answerWidgetQuery"

    asyncio.run(scenario())
