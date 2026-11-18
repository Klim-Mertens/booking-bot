"""Тесты ядра: слоты, двойная запись, IDOR, шифрование, валидация."""
from __future__ import annotations

import threading
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet

from core.booking import BookingError, BookingService, Catalog
from core.crypto import PiiCipher, normalize_phone
from core.models import Master, Service
from core.slots import free_slots, overlaps
from core.storage import Storage

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 11, 16, 9, 0, tzinfo=TZ)        # понедельник, 09:00
DAY = date(2026, 11, 17)                             # вторник

HAIRCUT = Service("haircut", "Мужская стрижка", 60, 1500)
BEARD = Service("beard", "Оформление бороды", 30, 800)
ANTON = Master("anton", "Антон", frozenset({"haircut", "beard"}), time(10), time(14), frozenset({6}))


@pytest.fixture
def svc(tmp_path):
    cipher = PiiCipher(Fernet.generate_key(), b"k" * 32)
    catalog = Catalog({s.id: s for s in (HAIRCUT, BEARD)}, {ANTON.id: ANTON})
    return BookingService(catalog, Storage(str(tmp_path / "t.db")), cipher, TZ)


def book(svc, start, user="tg:1", phone="+79991234567", consent=True, service="haircut"):
    return svc.book(service_id=service, master_id="anton", start_at=start, client_user_id=user,
                    client_name="Иван", phone_raw=phone, consent_given=consent, now=NOW)


def test_overlaps_back_to_back_is_not_conflict():
    t = lambda h: datetime(2026, 1, 1, h, tzinfo=TZ)
    assert not overlaps(t(10), t(11), t(11), t(12))
    assert overlaps(t(10), t(12), t(11), t(13))


def test_free_slots_respects_hours_and_busy():
    busy = [(datetime(2026, 11, 17, 11, tzinfo=TZ), datetime(2026, 11, 17, 12, tzinfo=TZ))]
    slots = free_slots(ANTON, HAIRCUT, DAY, busy, TZ, NOW)
    hours = [s.strftime("%H:%M") for s in slots]
    assert hours == ["10:00", "12:00", "12:30", "13:00"]   # 10:30 и 11:xx пересекаются с занятым


def test_day_off_has_no_slots():
    assert free_slots(ANTON, HAIRCUT, date(2026, 11, 22), [], TZ, NOW) == []   # воскресенье


def test_booking_removes_slot(svc):
    start = datetime(2026, 11, 17, 10, tzinfo=TZ)
    book(svc, start)
    assert start not in svc.available_slots("haircut", "anton", DAY, NOW)


def test_double_booking_rejected(svc):
    start = datetime(2026, 11, 17, 10, tzinfo=TZ)
    book(svc, start, user="tg:1")
    with pytest.raises(BookingError):
        book(svc, start, user="tg:2")


def test_parallel_double_booking_only_one_wins(svc):
    """Имитируем одновременные нажатия: успешной должна быть ровно одна запись."""
    start = datetime(2026, 11, 17, 12, tzinfo=TZ)
    ok, failed = [], []
    barrier = threading.Barrier(5)

    def worker(i):
        barrier.wait()
        try:
            book(svc, start, user=f"tg:{i}")
            ok.append(i)
        except BookingError:
            failed.append(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 1 and len(failed) == 4


def test_forged_time_not_in_grid_rejected(svc):
    with pytest.raises(BookingError):
        book(svc, datetime(2026, 11, 17, 10, 7, tzinfo=TZ))   # 10:07 — не слот
    with pytest.raises(BookingError):
        book(svc, datetime(2026, 11, 17, 3, tzinfo=TZ))       # ночью мастер не работает


def test_unknown_service_rejected(svc):
    with pytest.raises(BookingError):
        book(svc, datetime(2026, 11, 17, 10, tzinfo=TZ), service="'; DROP TABLE bookings;--")


def test_consent_required(svc):
    with pytest.raises(BookingError):
        book(svc, datetime(2026, 11, 17, 10, tzinfo=TZ), consent=False)


def test_cannot_cancel_someone_elses_booking(svc):
    b = book(svc, datetime(2026, 11, 17, 10, tzinfo=TZ), user="tg:1")
    with pytest.raises(BookingError):
        svc.cancel(b.id, requester_user_id="tg:999")
    assert svc.cancel(b.id, requester_user_id="tg:1").status == "cancelled"


def test_admin_can_cancel_and_read_phone(svc):
    b = book(svc, datetime(2026, 11, 17, 10, tzinfo=TZ), phone="8 (999) 123-45-67")
    assert svc.admin_phone(b.id) == "+79991234567"
    assert svc.cancel(b.id, requester_user_id="tg:admin", is_admin=True).status == "cancelled"


def test_limit_active_bookings_per_client(svc):
    for h in (10, 11, 12):
        book(svc, datetime(2026, 11, 17, h, tzinfo=TZ))
    with pytest.raises(BookingError):
        book(svc, datetime(2026, 11, 17, 13, tzinfo=TZ))


def test_phone_is_encrypted_at_rest(tmp_path):
    db = tmp_path / "raw.db"
    cipher = PiiCipher(Fernet.generate_key(), b"k" * 32)
    catalog = Catalog({HAIRCUT.id: HAIRCUT}, {ANTON.id: ANTON})
    s = BookingService(catalog, Storage(str(db)), cipher, TZ)
    book(s, datetime(2026, 11, 17, 10, tzinfo=TZ))
    assert b"9991234567" not in db.read_bytes() + (tmp_path / "raw.db-wal").read_bytes()


@pytest.mark.parametrize("raw", ["123", "abc", "+7999123456789012345", ""])
def test_bad_phones_rejected(raw):
    with pytest.raises(ValueError):
        normalize_phone(raw)


def test_horizon_limit(svc):
    assert svc.available_slots("haircut", "anton", NOW.date() + timedelta(days=60), NOW) == []


def test_reminders(svc):
    from core.booking import mark_reminder_sent, reminders_due
    b = book(svc, datetime(2026, 11, 17, 10, tzinfo=TZ))
    # за 20 часов — пора напоминание «за сутки»
    due = reminders_due(svc, datetime(2026, 11, 16, 14, tzinfo=TZ))
    assert [(k, x.id) for k, x in due] == [("24h", b.id)]
    mark_reminder_sent(svc, b.id, "24h")
    assert reminders_due(svc, datetime(2026, 11, 16, 15, tzinfo=TZ)) == []
    # за 1,5 часа — напоминание «за 2 часа», ровно одно
    due = reminders_due(svc, datetime(2026, 11, 17, 8, 30, tzinfo=TZ))
    assert [k for k, _ in due] == ["2h"]
    mark_reminder_sent(svc, b.id, "2h")
    assert reminders_due(svc, datetime(2026, 11, 17, 8, 40, tzinfo=TZ)) == []


def test_max_tls_verifies_certificates():
    import ssl
    from adapters.max.client import tls_context
    ctx = tls_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
