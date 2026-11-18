"""Расчёт свободных слотов для записи.

Идея: берём рабочий день мастера, нарезаем его шагом `step_min`
и выкидываем слоты, которые пересекаются с уже занятыми интервалами
или уже прошли.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, tzinfo
from typing import Iterable

from .models import Master, Service


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Два полуинтервала [a_start, a_end) и [b_start, b_end) пересекаются?

    Типичная ошибка — сравнивать через <=: тогда запись 10:00–11:00
    «конфликтует» с записью 11:00–12:00, хотя они идут встык.
    """
    return a_start < b_end and b_start < a_end


def free_slots(
    master: Master,
    service: Service,
    day: date,
    busy: Iterable[tuple[datetime, datetime]],
    tz: tzinfo,
    now: datetime,
    step_min: int = 30,
) -> list[datetime]:
    """Вернуть список времён начала, на которые можно записаться."""
    if service.id not in master.service_ids or day.weekday() in master.days_off:
        return []

    # Все даты храним с часовым поясом: иначе на сервере в UTC
    # запись «на 10:00» внезапно окажется на 13:00 по Москве.
    day_start = datetime.combine(day, master.work_start, tzinfo=tz)
    day_end = datetime.combine(day, master.work_end, tzinfo=tz)
    duration = timedelta(minutes=service.duration_min)
    step = timedelta(minutes=step_min)
    busy_list = list(busy)

    result: list[datetime] = []
    start = day_start
    while start + duration <= day_end:
        end = start + duration
        if start > now and not any(overlaps(start, end, b0, b1) for b0, b1 in busy_list):
            result.append(start)
        start += step
    return result
