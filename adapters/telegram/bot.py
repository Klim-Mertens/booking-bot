"""Telegram-адаптер: кнопки и сообщения → вызовы BookingService.

Сценарий клиента:
  услуга → мастер → время (виджет-календарь ИЛИ обычные кнопки)
  → согласие на обработку ПДн → имя → телефон → подтверждение.

Правило безопасности для всего файла: всё, что приходит от пользователя
(callback_data, текст, данные виджета, контакт), считается недоверенным
и перепроверяется в ядре.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, CopyTextButton, DisabledButton, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup,
                           ReplyKeyboardRemove, TelegramObject, Update)

from adapters.common import (RateLimiter, booking_text, calendar_state, human_dt, offered_days,
                             parse_widget_request)
from core.booking import BookingError, BookingService, mark_reminder_sent, reminders_due
from core.config import Business
from core.crypto import normalize_phone
from core.models import Booking

from .methods import AnswerWidgetQuery, EditMessageWidgetData, SendWidget

log = logging.getLogger("booking.tg")
WIDGET_HTML = (Path(__file__).resolve().parents[2] / "widgets" / "calendar.html").read_text("utf-8")

BTN_BOOK, BTN_MY, BTN_CONTACTS = "📅 Записаться", "🗂 Мои записи", "📍 Контакты"
# Эффект «конфетти» для сообщения об успешной записи (работает в личных чатах).
EFFECT_CONFETTI = "5046509860389126442"
# Цвет кнопок (Bot API 9.4): success — зелёная, danger — красная, primary — синяя.
OK, DANGER, PRIMARY = "success", "danger", "primary"


class Flow(StatesGroup):
    choosing = State()   # выбирает услугу / мастера / время
    consent = State()
    name = State()
    phone = State()
    confirm = State()


def uid(user_id: int) -> str:
    """ID клиента с префиксом платформы: в одной базе живут клиенты Telegram и MAX."""
    return f"tg:{user_id}"


def ikb(rows: list[list[tuple]]) -> InlineKeyboardMarkup:
    """Кнопки из кортежей (текст, callback_data[, цвет]). callback_data=None — неактивная кнопка."""
    def make(t: str, d: str | None, style: str | None = None) -> InlineKeyboardButton:
        if d is None:
            return InlineKeyboardButton(text=t, disabled=DisabledButton())   # Bot API 10.3
        return InlineKeyboardButton(text=t, callback_data=d, style=style)
    return InlineKeyboardMarkup(inline_keyboard=[[make(*b) for b in row] for row in rows])


def tg_when(dt: datetime) -> str:
    """Дата-время сущностью date_time (Bot API 9.5): Telegram покажет её в часовом
    поясе пользователя. Внутри тега — обычный текст на случай старого клиента."""
    return f'<tg-time unix="{int(dt.timestamp())}">{escape(human_dt(dt))}</tg-time>'


def tg_booking_text(svc: BookingService, b: Booking) -> str:
    """booking_text, но с «живой» датой. Возвращает готовый HTML."""
    first, _, rest = booking_text(svc, b).partition("\n")
    return f"{tg_when(b.start_at.astimezone(svc.tz))}\n{escape(rest)}"


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_BOOK, style=PRIMARY)],
                                         [KeyboardButton(text=BTN_MY), KeyboardButton(text=BTN_CONTACTS)]],
                               resize_keyboard=True)


class RateLimitMiddleware(BaseMiddleware):
    """Отсекает флуд до того, как он дойдёт до базы."""

    def __init__(self, limiter: RateLimiter) -> None:
        self.limiter = limiter

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user and not self.limiter.allow(uid(user.id)):
            if isinstance(event, CallbackQuery):
                await event.answer("Слишком часто, подождите минуту", show_alert=False)
            return None
        return await handler(event, data)


def build(svc: BookingService, biz: Business, admins: frozenset[int],
          extra_notify: Callable[[str], Awaitable[None]] | None = None) -> Dispatcher:
    dp = Dispatcher()
    router = Router()
    limiter = RateLimiter()
    router.message.middleware(RateLimitMiddleware(limiter))
    router.callback_query.middleware(RateLimitMiddleware(limiter))

    def now() -> datetime:
        return datetime.now(biz.tz)

    # ------------------------------------------------------------------ меню

    @router.message(CommandStart())
    async def start(m: Message, state: FSMContext) -> None:
        await state.clear()
        await m.answer(f"Здравствуйте! Это бот записи в <b>{escape(biz.name)}</b>.\n"
                       "Запишитесь за минуту, а мы напомним о визите.", reply_markup=main_kb())

    @router.message(F.text == BTN_CONTACTS)
    async def contacts(m: Message) -> None:
        await m.answer(f"<b>{escape(biz.name)}</b>\n{escape(biz.address)}\n{escape(biz.phone)}",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                           [InlineKeyboardButton(text="📋 Скопировать адрес",
                                                 copy_text=CopyTextButton(text=biz.address[:256]))],
                           [InlineKeyboardButton(text="📋 Скопировать телефон",
                                                 copy_text=CopyTextButton(text=biz.phone[:256]))],
                       ]))

    @router.message(F.text == BTN_MY)
    async def my_bookings(m: Message) -> None:
        items = svc.client_bookings(uid(m.from_user.id), now())
        if not items:
            await m.answer("Активных записей нет.", reply_markup=main_kb())
            return
        for b in items:
            await m.answer(tg_booking_text(svc, b),
                           reply_markup=ikb([[("Отменить запись", f"cnl:{b.id}", DANGER)]]))

    @router.callback_query(F.data.regexp(r"^cnl:\d{1,10}$"))
    async def cancel(c: CallbackQuery) -> None:
        try:
            b = svc.cancel(int(c.data.split(":")[1]), uid(c.from_user.id))
        except BookingError as e:
            await c.answer(str(e), show_alert=True)
            return
        await c.message.edit_text(f"❌ Отменено: {tg_booking_text(svc, b)}")
        await notify_admins(c.bot, f"❌ Клиент отменил запись #{b.id}\n{escape(booking_text(svc, b))}")
        await c.answer()

    # ------------------------------------------------------- выбор услуги

    @router.message(F.text == BTN_BOOK)
    async def choose_service(m: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(Flow.choosing)
        rows = [[(f"{s.title} · {s.price_rub} ₽", f"svc:{s.id}")] for s in svc.catalog.services.values()]
        await m.answer("Выберите услугу:", reply_markup=ikb(rows))

    @router.callback_query(Flow.choosing, F.data.regexp(r"^svc:[a-z0-9_]{1,16}$"))
    async def choose_master(c: CallbackQuery, state: FSMContext) -> None:
        service_id = c.data[4:]
        masters = svc.catalog.masters_for(service_id)
        if service_id not in svc.catalog.services or not masters:
            await c.answer("Услуга недоступна", show_alert=True)
            return
        await state.update_data(service=service_id)
        await c.message.edit_text("Выберите мастера:",
                                  reply_markup=ikb([[(mm.name, f"mst:{mm.id}")] for mm in masters]))
        await c.answer()

    @router.callback_query(Flow.choosing, F.data.regexp(r"^mst:[a-z0-9_]{1,16}$"))
    async def choose_time(c: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        service_id, master_id = data.get("service"), c.data[4:]
        master = svc.catalog.masters.get(master_id)
        if not service_id or master is None or service_id not in master.service_ids:
            await c.answer("Мастер недоступен", show_alert=True)
            return
        days = offered_days(now().date(), biz.days_ahead)
        await state.update_data(master=master_id, days=[d.isoformat() for d in days])
        title = f"{svc.catalog.services[service_id].title} · {master.name}"
        await c.message.edit_text(f"<b>{escape(title)}</b>")
        await c.answer()
        try:
            # Основной путь — виджет-календарь (Bot API 10.4).
            msg = await c.bot(SendWidget(
                chat_id=c.message.chat.id, html=WIDGET_HTML, allow_queries=True,
                chat_data=calendar_state(svc, service_id, master_id, days, now(), title),
            ))
            await state.update_data(widget_msg=msg.message_id)
            await c.message.answer("Нажмите на удобное время в календаре.",
                                   reply_markup=ikb([[("Выбрать списком", "nowidget")]]))
        except TelegramAPIError as e:
            # Запасной путь: старый клиент или виджеты недоступны → обычные кнопки.
            log.warning("виджет недоступен, показываю кнопки: %s: %s", type(e).__name__, e)  # текст ошибки Telegram, без токена
            await send_days(c.message, svc, service_id, master_id, days)

    @router.callback_query(Flow.choosing, F.data == "nowidget")
    async def no_widget(c: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        await c.answer()
        await send_days(c.message, svc, data["service"], data["master"],
                        [date.fromisoformat(d) for d in data["days"]])

    async def send_days(msg: Message, s: BookingService, service_id: str, master_id: str,
                        days: list[date]) -> None:
        rows, any_free = [], False
        for i, d in enumerate(days):
            label = human_dt(datetime.combine(d, datetime.min.time())).rsplit(",", 1)[0]
            if s.available_slots(service_id, master_id, d, now()):
                rows.append([(label, f"day:{i}")])
                any_free = True
            else:
                rows.append([(f"{label} — занято", None)])   # видно, но нажать нельзя
        if not any_free:
            await msg.answer("Свободного времени на ближайшие дни нет. Позвоните нам: " + escape(biz.phone))
            return
        await msg.answer("Выберите день:", reply_markup=ikb(rows))

    @router.callback_query(Flow.choosing, F.data.regexp(r"^day:\d{1,2}$"))
    async def choose_slot(c: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        idx = int(c.data[4:])
        days = [date.fromisoformat(d) for d in data.get("days", [])]
        if idx >= len(days):
            await c.answer()
            return
        slots = svc.available_slots(data["service"], data["master"], days[idx], now())
        buttons = [(f"{s:%H:%M}", f"tm:{idx}:{s.hour * 60 + s.minute}") for s in slots]
        rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
        rows.append([("← Другой день", "nowidget")])
        await c.message.edit_text("Выберите время:" if buttons else "На этот день время уже заняли.",
                                  reply_markup=ikb(rows))
        await c.answer()

    @router.callback_query(Flow.choosing, F.data.regexp(r"^tm:\d{1,2}:\d{1,4}$"))
    async def slot_button(c: CallbackQuery, state: FSMContext) -> None:
        _, idx, minutes = c.data.split(":")
        ok, err = await on_slot(c.bot, state, f"s:{idx}:{minutes}", c.message.chat.id)
        await c.answer(None if ok else err, show_alert=not ok)

    async def on_slot(bot: Bot, state: FSMContext, raw: str, chat_id: int) -> tuple[bool, str]:
        """Общая точка для виджета и кнопок: проверить слот и перейти к согласию."""
        if await state.get_state() != Flow.choosing.state:
            return False, "Начните запись заново"
        data = await state.get_data()
        days = [date.fromisoformat(d) for d in data.get("days", [])]
        start_at = parse_widget_request(raw, days, biz.tz)
        if start_at is None or start_at not in svc.available_slots(data["service"], data["master"],
                                                                   start_at.date(), now()):
            return False, "Время недоступно, выберите другое"
        await state.update_data(start=start_at.isoformat())
        await state.set_state(Flow.consent)
        await bot.send_message(
            chat_id,
            f"Вы выбрали: <b>{tg_when(start_at)}</b>\n\n"
            "Для записи нужны имя и телефон. Нажимая «Согласен», вы даёте согласие на обработку "
            f"персональных данных по <a href=\"{escape(biz.privacy_url)}\">политике</a>. "
            f"Данные хранятся в зашифрованном виде и удаляются через {biz.keep_days} дней после визита.",
            reply_markup=ikb([[("✅ Согласен", "consent", OK), ("Отмена", "abort", DANGER)]]),
            disable_web_page_preview=True,
        )
        return True, ""

    # ------------------------------------------------- виджет (Bot API 10.4)

    async def widget_middleware(handler, update: Update, data: dict[str, Any]) -> Any:
        """aiogram 3.31 ещё не знает апдейт widget_query, поэтому ловим его сами
        до стандартной маршрутизации. Поле приходит как «лишнее» (extra) в Update."""
        wq = (update.model_extra or {}).get("widget_query")
        if not wq:
            return await handler(update, data)
        bot: Bot = data["bot"]
        # Доверяем только полям, которые подписывает Telegram: from.id и chat.id.
        user_id = int(wq["from"]["id"])
        chat_id = int(wq.get("chat", {}).get("id", user_id))
        answer: dict[str, Any]
        answer: dict[str, Any] = {"err": "Ошибка, попробуйте ещё раз"}
        try:
            if not limiter.allow(uid(user_id)):
                answer = {"err": "Слишком часто, подождите"}
            else:
                state = dp.fsm.get_context(bot=bot, chat_id=chat_id, user_id=user_id)
                ok, err = await on_slot(bot, state, str(wq.get("data", ""))[:64], chat_id)
                answer = {"ok": 1} if ok else {"err": err}
                await refresh_widget(bot, state, chat_id, chosen=ok)
        finally:
            # Ответить нужно в любом случае, иначе виджет «зависнет» в ожидании.
            await bot(AnswerWidgetQuery(widget_query_id=str(wq["id"]),
                                        data=json.dumps(answer, ensure_ascii=False)))
        return None

    async def refresh_widget(bot: Bot, state: FSMContext, chat_id: int, chosen: bool) -> None:
        """Обновить календарь на месте: выбранное время или свежие свободные слоты."""
        data = await state.get_data()
        if not data.get("widget_msg"):
            return
        master = svc.catalog.masters[data["master"]]
        title = f"{svc.catalog.services[data['service']].title} · {master.name}"
        if chosen:
            payload = json.dumps({"t": title, "d": [], "ok": human_dt(datetime.fromisoformat(data["start"]))},
                                 ensure_ascii=False)
        else:
            payload = calendar_state(svc, data["service"], data["master"],
                                     [date.fromisoformat(d) for d in data["days"]], now(), title)
        try:
            await bot(EditMessageWidgetData(chat_id=chat_id, message_id=data["widget_msg"], chat_data=payload))
        except TelegramBadRequest:
            pass  # «message is not modified» — состояние не изменилось

    dp.update.outer_middleware(widget_middleware)

    # ------------------------------------------- согласие, имя, телефон

    @router.callback_query(F.data == "abort")
    async def abort(c: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await c.message.edit_text("Запись отменена.")
        await c.answer()

    @router.callback_query(Flow.consent, F.data == "consent")
    async def consent(c: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(consent=True)
        await state.set_state(Flow.name)
        await c.message.edit_reply_markup(reply_markup=None)
        await c.message.answer("Как вас зовут?")
        await c.answer()

    @router.message(Flow.name, F.text)
    async def got_name(m: Message, state: FSMContext) -> None:
        name = " ".join(m.text.split())[:64]
        if not name or name.startswith("/"):
            await m.answer("Напишите, пожалуйста, имя текстом.")
            return
        await state.update_data(name=name)
        await state.set_state(Flow.phone)
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
                                 resize_keyboard=True, one_time_keyboard=True)
        await m.answer("Отправьте номер кнопкой ниже или напишите его.", reply_markup=kb)

    @router.message(Flow.phone, F.contact | F.text)
    async def got_phone(m: Message, state: FSMContext) -> None:
        if m.contact:
            # Чужой контакт (переслал карточку друга) не принимаем — только свой.
            if m.contact.user_id != m.from_user.id:
                await m.answer("Отправьте, пожалуйста, свой номер.")
                return
            phone = m.contact.phone_number
        else:
            phone = m.text
        try:
            normalize_phone(phone)          # проверяем сразу, а не после «Проверьте данные»
        except ValueError:
            await m.answer("Не похоже на номер телефона. Напишите, например, +7 999 123-45-67.")
            return
        await state.update_data(phone=phone)
        await state.set_state(Flow.confirm)
        data = await state.get_data()
        s = svc.catalog.services[data["service"]]
        await m.answer("Проверьте данные:", reply_markup=ReplyKeyboardRemove())
        await m.answer(
            f"<b>{escape(s.title)}</b> · {s.price_rub} ₽\n"
            f"Мастер: {escape(svc.catalog.masters[data['master']].name)}\n"
            f"Когда: {tg_when(datetime.fromisoformat(data['start']))}\n"
            f"Имя: {escape(data['name'])}",
            reply_markup=ikb([[("✅ Записаться", "confirm", OK), ("Отмена", "abort", DANGER)]]),
        )

    @router.callback_query(Flow.confirm, F.data == "confirm")
    async def confirm(c: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        try:
            b = svc.book(service_id=data["service"], master_id=data["master"],
                         start_at=datetime.fromisoformat(data["start"]), client_user_id=uid(c.from_user.id),
                         client_name=data["name"], phone_raw=data["phone"],
                         consent_given=bool(data.get("consent")), now=now())
        except BookingError as e:
            if "Неверный формат телефона" in str(e):
                await state.set_state(Flow.phone)
                await c.message.edit_text("Неверный формат телефона. Напишите номер ещё раз, например +7 999 123-45-67.")
            else:
                await state.clear()
                await c.message.edit_text(f"{escape(str(e))}. Нажмите «{BTN_BOOK}», чтобы выбрать снова.")
            await c.answer()
            return
        await state.clear()   # телефон больше не держим в памяти FSM
        await c.message.edit_text(f"Запись #{b.id} подтверждена.")
        try:
            # Отдельное сообщение — чтобы показать эффект «конфетти» (он есть только у новых сообщений).
            await c.message.answer(f"✅ Вы записаны!\n{tg_booking_text(svc, b)}\n\nНапомним за сутки и за 2 часа.",
                                   message_effect_id=EFFECT_CONFETTI, reply_markup=main_kb())
        except TelegramBadRequest:
            await c.message.answer(f"✅ Вы записаны!\n{tg_booking_text(svc, b)}\n\nНапомним за сутки и за 2 часа.",
                                   reply_markup=main_kb())
        await notify_admins(c.bot, f"🆕 Новая запись #{b.id}\n{escape(booking_text(svc, b))}\n"
                                   f"Клиент: {escape(b.client_name)}",
                            ikb([[("📞 Показать телефон", f"aph:{b.id}"), ("Отменить", f"acn:{b.id}", DANGER)]]))
        await c.answer()

    # ---------------------------------------------------------- админ

    def is_admin(user_id: int) -> bool:
        return user_id in admins

    async def notify_admins(bot: Bot, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        for admin_id in admins:
            try:
                await bot.send_message(admin_id, text, reply_markup=markup)
            except TelegramAPIError:
                log.warning("cannot notify admin")  # без ID и текста в логе
        if extra_notify:
            try:
                await extra_notify(text)            # копия админам в MAX
            except Exception:  # noqa: BLE001
                log.warning("cross-platform notify failed")

    @router.message(Command("today"))
    async def today(m: Message) -> None:
        if not is_admin(m.from_user.id):
            return  # для чужих команда «не существует»
        day_start = datetime.combine(now().date(), datetime.min.time(), tzinfo=biz.tz)
        items = svc.day_bookings(day_start)
        if not items:
            await m.answer("Сегодня записей нет.")
        for b in items:
            await m.answer(f"#{b.id} {escape(booking_text(svc, b))}\nКлиент: {escape(b.client_name)}",
                           reply_markup=ikb([[("📞 Телефон", f"aph:{b.id}"), ("Отменить", f"acn:{b.id}", DANGER)]]))

    @router.callback_query(F.data.regexp(r"^aph:\d{1,10}$"))
    async def admin_phone(c: CallbackQuery) -> None:
        if not is_admin(c.from_user.id):
            await c.answer()
            return
        try:
            phone = svc.admin_phone(int(c.data[4:]))
        except BookingError as e:
            await c.answer(str(e), show_alert=True)
            return
        log.info("admin revealed phone for booking %s", c.data[4:])   # аудит без самого номера
        await c.answer(phone, show_alert=True)   # всплывающее окно, номер не остаётся в чате

    @router.callback_query(F.data.regexp(r"^acn:\d{1,10}$"))
    async def admin_cancel(c: CallbackQuery) -> None:
        if not is_admin(c.from_user.id):
            await c.answer()
            return
        try:
            b = svc.cancel(int(c.data[4:]), uid(c.from_user.id), is_admin=True)
        except BookingError as e:
            await c.answer(str(e), show_alert=True)
            return
        await c.message.edit_text(f"❌ Отменено администратором: #{b.id}\n{escape(booking_text(svc, b))}")
        if b.client_user_id.startswith("tg:"):
            try:
                await c.bot.send_message(int(b.client_user_id[3:]),
                                         f"К сожалению, ваша запись отменена:\n{escape(booking_text(svc, b))}\n"
                                         f"Свяжитесь с нами: {escape(biz.phone)}")
            except TelegramAPIError:
                pass
        await c.answer()

    dp.include_router(router)
    return dp


async def reminder_loop(bot: Bot, svc: BookingService, biz: Business) -> None:
    """Раз в минуту: напоминания клиентам и удаление старых записей (152-ФЗ)."""
    while True:
        try:
            current = datetime.now(biz.tz)
            for kind, b in reminders_due(svc, current):
                if not b.client_user_id.startswith("tg:"):
                    continue   # клиентам MAX напоминает MAX-адаптер
                when = "завтра" if kind == "24h" else "через 2 часа"
                try:
                    await bot.send_message(int(b.client_user_id[3:]),
                                           f"⏰ Напоминаем: {when} у вас запись\n{tg_booking_text(svc, b)}",
                                           reply_markup=ikb([[("Отменить запись", f"cnl:{b.id}", DANGER)]]))
                except TelegramAPIError:
                    pass
                mark_reminder_sent(svc, b.id, kind)
            svc.purge_old(current, biz.keep_days)
        except Exception:  # noqa: BLE001 — цикл не должен падать из-за одной ошибки
            log.exception("reminder loop error")
        await asyncio.sleep(60)
