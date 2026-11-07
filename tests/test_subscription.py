from datetime import datetime, timedelta, timezone

from bot.services.subscription import (
    Payload,
    days_left,
    extend,
    has_access,
    is_active,
    needs_reminder,
    validate_payment,
)
from bot.timeutil import to_db
from tests.conftest import make_settings

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def test_extend_from_now_when_expired_or_empty():
    assert extend(None, NOW, 30) == NOW + timedelta(days=30)
    assert extend(to_db(NOW - timedelta(days=5)), NOW, 30) == NOW + timedelta(days=30)


def test_extend_adds_to_active():
    until = NOW + timedelta(days=10)
    assert extend(to_db(until), NOW, 30) == until + timedelta(days=30)


def test_active_and_access():
    future = to_db(NOW + timedelta(hours=1))
    past = to_db(NOW - timedelta(seconds=1))
    assert is_active(future, NOW) and not is_active(past, NOW) and not is_active(None, NOW)
    assert has_access(42, None, admin_id=42, now=NOW)  # админ бесплатно
    assert not has_access(7, past, admin_id=42, now=NOW)
    assert has_access(7, future, admin_id=42, now=NOW)


def test_reminder_window_and_days_left():
    assert needs_reminder(to_db(NOW + timedelta(days=2, hours=23)), NOW)
    assert not needs_reminder(to_db(NOW + timedelta(days=3, hours=1)), NOW)
    assert not needs_reminder(to_db(NOW - timedelta(hours=1)), NOW)
    assert days_left(to_db(NOW + timedelta(days=2, hours=1)), NOW) == 3
    assert days_left(None, NOW) == 0


def test_payload_roundtrip_and_garbage():
    p = Payload("stars", 123, 30)
    assert Payload.parse(p.pack()) == p
    for raw in ("", "sub:stars:abc:30", "sub:paypal:1:30", "x:stars:1:30", "sub:stars:1:0"):
        assert Payload.parse(raw) is None


def test_validate_payment(tmp_path):
    s = make_settings(tmp_path)
    good = Payload("stars", 5, 30).pack()
    assert validate_payment(good, "XTR", 250, 5, s)
    assert validate_payment(good, "XTR", 1, 5, s) is None  # неверная сумма
    assert validate_payment(good, "XTR", 250, 6, s) is None  # чужой счёт
    click = Payload("click", 5, 30).pack()
    assert validate_payment(click, "UZS", 4_900_000, 5, s) is None  # Click выключен
    s_on = make_settings(tmp_path, click_enabled=True)
    assert validate_payment(click, "UZS", 4_900_000, 5, s_on)
