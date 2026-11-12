"""Движок на Playwright.

Почему браузер, а не requests: файрвол Авито отдаёт страницу proof-of-work
(HTTP 439), которую проходит только JS. Плюс сессия (cookies файрвола) должна
жить между запусками — поэтому persistent context, а не новый браузер каждый раз.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import BrowserConfig
from ..models import FetchResult, PageState
from ..parser.state import detect_state
from .base import CaptchaHandler, Engine, PageAdapter
from .stealth import HUMANIZE_JS, STEALTH_JS

log = logging.getLogger(__name__)

_BLOCKED_TYPES = {"image", "media", "font"}
_DEFAULT_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--metrics-recording-only",
    "--no-service-autorun",
    "--password-store=basic",
    "--lang=ru-RU",
]


class PlaywrightPage(PageAdapter):
    """Живая страница: нужна решателям капчи (скриншоты, клики, drag, evaluate)."""

    supports_interaction = True

    def __init__(self, page: Any):
        self._page = page

    @property
    def raw(self) -> Any:
        return self._page

    @property
    def url(self) -> str:
        try:
            return self._page.url
        except Exception:
            return ""

    async def html(self) -> str:
        for _ in range(3):
            try:
                return await self._page.content()
            except Exception as exc:  # page is navigating
                log.debug("content() во время навигации: %s", exc)
                await asyncio.sleep(0.7)
        return ""

    async def screenshot(self, *, full_page: bool = False) -> bytes | None:
        try:
            return await self._page.screenshot(full_page=full_page, type="png", timeout=15000)
        except Exception as exc:
            log.warning("Не удалось сделать скриншот: %s", exc)
            return None

    async def element_screenshot(self, selector: str) -> bytes | None:
        try:
            el = await self._page.query_selector(selector)
            if not el:
                return None
            return await el.screenshot(type="png", timeout=10000)
        except Exception as exc:
            log.debug("element_screenshot(%s): %s", selector, exc)
            return None

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        try:
            return await self._page.evaluate(script, arg)
        except Exception as exc:
            log.debug("evaluate: %s", exc)
            return None

    async def bounding_box(self, selector: str) -> dict[str, float] | None:
        try:
            el = await self._page.query_selector(selector)
            if not el:
                return None
            return await el.bounding_box()
        except Exception:
            return None

    async def is_visible(self, selector: str) -> bool:
        try:
            el = await self._page.query_selector(selector)
            return bool(el and await el.is_visible())
        except Exception:
            return False

    async def click(self, selector: str) -> bool:
        try:
            el = await self._page.query_selector(selector)
            if not el or not await el.is_visible():
                return False
            box = await el.bounding_box()
            if box:
                # клик не в центр, а в случайную точку внутри элемента
                x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
                y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
                await self._page.mouse.move(x, y, steps=random.randint(5, 15))
                await asyncio.sleep(random.uniform(0.05, 0.2))
                await self._page.mouse.click(x, y, delay=random.uniform(40, 110))
            else:
                await el.click(timeout=5000)
            return True
        except Exception as exc:
            log.debug("click(%s): %s", selector, exc)
            return False

    async def fill(self, selector: str, value: str) -> bool:
        try:
            el = await self._page.query_selector(selector)
            if not el:
                return False
            await el.click()
            await el.fill("")
            for ch in value:  # посимвольно, с человеческими паузами
                await self._page.keyboard.type(ch, delay=random.uniform(60, 180))
            return True
        except Exception as exc:
            log.debug("fill(%s): %s", selector, exc)
            return False

    async def drag(self, start: tuple[float, float], distance_px: float, *,
                   duration: float = 1.0) -> bool:
        from ..captcha.trajectory import human_track, press_delays

        try:
            mouse = self._page.mouse
            await mouse.move(start[0], start[1], steps=random.randint(6, 14))
            await asyncio.sleep(random.uniform(0.08, 0.25))
            down_delay, up_delay = press_delays()
            await mouse.down()
            await asyncio.sleep(down_delay)
            for point in human_track(start[0], start[1], distance_px, duration=duration):
                await asyncio.sleep(point.delay)
                await mouse.move(point.x, point.y)
            await asyncio.sleep(up_delay)
            await mouse.up()
            return True
        except Exception as exc:
            log.warning("drag не удался: %s", exc)
            return False

    async def reload(self) -> None:
        try:
            await self._page.reload(wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            log.debug("reload: %s", exc)

    async def submit_firewall_form(self, payload: str | None = None,
                                   input_selector: str = "input[name=captcha-response]") -> bool:
        """Отдаём решение так же, как это делает код самой страницы файрвола."""
        script = """([value, inputSelector]) => {
            const form = document.querySelector('form.js-firewall-form')
                      || document.querySelector('.js-firewall-form');
            if (!form) return false;
            if (value !== null) {
                const inp = form.querySelector(inputSelector) || document.querySelector(inputSelector);
                if (!inp) return false;
                inp.value = value;
            }
            form.dispatchEvent(new Event('submit', {cancelable: true, bubbles: true}));
            return true;
        }"""
        return bool(await self.evaluate(script, [payload, input_selector]))


class BrowserEngine(Engine):
    name = "browser"

    def __init__(self, cfg: BrowserConfig, *, session_path: str = "data/session.json"):
        self.cfg = cfg
        self.session_path = session_path
        self._pw: Any = None
        self._ctx: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()
        self._allow_images = False
        self._pages_fetched = 0
        self._started_at = 0.0
        self.last_state: PageState = PageState.UNKNOWN

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._ctx is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Playwright не установлен. Выполните: pip install playwright && playwright install chromium"
            ) from exc

        self._pw = await async_playwright().start()
        args = list(_DEFAULT_ARGS) + list(self.cfg.extra_args)
        # в контейнере под root песочница Chromium не поднимается
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args += ["--no-sandbox", "--disable-setuid-sandbox"]
        proxy = self._proxy_settings()

        if self.cfg.cdp_endpoint:
            log.info("Подключаюсь к внешнему браузеру по CDP")
            browser = await self._pw.chromium.connect_over_cdp(self.cfg.cdp_endpoint)
            self._ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        else:
            launch_kwargs: dict[str, Any] = dict(
                user_data_dir=str(Path(self.cfg.user_data_dir).resolve()),
                headless=self.cfg.headless,
                args=args,
                ignore_default_args=["--enable-automation"],
                locale=self.cfg.locale,
                timezone_id=self.cfg.timezone,
                viewport={"width": self.cfg.viewport_width, "height": self.cfg.viewport_height},
                accept_downloads=False,
                device_scale_factor=1,
            )
            if self.cfg.user_agent:
                launch_kwargs["user_agent"] = self.cfg.user_agent
            if self.cfg.chrome_channel and self.cfg.chrome_channel != "chromium":
                launch_kwargs["channel"] = self.cfg.chrome_channel
            if proxy:
                launch_kwargs["proxy"] = proxy
            self._ctx = await self._pw.chromium.launch_persistent_context(**launch_kwargs)

        self._ctx.set_default_timeout(self.cfg.nav_timeout_sec * 1000)
        await self._ctx.add_init_script(STEALTH_JS)
        await self._ctx.route("**/*", self._route)
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self._started_at = time.time()
        log.info("Браузер запущен (headless=%s, proxy=%s, профиль=%s)",
                 self.cfg.headless, bool(proxy), self.cfg.user_data_dir)

    async def stop(self) -> None:
        try:
            if self._ctx is not None:
                await self.save_session(self.session_path)
        except Exception as exc:
            log.debug("save_session: %s", exc)
        for closer in (getattr(self._ctx, "close", None), getattr(self._pw, "stop", None)):
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:
                log.debug("close: %s", exc)
        self._ctx = self._page = self._pw = None

    async def restart(self) -> None:
        log.info("Перезапускаю браузер (профилактика после %d страниц)", self._pages_fetched)
        await self.stop()
        self._pages_fetched = 0
        await self.start()

    def _proxy_settings(self) -> dict[str, str] | None:
        raw = (self.cfg.proxy or "").strip()
        if not raw:
            return None
        if "://" not in raw:
            raw = "http://" + raw
        parsed = urlparse(raw)
        if not parsed.hostname or not parsed.port:
            log.warning("Прокси задан некорректно, игнорирую: %s", self.cfg.proxy)
            return None
        settings = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
        if parsed.username:
            settings["username"] = parsed.username
        if parsed.password:
            settings["password"] = parsed.password
        return settings

    async def _route(self, route: Any) -> None:
        try:
            if self.cfg.block_resources and not self._allow_images:
                if route.request.resource_type in _BLOCKED_TYPES:
                    await route.abort()
                    return
            await route.continue_()
        except Exception:
            pass

    async def save_session(self, path: str) -> None:
        if self._ctx is None:
            return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        await self._ctx.storage_state(path=path)

    async def health(self) -> dict[str, Any]:
        return {
            "engine": self.name,
            "running": self._ctx is not None,
            "headless": self.cfg.headless,
            "pages_fetched": self._pages_fetched,
            "uptime_sec": round(time.time() - self._started_at, 1) if self._started_at else 0,
            "proxy": bool(self.cfg.proxy),
            "last_state": self.last_state.value,
        }

    # ---------------------------------------------------------------- fetch
    async def fetch(self, url: str, *, is_item: bool = False, source_id: int | None = None,
                    captcha_handler: CaptchaHandler | None = None) -> FetchResult:
        if self._ctx is None:
            await self.start()
        if self.cfg.restart_after_pages and self._pages_fetched >= self.cfg.restart_after_pages:
            await self.restart()

        async with self._lock:  # Авито считает частоту по IP: параллелить вредно
            started = time.time()
            self._allow_images = False
            page = self._page
            status = 0
            error: str | None = None
            try:
                resp = await page.goto(url, wait_until="domcontentloaded",
                                       timeout=self.cfg.nav_timeout_sec * 1000)
                status = resp.status if resp else 0
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                log.warning("Навигация %s: %s", url, error)

            if self.cfg.humanize:
                await self._humanize(page)

            html = await self._safe_content(page)
            state = detect_state(html, http_status=status, is_item_page=is_item)
            captcha_attempts = 0

            # proof-of-work: ждём, НЕ перезагружаем (иначе проверка начнётся заново)
            if state is PageState.CHALLENGE:
                state, html = await self._wait_pow(page, status, is_item)

            while state is PageState.CAPTCHA and captcha_handler is not None:
                captcha_attempts += 1
                self._allow_images = True   # для капчи картинки нужны
                result = await captcha_handler(PlaywrightPage(page), source_id)
                if not result.solved:
                    break
                await self._settle(page)
                html = await self._safe_content(page)
                state = detect_state(html, http_status=200, is_item_page=is_item)
                if state is PageState.CAPTCHA and captcha_attempts >= 3:
                    break

            if state is PageState.UNKNOWN and error is None:
                await asyncio.sleep(2.0)
                html = await self._safe_content(page)
                state = detect_state(html, http_status=status, is_item_page=is_item)

            self._pages_fetched += 1
            self.last_state = state
            elapsed = time.time() - started
            log.info("Загружено %s → %s (%.1fs, %d байт)", url, state.value, elapsed, len(html))
            return FetchResult(url=url, html=html, status=status, state=state,
                               engine=self.name, elapsed=elapsed, error=error)

    async def _wait_pow(self, page: Any, status: int, is_item: bool) -> tuple[PageState, str]:
        deadline = time.time() + self.cfg.pow_wait_sec
        html = await self._safe_content(page)
        while time.time() < deadline:
            await asyncio.sleep(1.5)
            html = await self._safe_content(page)
            state = detect_state(html, http_status=200, is_item_page=is_item)
            if state is not PageState.CHALLENGE:
                log.info("Proof-of-work пройден браузером самостоятельно")
                return state, html
        log.warning("Proof-of-work не завершился за %.0fs", self.cfg.pow_wait_sec)
        return PageState.CHALLENGE, html

    async def _settle(self, page: Any) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(1.2)

    async def _humanize(self, page: Any) -> None:
        try:
            await page.mouse.move(random.randint(200, 900), random.randint(150, 600),
                                  steps=random.randint(8, 20))
            await page.evaluate(HUMANIZE_JS)
            await asyncio.sleep(random.uniform(0.4, 1.3))
        except Exception as exc:
            log.debug("humanize: %s", exc)

    async def _safe_content(self, page: Any) -> str:
        for _ in range(4):
            try:
                return await page.content()
            except Exception as exc:
                log.debug("content(): %s", exc)
                await asyncio.sleep(0.8)
        return ""

    async def open_manual(self, url: str, *, seconds: float = 600) -> None:
        """Прогрев профиля вручную: headed-браузер, вы проходите капчу/логин сами."""
        await self.start()
        await self._page.goto(url, wait_until="domcontentloaded")
        deadline = time.time() + seconds
        while time.time() < deadline:
            await asyncio.sleep(3)
            html = await self._safe_content(self._page)
            state = detect_state(html)
            if state in (PageState.CONTENT, PageState.EMPTY):
                log.info("Профиль прошёл файрвол, сохраняю сессию")
                await self.save_session(self.session_path)
                return
        await self.save_session(self.session_path)