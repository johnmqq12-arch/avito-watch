"""Утилиты: разбор цен и дат Авито, текст, случайные задержки."""
from __future__ import annotations

import asyncio
import hashlib
import random
import re
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3), "Europe/Moscow")

_NBSP = "\u00a0\u2009\u202f\u2007"
_PRICE_RE = re.compile(r"(\d[\d\s\u00a0\u2009\u202f]*)")
_FREE_WORDS = ("бесплатно", "даром")
_NEGOTIABLE_WORDS = ("договорная", "цена не указана", "по запросу", "не указана")

_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}


def now() -> datetime:
    return datetime.now(MSK)


def clean_text(value: str | None) -> str:
    """Убирает неразрывные пробелы и лишние пробелы/переводы строк."""
    if not value:
        return ""
    for ch in _NBSP:
        value = value.replace(ch, " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n\s*\n\s*", "\n", value)
    return value.strip()


def parse_price(value: str | int | float | None) -> int | None:
    """'62 990 ₽' -> 62990. 'Договорная'/'Бесплатно' -> None (текст сохраняем отдельно)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = clean_text(str(value)).lower()
    if not text or any(w in text for w in _NEGOTIABLE_WORDS):
        return None
    if any(w in text for w in _FREE_WORDS):
        return 0
    m = _PRICE_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def format_price(price: int | None, currency: str = "RUB") -> str:
    if price is None:
        return "цена не указана"
    if price == 0:
        return "Бесплатно"
    symbol = {"RUB": "₽", "USD": "$", "EUR": "€"}.get(currency, currency)
    return f"{price:,}".replace(",", " ") + f" {symbol}"


def parse_published(value: str | int | float | None, *, base: datetime | None = None) -> datetime | None:
    """Понимает и абсолютные, и относительные даты Авито.

    Примеры: '2 часа назад', 'вчера в 14:20', 'сегодня в 09:15',
    '3 дня назад', 'неделю назад', '12 октября', '12 октября 2025',
    '12.10.2025', unix timestamp (сек/мс), ISO-8601.
    """
    if value is None or value == "":
        return None
    base = base or now()

    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        ts = float(value)
        if ts > 1e12:  # миллисекунды
            ts /= 1000.0
        if ts <= 0:
            return None
        try:
            return datetime.fromtimestamp(ts, MSK)
        except (ValueError, OSError, OverflowError):
            return None

    text = clean_text(str(value)).lower().replace("ё", "е")

    # ISO 8601
    iso = re.match(r"^(\d{4}-\d{2}-\d{2})[t ](\d{2}:\d{2}(?::\d{2})?)", text)
    if iso:
        try:
            dt = datetime.fromisoformat(f"{iso.group(1)}T{iso.group(2)}")
            return dt.replace(tzinfo=MSK) if dt.tzinfo is None else dt.astimezone(MSK)
        except ValueError:
            pass

    time_match = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    hh, mm = (int(time_match.group(1)), int(time_match.group(2))) if time_match else (None, None)

    def at_time(day: datetime) -> datetime:
        if hh is not None:
            return day.replace(hour=hh % 24, minute=mm % 60, second=0, microsecond=0)
        return day.replace(hour=12, minute=0, second=0, microsecond=0)

    if "только что" in text or "несколько секунд" in text:
        return base
    if "сегодня" in text:
        return at_time(base)
    if "вчера" in text:
        return at_time(base - timedelta(days=1))
    if "позавчера" in text:
        return at_time(base - timedelta(days=2))

    # N минут/часов/дней/недель/месяцев назад
    rel = re.search(
        r"(\d+|одну|один|две|два|пол)\s*(секунд\w*|минут\w*|час\w*|день|дня|дней|недел\w*|месяц\w*|год\w*|года|лет)",
        text,
    )
    if rel is None and re.search(r"\b(минуту|час|день|неделю|месяц|год)\s+назад", text):
        rel = re.search(r"()\b(минуту|час|день|неделю|месяц|год)\s+назад", text)
    if rel:
        raw_n, unit = rel.group(1), rel.group(2)
        numbers = {"один": 1, "одну": 1, "два": 2, "две": 2, "пол": 0}
        n = numbers.get(raw_n, None)
        if n is None:
            n = int(raw_n) if raw_n.isdigit() else 1
        if unit.startswith("секунд"):
            return base - timedelta(seconds=n)
        if unit.startswith("минут"):
            return base - timedelta(minutes=n)
        if unit.startswith("час"):
            return base - timedelta(hours=n)
        if unit.startswith(("день", "дня", "дней")):
            return at_time(base - timedelta(days=n))
        if unit.startswith("недел"):
            return at_time(base - timedelta(weeks=n))
        if unit.startswith("месяц"):
            return at_time(base - timedelta(days=30 * n))
        if unit.startswith(("год", "лет")):
            return at_time(base - timedelta(days=365 * n))

    # 12 октября [2025] [в 14:20]
    m = re.search(r"(\d{1,2})\s+([а-я]{3,})\.?(?:\s+(\d{4}))?", text)
    if m:
        day = int(m.group(1))
        month = _MONTHS.get(m.group(2)[:3])
        if month and 1 <= day <= 31:
            year = int(m.group(3)) if m.group(3) else base.year
            try:
                dt = datetime(year, month, day, tzinfo=MSK)
            except ValueError:
                return None
            # «12 декабря» в январе означает прошлый год
            if dt - base > timedelta(days=2):
                try:
                    dt = dt.replace(year=year - 1)
                except ValueError:
                    return None
            return at_time(dt)

    # 12.10.2025 / 12.10
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b", text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else base.year
        if year < 100:
            year += 2000
        try:
            return at_time(datetime(year, month, day, tzinfo=MSK))
        except ValueError:
            return None
    return None


def human_date(dt: datetime | None) -> str:
    if dt is None:
        return "дата неизвестна"
    dt = dt.astimezone(MSK)
    delta = now() - dt
    if delta.total_seconds() < 60:
        rel = "только что"
    elif delta.total_seconds() < 3600:
        rel = f"{int(delta.total_seconds() // 60)} мин назад"
    elif delta.total_seconds() < 86400:
        rel = f"{int(delta.total_seconds() // 3600)} ч назад"
    elif delta.days < 7:
        rel = f"{delta.days} дн назад"
    else:
        rel = dt.strftime("%d.%m.%Y")
    return f"{dt.strftime('%d.%m %H:%M')} ({rel})"


def content_hash(*parts: str | int | None) -> str:
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def jitter(value: float, spread: float = 0.25) -> float:
    """±spread от значения, никогда не меньше 10% значения."""
    return max(value * 0.1, value * (1 + random.uniform(-spread, spread)))


async def asleep(seconds: float) -> None:
    await asyncio.sleep(max(0.0, seconds))


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,.;:-") + suffix


def absolute_avito_url(href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return "https://www.avito.ru" + href
    if href.startswith("http"):
        return href
    return None


def item_id_from_url(url: str | None) -> str | None:
    """https://www.avito.ru/moskva/telefony/iphone_15_1234567890 -> 1234567890"""
    if not url:
        return None
    m = re.search(r"_(\d{6,})(?:\?|#|$)", url) or re.search(r"/(\d{6,})(?:\?|#|$)", url)
    return m.group(1) if m else None


_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "slocation", "async", "context", "iid", "nav_from", "referrer", "rnd",
    "fbclid", "gclid", "yclid", "ymclid", "ysclid", "clid", "_openstats",
}


def strip_tracking(url: str) -> str:
    """Убирает мусорные параметры, оставляя рабочие (?q=, ?p=, ?f= и фильтры).

    Важно: ?context=… у Авито — это простыня base64 на пару килобайт. Если её
    не срезать, ссылка в Telegram разъезжается, а одно и то же объявление
    выглядит как разные URL.
    """
    if "?" not in url:
        return url
    base, query = url.split("?", 1)
    kept = []
    for part in query.split("&"):
        if not part:
            continue
        key = part.split("=")[0]
        if key in _TRACKING_PARAMS or key.startswith("utm_"):
            continue
        kept.append(part)
    return base + ("?" + "&".join(kept) if kept else "")