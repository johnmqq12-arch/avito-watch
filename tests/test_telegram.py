"""Telegram: формат сообщений, ретраи, фолбэк с URL на байты, кнопки."""
import json

import httpx
import pytest

from avito_watch.config import TelegramConfig
from avito_watch.models import Listing
from avito_watch.notify.formatting import build_caption, captcha_keyboard, captcha_message
from avito_watch.notify.telegram import Telegram
from avito_watch.utils import now


def make_listing(**kwargs) -> Listing:
    base = dict(item_id="7654321098", url="https://www.avito.ru/moskva/x_7654321098",
                title="iPhone 15 Pro 256 ГБ", price=62990, price_text="62 990 ₽",
                old_price=69989, discount_pct=10,
                image_url="https://00.img.avito.st/image/1/a.jpg",
                description="Продаю iPhone 15 Pro. " * 40,
                published_at=now(), location="Москва, м. Авиамоторная")
    base.update(kwargs)
    return Listing(**base)


def test_caption_contains_required_fields():
    cfg = TelegramConfig()
    caption = build_caption(make_listing(), "iPhone Москва", cfg)
    assert len(caption) <= 1024
    assert "62 990 ₽" in caption
    assert "69 989 ₽" in caption          # старая цена зачёркнута
    assert "−10%" in caption
    assert "Москва" in caption
    assert "iPhone 15 Pro" in caption
    assert "https://www.avito.ru/moskva/x_7654321098" in caption
    assert "🕒" in caption                 # дата публикации
    assert "📝" in caption                 # описание


def test_caption_escapes_html():
    caption = build_caption(make_listing(title='<script>alert("x")</script>'), "s", TelegramConfig())
    assert "<script>" not in caption
    assert "&lt;script&gt;" in caption


def test_caption_without_price_and_description():
    caption = build_caption(make_listing(price=None, price_text="Договорная", description=None,
                                         old_price=None, discount_pct=None), "s", TelegramConfig())
    assert "Договорная" in caption
    assert "описание на странице" in caption


def test_captcha_message_and_keyboard():
    text = captcha_message(source_label="iPhone", variant="geetest", auto_tried=True,
                           auto_detail="GeeTest отклонил", manual_url="http://localhost:6080/vnc.html",
                           image_captcha=False)
    assert "капчу" in text.lower()
    assert "GeeTest отклонил" in text
    keyboard = captcha_keyboard(event_id=7, source_id=3, manual_url="http://localhost:6080")
    flat = [b for row in keyboard["inline_keyboard"] for b in row]
    assert any(b.get("url") == "http://localhost:6080" for b in flat)
    assert any(b.get("callback_data") == "cap:done:7" for b in flat)
    assert any(b.get("callback_data") == "src:pause:3" for b in flat)


async def test_send_photo_falls_back_to_bytes():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/sendPhoto"):
            body = request.content
            if b"application/json" in request.headers.get("content-type", "").encode():
                payload = json.loads(body)
                calls.append("sendPhoto:url")
                assert payload["photo"].startswith("https://")
                return httpx.Response(200, json={"ok": False, "error_code": 400,
                                                 "description": "Bad Request: failed to get HTTP URL content"})
            calls.append("sendPhoto:upload")
            assert b"image.jpg" in body
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})
        if request.url.host == "00.img.avito.st":
            calls.append("download")
            return httpx.Response(200, content=b"\xff\xd8\xff fake jpeg")
        raise AssertionError(f"неожиданный запрос {request.url}")

    cfg = TelegramConfig(bot_token="1:T", chat_id="1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        telegram = Telegram(cfg, client=client)
        message_id = await telegram.send_photo("https://00.img.avito.st/image/1/a.jpg", "подпись")
    assert message_id == 42
    assert calls == ["sendPhoto:url", "download", "sendPhoto:upload"]


async def test_retry_on_429():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(200, json={"ok": False, "error_code": 429,
                                             "parameters": {"retry_after": 0},
                                             "description": "Too Many Requests"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    cfg = TelegramConfig(bot_token="1:T", chat_id="1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        telegram = Telegram(cfg, client=client)
        assert await telegram.send_text("привет") == 1
    assert attempts["n"] == 2


async def test_disabled_telegram_is_noop():
    telegram = Telegram(TelegramConfig(bot_token="", chat_id=""))
    assert telegram.enabled is False
    assert await telegram.send_text("тест") is None
    assert await telegram.send_listing(make_listing()) is False


async def test_send_listing_adds_button():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    cfg = TelegramConfig(bot_token="1:T", chat_id="1", send_photos=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        telegram = Telegram(cfg, client=client)
        assert await telegram.send_listing(make_listing(), source_label="iPhone") is True
    keyboard = captured["reply_markup"]["inline_keyboard"]
    assert keyboard[0][0]["url"].endswith("7654321098")