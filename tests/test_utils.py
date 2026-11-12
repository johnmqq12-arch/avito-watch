"""Утилиты: цены, даты, ссылки."""
from datetime import datetime, timedelta

import pytest

from avito_watch.utils import (MSK, absolute_avito_url, clean_text, format_price,
                               human_date, item_id_from_url, parse_price, parse_published,
                               strip_tracking, truncate)

BASE = datetime(2026, 11, 12, 15, 30, tzinfo=MSK)


@pytest.mark.parametrize("raw,expected", [
    ("62 990 ₽", 62990),
    ("62\u00a0990\u2009₽", 62990),
    ("от 1 500 руб.", 1500),
    (62990, 62990),
    ("Бесплатно", 0),
    ("Даром", 0),
    ("Договорная", None),
    ("Цена не указана", None),
    ("", None),
    (None, None),
])
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


def test_format_price():
    assert format_price(62990) == "62 990 ₽"
    assert format_price(0) == "Бесплатно"
    assert format_price(None) == "цена не указана"


@pytest.mark.parametrize("raw,delta", [
    ("2 часа назад", timedelta(hours=2)),
    ("15 минут назад", timedelta(minutes=15)),
    ("час назад", timedelta(hours=1)),
    ("минуту назад", timedelta(minutes=1)),
    ("3 дня назад", timedelta(days=3, hours=3, minutes=30)),   # время суток обнуляется в 12:00
    ("неделю назад", timedelta(days=7, hours=3, minutes=30)),
])
def test_relative_dates(raw, delta):
    parsed = parse_published(raw, base=BASE)
    assert parsed is not None
    if "дн" in raw or "недел" in raw:
        assert parsed.date() == (BASE - delta).date()
        assert (parsed.hour, parsed.minute) == (12, 0)
    else:
        assert abs((BASE - parsed) - delta) < timedelta(minutes=1)


def test_today_yesterday_with_time():
    today = parse_published("сегодня в 09:15", base=BASE)
    assert (today.hour, today.minute) == (9, 15)
    assert today.date() == BASE.date()
    yesterday = parse_published("вчера в 19:05", base=BASE)
    assert (yesterday.hour, yesterday.minute) == (19, 5)
    assert yesterday.date() == (BASE - timedelta(days=1)).date()


def test_absolute_dates():
    assert parse_published("12 октября 2025", base=BASE) == datetime(2025, 10, 12, 12, 0, tzinfo=MSK)
    assert parse_published("12.10.2025", base=BASE).date() == datetime(2025, 10, 12).date()
    # «12 декабря» в ноябре — это прошлый год? нет: будущее в пределах 2 дней допустимо,
    # далеко впереди — значит прошлый год
    assert parse_published("12 декабря", base=BASE).year == 2025


def test_timestamp_dates():
    assert parse_published(1762941600, base=BASE).year == 2025
    assert parse_published(1762941600000, base=BASE).year == 2025   # миллисекунды
    assert parse_published("1762941600", base=BASE).year == 2025
    assert parse_published(None) is None
    assert parse_published("какой-то текст") is None


def test_iso_date():
    parsed = parse_published("2026-11-10T08:45:00", base=BASE)
    assert parsed == datetime(2026, 11, 10, 8, 45, tzinfo=MSK)


def test_human_date():
    assert "мин назад" in human_date(datetime.now(MSK) - timedelta(minutes=5))
    assert human_date(None) == "дата неизвестна"


def test_urls():
    assert absolute_avito_url("/moskva/telefony/x_123") == "https://www.avito.ru/moskva/telefony/x_123"
    assert absolute_avito_url("//www.avito.ru/x") == "https://www.avito.ru/x"
    assert item_id_from_url("https://www.avito.ru/moskva/telefony/iphone_15_7654321098?slocation=1") == "7654321098"
    assert item_id_from_url("https://www.avito.ru/moskva?q=iphone") is None
    assert strip_tracking("https://www.avito.ru/x?q=iphone&utm_source=telegram&p=2") == \
        "https://www.avito.ru/x?q=iphone&p=2"


def test_text_helpers():
    assert clean_text("  много\u00a0 пробелов  ") == "много пробелов"
    assert truncate("одно два три четыре", 12).endswith("…")
    assert truncate("коротко", 100) == "коротко"