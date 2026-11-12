"""Определение типа капчи на странице файрвола.

В коде страницы есть три блока; сервер показывает один, остальные скрыты
(display:none). Смотрим именно видимость элемента, а не наличие слова в HTML.
"""
from __future__ import annotations

import base64
import logging
import re

from bs4 import BeautifulSoup

from ..engine.base import PageAdapter
from ..models import CaptchaChallenge, CaptchaVariant

log = logging.getLogger(__name__)

CAPTCHA_ID_RE = re.compile(r"captchaId\s*=\s*['\"]([0-9a-f]{32})['\"]", re.I)
SITEKEY_RE = re.compile(r'data-sitekey=["\']([0-9a-zA-Z\-_]{20,})["\']')
DATA_URL_RE = re.compile(r"data:image/(?:png|jpe?g|gif|webp);base64,([A-Za-z0-9+/=\s]+)")

VARIANT_SELECTORS: dict[CaptchaVariant, tuple[str, ...]] = {
    CaptchaVariant.GEETEST: ("#geetest_captcha", "#geetest-captcha", ".geetest_holder",
                             ".geetest_widget", "[id*=geetest]"),
    CaptchaVariant.IMAGE: ("#inner-captcha", "#inner_captcha", ".js-inner-captcha"),
    CaptchaVariant.HCAPTCHA: ("#h-captcha", "#hcaptcha", ".h-captcha"),
}


def _hidden(node) -> bool:
    cur = node
    while cur is not None and getattr(cur, "name", None):
        style = (cur.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style or cur.has_attr("hidden"):
            return True
        classes = " ".join(cur.get("class") or [])
        if "hidden" in classes.split():
            return True
        cur = cur.parent
    return False


def detect_variant(html: str) -> CaptchaVariant:
    soup = BeautifulSoup(html or "", "lxml")
    for variant, selectors in VARIANT_SELECTORS.items():
        for selector in selectors:
            for node in soup.select(selector):
                if not _hidden(node):
                    return variant
    # если блоки не найдены, но форма есть — считаем «неизвестной» капчей:
    # решаем вручную, но пользователя уведомляем обязательно
    if soup.select_one("form.js-firewall-form, .js-firewall-form"):
        for variant, selectors in VARIANT_SELECTORS.items():
            for selector in selectors:
                if soup.select_one(selector):
                    return variant
    return CaptchaVariant.NONE


def extract_captcha_id(html: str) -> str | None:
    m = CAPTCHA_ID_RE.search(html or "")
    return m.group(1) if m else None


def extract_sitekey(html: str) -> str | None:
    m = SITEKEY_RE.search(html or "")
    return m.group(1) if m else None


def extract_inline_image(html: str) -> bytes | None:
    """Капча-картинка Авито приходит как data: URL прямо в разметке."""
    soup = BeautifulSoup(html or "", "lxml")
    for selector in ("#inner-captcha img", "#inner_captcha img", ".js-inner-captcha img",
                     "form.js-firewall-form img"):
        node = soup.select_one(selector)
        if node and (node.get("src") or "").startswith("data:image"):
            return _decode_data_url(node["src"])
    m = DATA_URL_RE.search(html or "")
    return _decode_data_url(m.group(0)) if m else None


def _decode_data_url(value: str) -> bytes | None:
    m = DATA_URL_RE.match(value.strip())
    if not m:
        return None
    try:
        return base64.b64decode(re.sub(r"\s+", "", m.group(1)))
    except (ValueError, TypeError):
        return None


async def build_challenge(page: PageAdapter, html: str | None = None) -> CaptchaChallenge:
    html = html if html is not None else await page.html()
    variant = detect_variant(html)
    challenge = CaptchaChallenge(
        variant=variant,
        page_url=page.url,
        captcha_id=extract_captcha_id(html),
        sitekey=extract_sitekey(html),
        image_bytes=extract_inline_image(html) if variant is CaptchaVariant.IMAGE else None,
    )
    if variant is CaptchaVariant.IMAGE and not challenge.image_bytes and page.supports_interaction:
        for selector in ("#inner-captcha img", "#inner_captcha img", "form.js-firewall-form img"):
            shot = await page.element_screenshot(selector)
            if shot:
                challenge.image_bytes = shot
                break
    if page.supports_interaction:
        challenge.screenshot = await page.screenshot()
    log.info("Капча: вариант=%s captcha_id=%s", variant.value, challenge.captcha_id)
    return challenge