"""Сквозной тест MAX-сценария с подменённым клиентом API."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from adapters.max.bot import MaxBookingBot
from core.booking import BookingService
from core.config import load_config
from core.crypto import PiiCipher
from core.storage import Storage

USER, ADMIN, STRANGER = 10, 99, 77


class FakeClient:
    def __init__(self):
        self.sent, self.answers, self.edited = [], [], []

    async def send(self, user_id, text, buttons=None):
        self.sent.append((user_id, text, buttons))

    async def answer_callback(self, callback_id, notification=None, message=None):
        self.answers.append(notification)
        if message:      # экран, подменённый на месте, — тоже «отправлен» для проверок
            kb = [a["payload"]["buttons"] for a in message["attachments"] if a["type"] == "inline_keyboard"]
            self.edited.append(message)
            self.sent.append((None, message["text"], kb[0] if kb else None))

    @staticmethod
    def message_body(text, buttons=None):
        from adapters.max.client import MaxClient
        return MaxClient.message_body(text, buttons)


@pytest.fixture
def env(tmp_path):
    biz, catalog = load_config("config.example.toml")
    svc = BookingService(catalog, Storage(str(tmp_path / "m.db")), PiiCipher(Fernet.generate_key(), b"k" * 32), biz.tz)
    api = FakeClient()
    return svc, biz, api, MaxBookingBot(api, svc, biz, frozenset({ADMIN}))


def cb(payload, user=USER):
    return {"update_type": "message_callback",
            "callback": {"callback_id": "c", "payload": payload, "timestamp": 0,
                         "user": {"user_id": user, "first_name": "I"}}}


def text(t, user=USER, attachments=None):
    return {"update_type": "message_created",
            "message": {"sender": {"user_id": user, "first_name": "I", "is_bot": False},
                        "recipient": {"user_id": 1, "chat_type": "dialog"}, "timestamp": 0,
                        "body": {"mid": "m", "seq": 1, "text": t, "attachments": attachments}}}


def contact(owner, phone):
    return [{"type": "contact", "payload": {"vcf_info": f"BEGIN:VCARD\nTEL;TYPE=CELL:{phone}\nEND:VCARD",
                                            "max_info": {"user_id": owner, "first_name": "X"}}}]


def test_full_max_flow(env):
    svc, biz, api, bot = env

    async def run():
        h = bot.handle
        await h(cb("book")); await h(cb("svc:haircut")); await h(cb("mst:anton"))
        day_btn = api.sent[-1][2][0][0]["payload"]
        await h(cb(day_btn))
        tm_btn = api.sent[-1][2][0][0]["payload"]
        await h(cb("tm:0:7"))                      # подделка
        assert api.answers[-1] == "Время недоступно, выберите другое"
        await h(cb(tm_btn)); await h(cb("consent")); await h(text("Иван"))
        await h(text(None, attachments=contact(STRANGER, "+79990000000")))   # чужой контакт
        assert "свой номер" in api.sent[-1][1]
        await h(text(None, attachments=contact(USER, "+7 999 123-45-67")))
        await h(cb("confirm"))
        mine = svc.client_bookings(f"max:{USER}", datetime.now(biz.tz))
        assert len(mine) == 1
        assert any(uid == ADMIN for uid, *_ in api.sent)
        await h(cb(f"aph:{mine[0].id}", user=STRANGER))       # не админ
        assert api.answers[-1] is None
        await h(cb(f"aph:{mine[0].id}", user=ADMIN))
        assert api.answers[-1] == "+79991234567"
        await h(cb(f"cnl:{mine[0].id}", user=STRANGER))        # IDOR
        assert svc.client_bookings(f"max:{USER}", datetime.now(biz.tz))

    asyncio.run(run())


def test_screens_edit_in_place_and_colors(env):
    svc, biz, api, bot = env

    async def run():
        h = bot.handle
        await h(cb("book"))
        sent_before = len([x for x in api.sent if x[0] is not None])
        await h(cb("svc:haircut")); await h(cb("mst:anton"))
        # шаги после «Записаться» меняют сообщение на месте, а не шлют новые
        assert len([x for x in api.sent if x[0] is not None]) == sent_before
        assert len(api.edited) == 2
        day_btn = api.sent[-1][2][0][0]["payload"]
        await h(cb(day_btn))
        rows = api.sent[-1][2]
        assert rows[-1][0]["payload"] == "mst:anton"              # кнопка «другой день»
        await h(cb(rows[0][0]["payload"]))
        consent = api.sent[-1][2]
        assert consent[0][0]["intent"] == "positive" and consent[0][1]["intent"] == "negative"

    asyncio.run(run())


def test_bad_phone_rejected_immediately(env):
    svc, biz, api, bot = env

    async def run():
        h = bot.handle
        await h(cb("book")); await h(cb("svc:haircut")); await h(cb("mst:anton"))
        await h(cb(api.sent[-1][2][0][0]["payload"])); await h(cb(api.sent[-1][2][0][0]["payload"]))
        await h(cb("consent")); await h(text("Иван")); await h(text("позвоните мне"))
        assert "Не похоже на номер" in api.sent[-1][1]
        assert bot.sessions[USER].step == "phone"

    asyncio.run(run())


def test_contacts_have_copy_buttons(env):
    svc, biz, api, bot = env
    asyncio.run(bot.handle(cb("contacts")))
    kinds = {b["type"] for row in api.sent[-1][2] for b in row}
    assert kinds == {"clipboard"}


def test_admin_notified_in_other_messenger(env):
    svc, biz, api, _ = env
    got = []

    async def extra(text):
        got.append(text)

    bot = MaxBookingBot(api, svc, biz, frozenset({ADMIN}), extra_notify=extra)
    asyncio.run(bot.notify_admins("🆕 Новая запись"))
    assert got == ["🆕 Новая запись"]


def test_id_command_shows_own_id(env):
    svc, biz, api, bot = env
    asyncio.run(bot.handle(text("/id", user=1234)))
    assert api.sent[-1][0] == 1234 and "1234" in api.sent[-1][1]
