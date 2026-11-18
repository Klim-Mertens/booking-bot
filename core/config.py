"""Загрузка настроек: каталог из TOML, секреты из переменных окружения."""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

from .booking import Catalog
from .models import Master, Service

_ID_RE = re.compile(r"^[a-z0-9_]{1,16}$")  # ID уходят в callback_data (до 64 байт) — держим их короткими


@dataclass(frozen=True)
class Business:
    name: str
    address: str
    phone: str
    tz: ZoneInfo
    privacy_url: str
    days_ahead: int
    keep_days: int


def _parse_time(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def _check_id(value: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"Недопустимый ID в конфиге: {value!r}")
    return value


def load_config(path: str) -> tuple[Business, Catalog]:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    b = raw["business"]
    business = Business(
        name=b["name"], address=b["address"], phone=b["phone"], tz=ZoneInfo(b["timezone"]),
        privacy_url=b["privacy_url"], days_ahead=int(b.get("days_ahead", 7)),
        keep_days=int(b.get("keep_days", 90)),
    )
    services = {
        _check_id(s["id"]): Service(s["id"], s["title"], int(s["duration_min"]), int(s["price_rub"]))
        for s in raw["services"]
    }
    masters = {}
    for m in raw["masters"]:
        unknown = set(m["services"]) - services.keys()
        if unknown:
            raise ValueError(f"У мастера {m['id']} неизвестные услуги: {unknown}")
        masters[_check_id(m["id"])] = Master(
            m["id"], m["name"], frozenset(m["services"]),
            _parse_time(m["work_start"]), _parse_time(m["work_end"]), frozenset(m.get("days_off", [])),
        )
    return business, Catalog(services, masters)


def require_env(name: str) -> str:
    """Секрет обязателен: если его нет, бот не стартует (лучше упасть сразу, чем работать без защиты)."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


def admin_ids(name: str = "ADMIN_IDS") -> frozenset[int]:
    """Админы — по числовым ID, не по username (username можно сменить и занять)."""
    raw = os.environ.get(name, "")
    return frozenset(int(x) for x in raw.replace(" ", "").split(",") if x)
