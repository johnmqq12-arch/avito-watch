"""Определение состояния страницы: контент / proof-of-work / капча / блок / пусто.

Признаки проверяются в порядке надёжности. Два важных правила:
  * форму капчи ищем как ЭЛЕМЕНТ (<form ... js-firewall-form>), а не как подстроку:
    скрипт страницы файрвола упоминает .js-firewall-form в querySelector всегда;
  * слово «captcha» в HTML ничего не значит — его добавляют сторонние скрипты.
"""
from __future__ import annotations

import re

from ..models import PageState

WALL_TITLE = "Доступ ограничен"
WAIT_TEXT = "подождите немного и обновите страницу"

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_FORM_RE = re.compile(r"<form\b[^>]*js-firewall-form", re.I)
_CARD_RE = re.compile(r'data-marker=["\']item["\'][^>]*data-item-id=', re.I)
_CARD_RE2 = re.compile(r'data-item-id=["\']\d+["\'][^>]*data-marker=["\']item["\']', re.I)
_ITEM_RE = re.compile(r'__staticRouterHydrationData|data-marker=["\']item-view/', re.I)
_POW_RE = re.compile(r"firewallPow|proof-of-work|challenge-platform", re.I)
_EMPTY_RE = re.compile(
    r"Ничего не найдено|Объявлений не найдено|Попробуйте изменить параметры поиска|"
    r"Нет объявлений|ничего не нашлось",
    re.I,
)
_LISTING_ROOT_RE = re.compile(r'data-marker=["\'](catalog|search-core|items/list)["\']', re.I)


def page_title(html: str) -> str:
    m = _TITLE_RE.search(html or "")
    return m.group(1).strip() if m else ""


def is_firewall(html: str) -> bool:
    return WALL_TITLE in page_title(html) or bool(_FORM_RE.search(html or ""))


def has_cards(html: str) -> bool:
    return bool(_CARD_RE.search(html or "") or _CARD_RE2.search(html or ""))


def has_firewall_form(html: str) -> bool:
    return bool(_FORM_RE.search(html or ""))


def detect_state(html: str, *, http_status: int = 200, is_item_page: bool = False) -> PageState:
    html = html or ""
    title = page_title(html)
    wall = WALL_TITLE in title

    if wall or has_firewall_form(html):
        if has_firewall_form(html):
            return PageState.CAPTCHA
        if WAIT_TEXT.lower() in html.lower():
            return PageState.BLOCKED
        return PageState.CHALLENGE

    if is_item_page:
        return PageState.CONTENT if _ITEM_RE.search(html) else PageState.UNKNOWN

    if has_cards(html):
        return PageState.CONTENT
    if _EMPTY_RE.search(html) or _LISTING_ROOT_RE.search(html):
        return PageState.EMPTY
    if http_status in (429, 439) or _POW_RE.search(html):
        return PageState.CHALLENGE
    if http_status >= 500:
        return PageState.UNKNOWN
    if len(html) < 2000:
        return PageState.UNKNOWN
    return PageState.EMPTY if http_status == 200 else PageState.UNKNOWN