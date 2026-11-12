"""Клиент Telegram Bot API на httpx: без лишних зависимостей и магии.

Умеет: фото с подписью, кнопки, ретраи на 429, тихие часы, long polling для команд.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

import httpx

from ..config import TelegramConfig
from ..models import Listing
from ..utils import MSK
from .formatting import build_caption, build_price_drop_caption

log = logging.getLogger(__name__)

API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, cfg: TelegramConfig, *, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._client = client
        self._owns_client = client is None
        self._send_lock = asyncio.Lock()
        self._last_send = 0.0
        self.sent_count = 0
        self.error_count = 0
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.bot_token and self.cfg.chat_id)

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.cfg.request_timeout)

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    # ------------------------------------------------------------------ low level
    async def api(self, method: str, payload: dict[str, Any] | None = None, *,
                  files: dict[str, Any] | None = None, retries: int = 3) -> dict[str, Any]:
        if not self.cfg.bot_token:
            raise TelegramError("Не задан токен бота")
        await self.start()
        url = f"{API}/bot{self.cfg.bot_token}/{method}"
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                if files:
                    resp = await self._client.post(url, data=payload or {}, files=files)
                else:
                    resp = await self._client.post(url, json=payload or {})
                data = resp.json()
                if data.get("ok"):
                    self.error_count = 0
                    return data.get("result", {})
                code = data.get("error_code")
                description = str(data.get("description", ""))
                if code == 429:
                    retry_after = float((data.get("parameters") or {}).get("retry_after", 3))
                    log.warning("Telegram 429, жду %.1fs", retry_after)
                    await asyncio.sleep(retry_after + 0.5)
                    continue
                if code and 500 <= code < 600:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise TelegramError(f"{method}: {code} {description}")
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                last_exc = exc
                await asyncio.sleep(1.5 * (attempt + 1))
        self.error_count += 1
        self.last_error = str(last_exc) if last_exc else "unknown"
        raise TelegramError(f"{method}: сеть недоступна после {retries} попыток ({last_exc})")

    async def get_me(self) -> dict[str, Any]:
        return await self.api("getMe")

    async def _throttle(self) -> None:
        async with self._send_lock:
            delta = time.monotonic() - self._last_send
            if delta < 1.05:
                await asyncio.sleep(1.05 - delta)
            self._last_send = time.monotonic()

    def _silent(self) -> bool:
        window = (self.cfg.silent_hours or "").strip()
        if not window or "-" not in window:
            return False
        try:
            start_s, end_s = window.split("-", 1)
            start = datetime.strptime(start_s.strip(), "%H:%M").time()
            end = datetime.strptime(end_s.strip(), "%H:%M").time()
        except ValueError:
            return False
        current = datetime.now(MSK).time()
        return start <= current or current < end if start > end else start <= current < end

    # ------------------------------------------------------------------ messages
    async def send_text(self, text: str, *, chat_id: str | None = None,
                        reply_markup: dict | None = None, silent: bool | None = None,
                        preview: bool = False) -> int | None:
        if not self.enabled:
            log.info("[telegram off] %s", text[:200])
            return None
        await self._throttle()
        payload: dict[str, Any] = {
            "chat_id": chat_id or self.cfg.chat_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": not preview,
            "disable_notification": self._silent() if silent is None else silent,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            result = await self.api("sendMessage", payload)
            self.sent_count += 1
            return result.get("message_id")
        except TelegramError as exc:
            log.error("sendMessage: %s", exc)
            return None

    async def send_photo(self, photo: str | bytes | None, caption: str, *,
                         chat_id: str | None = None, reply_markup: dict | None = None,
                         silent: bool | None = None) -> int | None:
        if not self.enabled:
            log.info("[telegram off] %s", caption[:200])
            return None
        if not photo or not self.cfg.send_photos:
            return await self.send_text(caption, chat_id=chat_id, reply_markup=reply_markup,
                                        silent=silent, preview=True)
        await self._throttle()
        base: dict[str, Any] = {
            "chat_id": chat_id or self.cfg.chat_id,
            "caption": caption[:1024],
            "parse_mode": "HTML",
            "disable_notification": self._silent() if silent is None else silent,
        }
        if reply_markup:
            base["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        try:
            if isinstance(photo, bytes):
                result = await self.api("sendPhoto", base, files={"photo": ("image.jpg", photo, "image/jpeg")})
            else:
                try:
                    result = await self.api("sendPhoto", {**base, "photo": photo}, retries=1)
                except TelegramError as exc:
                    log.debug("Telegram не смог забрать фото по URL (%s), грузим байтами", exc)
                    data = await self._download(photo)
                    if not data:
                        return await self.send_text(caption, chat_id=chat_id,
                                                    reply_markup=reply_markup, silent=silent, preview=True)
                    result = await self.api("sendPhoto", base,
                                            files={"photo": ("image.jpg", data, "image/jpeg")})
            self.sent_count += 1
            return result.get("message_id")
        except TelegramError as exc:
            log.error("sendPhoto: %s", exc)
            return await self.send_text(caption, chat_id=chat_id, reply_markup=reply_markup,
                                         silent=silent, preview=True)

    async def _download(self, url: str) -> bytes | None:
        try:
            await self.start()
            resp = await self._client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Referer": "https://www.avito.ru/",
            }, timeout=25, follow_redirects=True)
            if resp.status_code == 200 and resp.content:
                return resp.content
        except httpx.HTTPError as exc:
            log.debug("Скачивание фото %s: %s", url, exc)
        return None

    async def send_listing(self, listing: Listing, *, source_label: str = "",
                           kind: str = "new", old_price: int | None = None) -> bool:
        caption = (build_price_drop_caption(listing, old_price, source_label)
                   if kind == "price_drop" else build_caption(listing, source_label, self.cfg))
        keyboard = {"inline_keyboard": [[{"text": "🔗 Открыть на Авито", "url": listing.url}]]}
        message_id = await self.send_photo(listing.image_url, caption, reply_markup=keyboard)
        if message_id is None:
            return False
        if self.cfg.full_description and listing.description and \
                len(listing.description) > self.cfg.description_limit:
            from ..utils import clean_text
            await self.send_text(f"<b>Полное описание</b>\n{clean_text(listing.description)[:3500]}")
        return True

    # ------------------------------------------------------------------ polling
    async def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        try:
            return await self.api("getUpdates", payload, retries=1)
        except TelegramError as exc:
            log.debug("getUpdates: %s", exc)
            await asyncio.sleep(3)
            return []

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            await self.api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})
        except TelegramError as exc:
            log.debug("answerCallbackQuery: %s", exc)