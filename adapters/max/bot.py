"""MAX-адаптер: тот же сценарий записи, что и в Telegram, но на inline-кнопках
(HTML-виджетов в MAX нет). Ядро (BookingService) общее — база тоже общая.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape
from typing import Any

from adapters.common import RateLimiter, booking_text, human_dt, offered_days, parse_widget_request
from core.booking import BookingError, BookingService, mark_reminder_sent, reminders_due
from core.config import Business

from .client import MaxApiError, MaxClient, callback_button as btn, contact_button

log = logging.getLogger("booking.max")
SESSION_TTL = 30 * 60          # незаконченная запись забывается через 30 минут
_TEL_RE = re.compile(r"^TEL[^:]*:(.+)$", re.MULTILINE)
_ID = r"[a-z0-9_]{1,16}"


def uid(user_id: int) -> str:
    return f"max:{user_id}"


@dataclass
class Session:
    step: str = "idle"                     # idle → choosing → consent → name → phone → confirm
    data: dict[str, Any] = field(default_factory=dict)
    touched: float = field(default_factory=time.monotonic)


class MaxBookingBot:
    def __init__(self, client: MaxClient, svc: BookingService, biz: Business, admins: frozenset[int]) -> None:
        self.api, self.svc, self.biz, self.admins = client, svc, biz, admins
        self.sessions: dict[int, Session] = {}
        self.limiter = RateLimiter()

    def now(self) -> datetime:
        return datetime.now(self.biz.tz)

    def session(self, user_id: int) -> Session:
        s = self.sessions.get(user_id)
        if s is None or time.monotonic() - s.touched > SESSION_TTL:
            s = self.sessions[user_id] = Session()
        s.touched = time.monotonic()
        return s

    # ------------------------------------------------------------ вход

    async def handle(self, update: dict[str, Any]) -> None:
        kind = update.get("update_type")
        if kind == "bot_started":
            await self.menu(int(update["user"]["user_id"]), greet=True)
        elif kind == "message_created":
            msg = update.get("message") or {}
            sender = msg.get("sender") or {}
            if sender.get("is_bot") or "user_id" not in sender:
                return
            user_id = int(sender["user_id"])
            if self.limiter.allow(uid(user_id)):
                await self.on_message(user_id, msg.get("body") or {})
        elif kind == "message_callback":
            cb = update.get("callback") or {}
            user_id = int(cb["user"]["user_id"])
            if not self.limiter.allow(uid(user_id)):
                await self.api.answer_callback(cb["callback_id"], "Слишком часто, подождите минуту")
                return
            note = await self.on_callback(user_id, str(cb.get("payload") or "")[:256])
            await self.api.answer_callback(cb["callback_id"], note)

    # ------------------------------------------------------------ меню

    async def menu(self, user_id: int, greet: bool = False) -> None:
        text = (f"Здравствуйте! Это бот записи в <b>{escape(self.biz.name)}</b>.\n"
                "Запишитесь за минуту, а мы напомним о визите." if greet else "Чем помочь?")
        await self.api.send(user_id, text, [[btn("📅 Записаться", "book")],
                                            [btn("🗂 Мои записи", "my"), btn("📍 Контакты", "contacts")]])

    async def on_message(self, user_id: int, body: dict[str, Any]) -> None:
        s = self.session(user_id)
        text = (body.get("text") or "").strip()
        if s.step == "name" and text:
            name = " ".join(text.split())[:64]
            if name.startswith("/"):
                return
            s.data["name"], s.step = name, "phone"
            await self.api.send(user_id, "Отправьте номер кнопкой или напишите его.",
                                [[contact_button("📱 Отправить номер")]])
        elif s.step == "phone":
            phone = self._own_contact_phone(user_id, body) or text
            if not phone:
                await self.api.send(user_id, "Отправьте, пожалуйста, свой номер.")
                return
            s.data["phone"], s.step = phone, "confirm"
            d = s.data
            svc_ = self.svc.catalog.services[d["service"]]
            await self.api.send(
                user_id,
                f"Проверьте данные:\n<b>{escape(svc_.title)}</b> · {svc_.price_rub} ₽\n"
                f"Мастер: {escape(self.svc.catalog.masters[d['master']].name)}\n"
                f"Когда: {escape(human_dt(datetime.fromisoformat(d['start'])))}\nИмя: {escape(d['name'])}",
                [[btn("✅ Записаться", "confirm"), btn("Отмена", "abort")]],
            )
        else:
            await self.menu(user_id, greet=s.step == "idle" and text.lower() in ("/start", "start", "старт"))

    @staticmethod
    def _own_contact_phone(user_id: int, body: dict[str, Any]) -> str | None:
        """Телефон из вложения-контакта, только если это контакт самого пользователя."""
        for att in body.get("attachments") or []:
            if att.get("type") != "contact":
                continue
            payload = att.get("payload") or {}
            owner = (payload.get("max_info") or {}).get("user_id")
            if owner is not None and int(owner) != user_id:
                return None   # чужая карточка контакта
            m = _TEL_RE.search(payload.get("vcf_info") or "")
            if m:
                return m.group(1).strip()
        return None

    # --------------------------------------------------------- кнопки

    async def on_callback(self, user_id: int, payload: str) -> str | None:
        s = self.session(user_id)
        svc, now = self.svc, self.now()

        if payload == "contacts":
            await self.api.send(user_id, f"<b>{escape(self.biz.name)}</b>\n{escape(self.biz.address)}\n"
                                         f"{escape(self.biz.phone)}")
        elif payload == "my":
            items = svc.client_bookings(uid(user_id), now)
            if not items:
                await self.api.send(user_id, "Активных записей нет.")
            for b in items:
                await self.api.send(user_id, escape(booking_text(svc, b)), [[btn("Отменить запись", f"cnl:{b.id}")]])
        elif payload == "book":
            self.sessions[user_id] = s = Session(step="choosing")
            await self.api.send(user_id, "Выберите услугу:",
                                [[btn(f"{x.title} · {x.price_rub} ₽", f"svc:{x.id}")] for x in svc.catalog.services.values()])
        elif m := re.fullmatch(rf"svc:({_ID})", payload):
            masters = svc.catalog.masters_for(m.group(1))
            if s.step != "choosing" or not masters:
                return "Начните запись заново"
            s.data["service"] = m.group(1)
            await self.api.send(user_id, "Выберите мастера:", [[btn(x.name, f"mst:{x.id}")] for x in masters])
        elif m := re.fullmatch(rf"mst:({_ID})", payload):
            master = svc.catalog.masters.get(m.group(1))
            if s.step != "choosing" or master is None or s.data.get("service") not in master.service_ids:
                return "Начните запись заново"
            days = offered_days(now.date(), self.biz.days_ahead)
            s.data.update(master=master.id, days=[d.isoformat() for d in days])
            rows = [[btn(human_dt(datetime.combine(d, datetime.min.time())).rsplit(",", 1)[0], f"day:{i}")]
                    for i, d in enumerate(days) if svc.available_slots(s.data["service"], master.id, d, now)]
            await self.api.send(user_id, "Выберите день:" if rows else "Свободного времени нет. Позвоните нам: "
                                + escape(self.biz.phone), rows or None)
        elif m := re.fullmatch(r"day:(\d{1,2})", payload):
            days = [date.fromisoformat(x) for x in s.data.get("days", [])]
            idx = int(m.group(1))
            if s.step != "choosing" or idx >= len(days):
                return "Начните запись заново"
            slots = svc.available_slots(s.data["service"], s.data["master"], days[idx], now)
            buttons = [btn(f"{t:%H:%M}", f"tm:{idx}:{t.hour * 60 + t.minute}") for t in slots]
            await self.api.send(user_id, "Выберите время:", [buttons[i:i + 4] for i in range(0, len(buttons), 4)])
        elif m := re.fullmatch(r"tm:(\d{1,2}):(\d{1,4})", payload):
            if s.step != "choosing":
                return "Начните запись заново"
            days = [date.fromisoformat(x) for x in s.data.get("days", [])]
            start = parse_widget_request(f"s:{m.group(1)}:{m.group(2)}", days, self.biz.tz)
            if start is None or start not in svc.available_slots(s.data["service"], s.data["master"], start.date(), now):
                return "Время недоступно, выберите другое"
            s.data["start"], s.step = start.isoformat(), "consent"
            await self.api.send(
                user_id,
                f"Вы выбрали: <b>{escape(human_dt(start))}</b>\n\nДля записи нужны имя и телефон. Нажимая "
                f"«Согласен», вы даёте согласие на обработку персональных данных по политике: "
                f"{escape(self.biz.privacy_url)}\nДанные хранятся зашифрованными и удаляются через "
                f"{self.biz.keep_days} дней после визита.",
                [[btn("✅ Согласен", "consent"), btn("Отмена", "abort")]],
            )
        elif payload == "consent" and s.step == "consent":
            s.data["consent"], s.step = True, "name"
            await self.api.send(user_id, "Как вас зовут?")
        elif payload == "confirm" and s.step == "confirm":
            d = s.data
            try:
                b = svc.book(service_id=d["service"], master_id=d["master"],
                             start_at=datetime.fromisoformat(d["start"]), client_user_id=uid(user_id),
                             client_name=d["name"], phone_raw=d["phone"],
                             consent_given=bool(d.get("consent")), now=now)
            except BookingError as e:
                if "телефона" in str(e):
                    s.step = "phone"
                    await self.api.send(user_id, "Неверный формат телефона. Напишите номер ещё раз.")
                else:
                    self.sessions.pop(user_id, None)
                    await self.api.send(user_id, f"{escape(str(e))}.", [[btn("📅 Записаться", "book")]])
                return None
            self.sessions.pop(user_id, None)     # телефон больше не держим в памяти
            await self.api.send(user_id, f"✅ Вы записаны!\n{escape(booking_text(svc, b))}\n\n"
                                         "Напомним за сутки и за 2 часа.")
            await self.notify_admins(f"🆕 Новая запись #{b.id} (MAX)\n{escape(booking_text(svc, b))}\n"
                                     f"Клиент: {escape(b.client_name)}",
                                     [[btn("📞 Телефон", f"aph:{b.id}"), btn("Отменить", f"acn:{b.id}")]])
        elif payload == "abort":
            self.sessions.pop(user_id, None)
            await self.api.send(user_id, "Запись отменена.")
        elif m := re.fullmatch(r"cnl:(\d{1,10})", payload):
            try:
                b = svc.cancel(int(m.group(1)), uid(user_id))   # проверка владельца внутри
            except BookingError as e:
                return str(e)
            await self.api.send(user_id, f"❌ Отменено: {escape(booking_text(svc, b))}")
            await self.notify_admins(f"❌ Клиент отменил запись #{b.id}\n{escape(booking_text(svc, b))}")
        elif m := re.fullmatch(r"aph:(\d{1,10})", payload):
            if user_id not in self.admins:
                return None
            try:
                phone = svc.admin_phone(int(m.group(1)))
            except BookingError as e:
                return str(e)
            log.info("admin revealed phone for booking %s", m.group(1))
            return phone    # всплывающее уведомление, номер не остаётся в истории чата
        elif m := re.fullmatch(r"acn:(\d{1,10})", payload):
            if user_id not in self.admins:
                return None
            try:
                b = svc.cancel(int(m.group(1)), uid(user_id), is_admin=True)
            except BookingError as e:
                return str(e)
            await self.api.send(user_id, f"❌ Отменено: #{b.id}")
            if b.client_user_id.startswith("max:"):
                await self.safe_send(int(b.client_user_id[4:]),
                                     f"К сожалению, ваша запись отменена:\n{escape(booking_text(svc, b))}\n"
                                     f"Свяжитесь с нами: {escape(self.biz.phone)}")
        return None

    async def notify_admins(self, text: str, buttons: list[list[dict]] | None = None) -> None:
        for admin_id in self.admins:
            await self.safe_send(admin_id, text, buttons)

    async def safe_send(self, user_id: int, text: str, buttons: list[list[dict]] | None = None) -> None:
        try:
            await self.api.send(user_id, text, buttons)
        except MaxApiError as e:
            log.warning("MAX send failed: %s", e.code)

    # --------------------------------------------------------- циклы

    async def poll(self) -> None:
        """Long polling. Для продакшена MAX рекомендует webhook (только HTTPS)."""
        marker: int | None = None
        while True:
            try:
                data = await self.api.get_updates(marker)
                marker = data.get("marker", marker)
                for upd in data.get("updates") or []:
                    try:
                        await self.handle(upd)
                    except Exception:  # noqa: BLE001 — одна плохая запись не должна ронять бота
                        log.exception("update handling failed")
            except (MaxApiError, asyncio.TimeoutError, OSError) as e:
                log.warning("polling error: %s", type(e).__name__)
                await asyncio.sleep(3)

    async def reminders(self) -> None:
        while True:
            try:
                current = self.now()
                for kind, b in reminders_due(self.svc, current):
                    if not b.client_user_id.startswith("max:"):
                        continue
                    when = "завтра" if kind == "24h" else "через 2 часа"
                    await self.safe_send(int(b.client_user_id[4:]),
                                         f"⏰ Напоминаем: {when} у вас запись\n{escape(booking_text(self.svc, b))}",
                                         [[btn("Отменить запись", f"cnl:{b.id}")]])
                    mark_reminder_sent(self.svc, b.id, kind)
            except Exception:  # noqa: BLE001
                log.exception("reminder loop error")
            await asyncio.sleep(60)
