"""Сценарии записи — единственная точка входа для адаптеров Telegram и MAX.

Адаптер передаёт сюда «сырые» ID из кнопок. Сервис сам проверяет,
что такие услуга, мастер и время существуют и свободны. Поэтому
подделанные callback_data ничего не сломают.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from .crypto import PiiCipher, normalize_phone
from .models import Booking, Master, Service
from .slots import free_slots
from .storage import SlotTakenError, Storage

MAX_NAME_LEN = 64
MAX_ACTIVE_PER_CLIENT = 3      # защита от «забронировать весь день» одним человеком
BOOKING_HORIZON_DAYS = 30      # дальше месяца записываться нельзя


class BookingError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


@dataclass(frozen=True)
class Catalog:
    services: dict[str, Service]
    masters: dict[str, Master]

    def masters_for(self, service_id: str) -> list[Master]:
        return [m for m in self.masters.values() if service_id in m.service_ids]


class BookingService:
    def __init__(self, catalog: Catalog, storage: Storage, cipher: PiiCipher, tz: tzinfo) -> None:
        self.catalog = catalog
        self._storage = storage
        self._cipher = cipher
        self.tz = tz

    # --- чтение -------------------------------------------------------------

    def available_slots(self, service_id: str, master_id: str, day: date, now: datetime) -> list[datetime]:
        service, master = self._resolve(service_id, master_id)
        if not self._in_horizon(day, now):
            return []
        day_start = datetime.combine(day, master.work_start, tzinfo=self.tz)
        day_end = datetime.combine(day, master.work_end, tzinfo=self.tz)
        busy = self._storage.busy_intervals(master.id, day_start, day_end)
        return free_slots(master, service, day, busy, self.tz, now)

    def client_bookings(self, client_user_id: str, now: datetime) -> list[Booking]:
        return self._storage.client_bookings(client_user_id, now)

    # --- запись -------------------------------------------------------------

    def book(
        self,
        *,
        service_id: str,
        master_id: str,
        start_at: datetime,
        client_user_id: str,
        client_name: str,
        phone_raw: str,
        consent_given: bool,
        now: datetime,
    ) -> Booking:
        if not consent_given:
            # Без согласия на обработку ПДн телефон не сохраняем вообще.
            raise BookingError("Нужно согласие на обработку персональных данных")

        service, master = self._resolve(service_id, master_id)
        name = " ".join(client_name.split())[:MAX_NAME_LEN]
        if not name:
            raise BookingError("Укажите имя")
        try:
            phone = normalize_phone(phone_raw)
        except ValueError as exc:
            raise BookingError(str(exc)) from exc

        if len(self._storage.client_bookings(client_user_id, now)) >= MAX_ACTIVE_PER_CLIENT:
            raise BookingError("У вас уже максимум активных записей")

        # Время из кнопки не принимаем на веру: оно должно быть среди свободных слотов.
        start_at = start_at.astimezone(self.tz)
        if start_at not in self.available_slots(service.id, master.id, start_at.date(), now):
            raise BookingError("Это время уже недоступно, выберите другое")

        try:
            booking_id = self._storage.create_booking(
                master_id=master.id,
                service_id=service.id,
                start_at=start_at,
                end_at=start_at + timedelta(minutes=service.duration_min),
                client_user_id=client_user_id,
                client_name=name,
                phone_enc=self._cipher.encrypt(phone),
                phone_hash=self._cipher.lookup_hash(phone),
                now=now,
            )
        except SlotTakenError as exc:
            raise BookingError("Это время только что заняли, выберите другое") from exc

        booking = self._storage.get(booking_id)
        assert booking is not None
        return booking

    def cancel(self, booking_id: int, requester_user_id: str, is_admin: bool = False) -> Booking:
        booking = self._storage.get(booking_id)
        # Проверка владельца: иначе любой, подставив чужой ID в callback_data,
        # отменит чужую запись (уязвимость IDOR).
        if booking is None or (booking.client_user_id != requester_user_id and not is_admin):
            raise BookingError("Запись не найдена")
        if not self._storage.cancel(booking_id):
            raise BookingError("Запись уже отменена")
        cancelled = self._storage.get(booking_id)
        assert cancelled is not None
        return cancelled

    def day_bookings(self, day_start: datetime) -> list[Booking]:
        return self._storage.day_bookings(day_start, day_start + timedelta(days=1))

    def purge_old(self, now: datetime, keep_days: int) -> int:
        return self._storage.purge_older_than(now - timedelta(days=keep_days))

    def admin_phone(self, booking_id: int) -> str:
        """Телефон расшифровывается только по запросу администратора."""
        enc = self._storage.phone_enc(booking_id)
        if enc is None:
            raise BookingError("Запись не найдена")
        return self._cipher.decrypt(enc)

    # --- служебное ----------------------------------------------------------

    def _resolve(self, service_id: str, master_id: str) -> tuple[Service, Master]:
        service = self.catalog.services.get(service_id)
        master = self.catalog.masters.get(master_id)
        if service is None or master is None or service.id not in master.service_ids:
            raise BookingError("Услуга или мастер не найдены")
        return service, master

    def _in_horizon(self, day: date, now: datetime) -> bool:
        today = now.astimezone(self.tz).date()
        return today <= day <= today + timedelta(days=BOOKING_HORIZON_DAYS)


def reminders_due(service: "BookingService", now: datetime) -> list[tuple[str, Booking]]:
    """Какие напоминания пора отправить. Напоминание за 2 ч важнее: если до записи
    меньше 2 ч, напоминание за 24 ч уже не шлём, чтобы не дублировать."""
    storage = service._storage
    result: list[tuple[str, Booking]] = []
    two_h = storage.due_reminders("2h", now, timedelta(hours=2))
    two_h_ids = {b.id for b in two_h}
    for b in two_h:
        storage.mark_reminded(b.id, "24h")
        result.append(("2h", b))
    for b in storage.due_reminders("24h", now, timedelta(hours=24)):
        if b.id not in two_h_ids:
            result.append(("24h", b))
    return result


def mark_reminder_sent(service: "BookingService", booking_id: int, kind: str) -> None:
    service._storage.mark_reminded(booking_id, kind)
