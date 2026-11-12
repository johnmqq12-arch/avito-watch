"""Облегчённый HTTP-движок (без Playwright) — экспериментальный.

Честно о ограничениях: файрвол Авито отдаёт proof-of-work, который требует JS,
поэтому «голым» HTTP пройти его нельзя. Движок имеет смысл только как ускоритель:
сессию (cookies) один раз прогревает браузер (`python -m avito_watch warmup`),
дальше часть страниц можно читать по HTTP с теми же cookies и TLS-отпечатком Chrome.
Как только файрвол снова поднимется — планировщик сам вернётся к браузеру.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from ..config import BrowserConfig
from ..models import FetchResult, PageState
from ..parser.state import detect_state
from .base import CaptchaHandler, Engine, PageAdapter

log = logging.getLogger(__name__)


class StaticPage(PageAdapter):
    """Страница без интерактива: капчу тут решить нельзя, только показать пользователю."""

    supports_interaction = False

    def __init__(self, url: str, html: str):
        self.url = url
        self._html = html

    async def html(self) -> str:
        return self._html

    async def screenshot(self, *, full_page: bool = False) -> bytes | None:
        return None


class HttpEngine(Engine):
    name = "http"

    def __init__(self, cfg: BrowserConfig, *, session_path: str = "data/session.json"):
        self.cfg = cfg
        self.session_path = session_path
        self._client: Any = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._client is not None:
            return
        try:
            from curl_cffi.requests import AsyncSession  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Для engine=http нужен curl_cffi: pip install curl_cffi "
                "(или оставьте engine=browser)"
            ) from exc
        proxies = {"https": self.cfg.proxy, "http": self.cfg.proxy} if self.cfg.proxy else None
        self._client = AsyncSession(impersonate="chrome", timeout=self.cfg.nav_timeout_sec,
                                    proxies=proxies, verify=True)
        self._load_cookies()
        log.info("HTTP-движок запущен (cookies из %s)", self.session_path)

    def _load_cookies(self) -> None:
        path = Path(self.session_path)
        if not path.exists():
            log.warning("Нет файла сессии %s — сначала выполните warmup браузером", path)
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            log.warning("Не читается сессия: %s", exc)
            return
        for cookie in data.get("cookies", []):
            try:
                self._client.cookies.set(cookie["name"], cookie["value"],
                                         domain=cookie.get("domain", ".avito.ru"))
            except Exception:
                continue

    async def stop(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
        self._client = None

    async def fetch(self, url: str, *, is_item: bool = False, source_id: int | None = None,
                    captcha_handler: CaptchaHandler | None = None) -> FetchResult:
        if self._client is None:
            await self.start()
        async with self._lock:
            started = time.time()
            html, status, error = "", 0, None
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
                "Referer": "https://www.avito.ru/",
                "Upgrade-Insecure-Requests": "1",
            }
            try:
                resp = await self._client.get(url, headers=headers)
                status, html = resp.status_code, resp.text
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            state = detect_state(html, http_status=status, is_item_page=is_item)
            if state is PageState.CAPTCHA and captcha_handler is not None:
                await captcha_handler(StaticPage(url, html), source_id)
            return FetchResult(url=url, html=html, status=status, state=state, engine=self.name,
                               elapsed=time.time() - started, error=error)

    async def health(self) -> dict[str, Any]:
        return {"engine": self.name, "running": self._client is not None,
                "session_file": self.session_path, "experimental": True}