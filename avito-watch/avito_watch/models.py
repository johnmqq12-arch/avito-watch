"""Доменные модели проекта."""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class PageState(str, enum.Enum):
    """Состояние документа, который вернул Авито.

    Определяется по признакам файрвола (см. parser/state.py).
    """

    CONTENT = "content"      # нормальная выдача/карточка
    EMPTY = "empty"          # честная пустая выдача — не ошибка
    CHALLENGE = "challenge"  # proof-of-work (HTTP 439), браузер пройдёт сам
    CAPTCHA = "captcha"      # файрвол показал капчу (.js-firewall-form)
    BLOCKED = "blocked"      # «подождите немного и обновите страницу» — нужен новый IP
    UNKNOWN = "unknown"      # непонятно, имеет смысл повторить


class CaptchaVariant(str, enum.Enum):
    GEETEST = "geetest"      # GeeTest v4, пазл со слайдером
    IMAGE = "image"          # собственная капча-картинка Авито (#inner-captcha)
    HCAPTCHA = "hcaptcha"    # hCaptcha (#h-captcha)
    NONE = "none"


class SourceStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    CAPTCHA = "captcha"      # ждём ручного решения
    ERROR = "error"


@dataclass(slots=True)
class Listing:
    """Одно объявление. Всё, что просил пользователь: фото, ссылка, дата, описание, цена."""

    item_id: str
    url: str
    title: str = ""
    price: int | None = None
    price_text: str = ""
    currency: str = "RUB"
    old_price: int | None = None
    discount_pct: int | None = None
    image_url: str | None = None
    images: list[str] = field(default_factory=list)
    description: str | None = None
    published_at: datetime | None = None
    published_raw: str | None = None
    location: str | None = None
    seller_name: str | None = None
    seller_type: str | None = None
    params: str | None = None
    source_id: int | None = None
    price_source: str = "unknown"   # state | dom | item
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        """Достаточно ли данных для уведомления без захода на страницу объявления."""
        return bool(self.title and self.image_url and (self.price is not None or self.price_text))


@dataclass(slots=True)
class Source:
    """Одна отслеживаемая ссылка (поиск/категория) со своим интервалом."""

    id: int
    url: str
    name: str = ""
    interval_sec: int = 300
    pages: int = 1
    status: str = SourceStatus.ACTIVE.value
    seed_existing: bool = True
    min_price: int | None = None
    max_price: int | None = None
    keywords_include: list[str] = field(default_factory=list)
    keywords_exclude: list[str] = field(default_factory=list)
    last_check_at: float | None = None
    next_check_at: float = 0.0
    last_error: str | None = None
    consecutive_errors: int = 0
    found_total: int = 0
    notified_total: int = 0
    created_at: float = field(default_factory=time.time)

    @property
    def label(self) -> str:
        return self.name or f"#{self.id}"


@dataclass(slots=True)
class FetchResult:
    """Ответ движка загрузки страницы."""

    url: str
    html: str
    status: int = 200
    state: PageState = PageState.UNKNOWN
    engine: str = "browser"
    elapsed: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.state in (PageState.CONTENT, PageState.EMPTY)


@dataclass(slots=True)
class CaptchaChallenge:
    """Описание капчи, которую нужно решить."""

    variant: CaptchaVariant
    page_url: str
    captcha_id: str | None = None
    sitekey: str | None = None
    image_bytes: bytes | None = None
    screenshot: bytes | None = None
    detected_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class SolveResult:
    solved: bool
    method: str = "none"          # ocr-local | slider-cv | manual | skipped
    attempts: int = 0
    detail: str = ""
    answer: str | None = None