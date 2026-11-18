"""Модели предметной области.

dataclass(frozen=True) — объекты неизменяемы: их нельзя случайно
поменять «по дороге» между адаптером и хранилищем.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time


@dataclass(frozen=True)
class Service:
    id: str              # короткий ID, например "haircut"; он же уходит в callback_data
    title: str           # "Мужская стрижка"
    duration_min: int    # длительность в минутах
    price_rub: int


@dataclass(frozen=True)
class Master:
    id: str
    name: str
    service_ids: frozenset[str]   # какие услуги делает мастер
    work_start: time               # начало рабочего дня
    work_end: time                 # конец рабочего дня
    days_off: frozenset[int] = frozenset()  # 0 = понедельник ... 6 = воскресенье


@dataclass(frozen=True)
class Booking:
    id: int
    master_id: str
    service_id: str
    start_at: datetime   # всегда timezone-aware (см. slots.py)
    end_at: datetime
    client_user_id: str  # ID пользователя в мессенджере вместе с платформой: "tg:123", "max:456"
    client_name: str
    status: str          # "active" | "cancelled"
