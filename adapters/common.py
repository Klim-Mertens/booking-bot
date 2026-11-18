"""Общее для адаптеров Telegram и MAX: тексты, разбор кнопок, данные для календаря."""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta

from core.booking import BookingService
from core.models import Booking

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]

# Формат запроса из виджета: "s:<номер дня 0..13>:<минуты от полуночи>"
_WIDGET_REQ = re.compile(r"^s:(\d{1,2}):(\d{1,4})$")


def human_dt(dt: datetime) -> str:
    return f"{WEEKDAYS[dt.weekday()]}, {dt.day} {MONTHS[dt.month - 1]}, {dt:%H:%M}"


def booking_text(svc: BookingService, b: Booking) -> str:
    service = svc.catalog.services.get(b.service_id)
    master = svc.catalog.masters.get(b.master_id)
    return (f"{human_dt(b.start_at.astimezone(svc.tz))}\n"
            f"{service.title if service else b.service_id} · {master.name if master else b.master_id}")


def offered_days(today: date, days_ahead: int) -> list[date]:
    return [today + timedelta(days=i) for i in range(days_ahead)]


def calendar_state(svc: BookingService, service_id: str, master_id: str,
                   days: list[date], now: datetime, title: str) -> str:
    """chat_data для виджета-календаря. Лимит Telegram — 1024 байта, поэтому формат
    компактный: время — минуты от полуночи. Если не влезает, отрезаем дальние дни."""
    rows = []
    for d in days:
        slots = svc.available_slots(service_id, master_id, d, now)
        rows.append([WEEKDAYS[d.weekday()], str(d.day), [s.hour * 60 + s.minute for s in slots]])
    while rows:
        payload = json.dumps({"t": title, "d": rows}, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode()) <= 1024:
            return payload
        rows.pop()
    return json.dumps({"t": title, "d": []}, ensure_ascii=False)


def parse_widget_request(data: str, days: list[date], tz) -> datetime | None:
    """Разобрать запрос виджета. Любой мусор → None. Существование слота
    потом ещё раз проверит BookingService.book()."""
    m = _WIDGET_REQ.fullmatch(data or "")
    if not m:
        return None
    day_idx, minutes = int(m.group(1)), int(m.group(2))
    if day_idx >= len(days) or not 0 <= minutes < 24 * 60:
        return None
    d = days[day_idx]
    return datetime(d.year, d.month, d.day, minutes // 60, minutes % 60, tzinfo=tz)


class RateLimiter:
    """Не больше `limit` действий за `window` секунд на пользователя (скользящее окно).
    Защита от флуда и перебора; хранится в памяти процесса."""

    def __init__(self, limit: int = 20, window: float = 60.0) -> None:
        self.limit, self.window = limit, window
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    MAX_KEYS = 50_000   # память не растёт бесконечно от потока новых пользователей

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        if len(self._hits) > self.MAX_KEYS:
            for k in [k for k, q in self._hits.items() if not q or now - q[-1] > self.window]:
                del self._hits[k]
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True
