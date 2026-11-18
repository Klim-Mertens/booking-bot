"""Хранилище записей на SQLite.

Главное здесь — защита от двойной записи: два клиента одновременно
жмут «10:00» у одного мастера, и оба получают подтверждение. Это самая
частая ошибка дешёвых ботов. Решение:

1. Транзакция `BEGIN IMMEDIATE` — SQLite сразу берёт блокировку на запись,
   второй запрос ждёт, пока первый не закончит.
2. Внутри транзакции заново проверяем пересечение интервалов.

Все SQL-запросы параметризованы (знаки `?`), никакой склейки строк —
это защита от SQL-инъекций.

Время хранится как Unix-время в UTC (целое число): его легко сравнивать
и нет путаницы с часовыми поясами.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from .models import Booking

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    master_id       TEXT    NOT NULL,
    service_id      TEXT    NOT NULL,
    start_ts        INTEGER NOT NULL,
    end_ts          INTEGER NOT NULL,
    client_user_id  TEXT    NOT NULL,
    client_name     TEXT    NOT NULL,
    phone_enc       BLOB    NOT NULL,
    phone_hash      TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'active',
    created_ts      INTEGER NOT NULL,
    reminded_24h    INTEGER NOT NULL DEFAULT 0,
    reminded_2h     INTEGER NOT NULL DEFAULT 0,
    CHECK (end_ts > start_ts),
    CHECK (status IN ('active', 'cancelled'))
);
CREATE INDEX IF NOT EXISTS ix_bookings_master_time ON bookings (master_id, start_ts);
CREATE INDEX IF NOT EXISTS ix_bookings_client ON bookings (client_user_id);
"""


_REMINDER_COLUMNS = {"24h": "reminded_24h", "2h": "reminded_2h"}


class SlotTakenError(Exception):
    """Время уже занято (кто-то успел раньше)."""


def _ts(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("Ожидается datetime с часовым поясом")
    return int(dt.timestamp())


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class Storage:
    def __init__(self, path: str) -> None:
        self._path = path
        # У каждого потока своё соединение. Одно общее соединение на все потоки —
        # ошибка: транзакции разных запросов смешиваются («transaction within
        # a transaction»), и защита от двойной записи перестаёт работать.
        self._local = threading.local()
        self._db.executescript(SCHEMA)

    @property
    def _db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # isolation_level=None — транзакциями управляем сами (BEGIN/COMMIT).
            conn = sqlite3.connect(self._path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")   # чтение не блокирует запись
            conn.execute("PRAGMA busy_timeout=5000")  # ждать блокировку до 5 с
            self._local.conn = conn
        return conn

    def busy_intervals(self, master_id: str, from_dt: datetime, to_dt: datetime) -> list[tuple[datetime, datetime]]:
        rows = self._db.execute(
            "SELECT start_ts, end_ts FROM bookings "
            "WHERE master_id = ? AND status = 'active' AND start_ts < ? AND end_ts > ?",
            (master_id, _ts(to_dt), _ts(from_dt)),
        ).fetchall()
        return [(_dt(r["start_ts"]), _dt(r["end_ts"])) for r in rows]

    def create_booking(
        self,
        *,
        master_id: str,
        service_id: str,
        start_at: datetime,
        end_at: datetime,
        client_user_id: str,
        client_name: str,
        phone_enc: bytes,
        phone_hash: str,
        now: datetime,
    ) -> int:
        start_ts, end_ts = _ts(start_at), _ts(end_at)
        cur = self._db.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            conflict = cur.execute(
                "SELECT 1 FROM bookings WHERE master_id = ? AND status = 'active' "
                "AND start_ts < ? AND end_ts > ? LIMIT 1",
                (master_id, end_ts, start_ts),
            ).fetchone()
            if conflict:
                raise SlotTakenError
            cur.execute(
                "INSERT INTO bookings (master_id, service_id, start_ts, end_ts, client_user_id, "
                "client_name, phone_enc, phone_hash, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (master_id, service_id, start_ts, end_ts, client_user_id,
                 client_name, phone_enc, phone_hash, _ts(now)),
            )
            booking_id = cur.lastrowid
            cur.execute("COMMIT")
            return int(booking_id)
        except BaseException:
            cur.execute("ROLLBACK")
            raise

    def get(self, booking_id: int) -> Booking | None:
        row = self._db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        return self._row_to_booking(row) if row else None

    def client_bookings(self, client_user_id: str, now: datetime) -> list[Booking]:
        rows = self._db.execute(
            "SELECT * FROM bookings WHERE client_user_id = ? AND status = 'active' AND start_ts > ? "
            "ORDER BY start_ts",
            (client_user_id, _ts(now)),
        ).fetchall()
        return [self._row_to_booking(r) for r in rows]

    def day_bookings(self, from_dt: datetime, to_dt: datetime) -> list[Booking]:
        rows = self._db.execute(
            "SELECT * FROM bookings WHERE status = 'active' AND start_ts >= ? AND start_ts < ? "
            "ORDER BY start_ts",
            (_ts(from_dt), _ts(to_dt)),
        ).fetchall()
        return [self._row_to_booking(r) for r in rows]

    def phone_enc(self, booking_id: int) -> bytes | None:
        row = self._db.execute("SELECT phone_enc FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        return row["phone_enc"] if row else None

    def cancel(self, booking_id: int) -> bool:
        cur = self._db.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE id = ? AND status = 'active'", (booking_id,)
        )
        return cur.rowcount == 1

    def due_reminders(self, kind: str, now: datetime, ahead: timedelta) -> list[Booking]:
        """Активные записи, которые начнутся в ближайшие `ahead` и ещё не получили напоминание."""
        column = _REMINDER_COLUMNS[kind]  # имя колонки только из белого списка, не из ввода
        rows = self._db.execute(
            f"SELECT * FROM bookings WHERE status = 'active' AND {column} = 0 "
            "AND start_ts > ? AND start_ts <= ?",
            (_ts(now), _ts(now + ahead)),
        ).fetchall()
        return [self._row_to_booking(r) for r in rows]

    def mark_reminded(self, booking_id: int, kind: str) -> None:
        column = _REMINDER_COLUMNS[kind]
        self._db.execute(f"UPDATE bookings SET {column} = 1 WHERE id = ?", (booking_id,))

    def purge_older_than(self, cutoff: datetime) -> int:
        """Удалить прошедшие записи старше cutoff (минимизация хранения ПДн, 152-ФЗ)."""
        cur = self._db.execute("DELETE FROM bookings WHERE end_ts < ?", (_ts(cutoff),))
        return cur.rowcount

    @staticmethod
    def _row_to_booking(row: sqlite3.Row) -> Booking:
        return Booking(
            id=row["id"],
            master_id=row["master_id"],
            service_id=row["service_id"],
            start_at=_dt(row["start_ts"]),
            end_at=_dt(row["end_ts"]),
            client_user_id=row["client_user_id"],
            client_name=row["client_name"],
            status=row["status"],
        )
