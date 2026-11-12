"""Движки загрузки: Playwright-адаптер страницы, прокси, HTTP-режим.

Без реального браузера: подставляем фейковую «живую» страницу с тем же интерфейсом,
что и playwright.async_api.Page — так проверяем нашу логику, а не Chromium.
"""
import json

import pytest

from avito_watch.config import BrowserConfig
from avito_watch.engine.browser import BrowserEngine, PlaywrightPage
from avito_watch.engine.http import HttpEngine, StaticPage
from avito_watch.engine.stealth import HUMANIZE_JS, STEALTH_JS
from avito_watch.models import PageState
from tests.conftest import load_html


class FakeMouse:
    def __init__(self):
        self.moves: list[tuple[float, float]] = []
        self.downs = 0
        self.ups = 0
        self.clicks: list[tuple[float, float]] = []

    async def move(self, x, y, steps=None):
        self.moves.append((x, y))

    async def down(self):
        self.downs += 1

    async def up(self):
        self.ups += 1

    async def click(self, x, y, delay=None):
        self.clicks.append((x, y))


class FakeKeyboard:
    def __init__(self):
        self.typed: list[str] = []

    async def type(self, text, delay=None):
        self.typed.append(text)


class FakeElement:
    def __init__(self, page, selector):
        self._page = page
        self._selector = selector

    async def is_visible(self):
        return self._selector in self._page.visible

    async def bounding_box(self):
        return self._page.boxes.get(self._selector)

    async def screenshot(self, **kwargs):
        return b"element-png"

    async def click(self, **kwargs):
        self._page.clicked.append(self._selector)

    async def fill(self, value):
        self._page.filled.append((self._selector, value))


class FakeRawPage:
    """Мини-копия playwright Page: ровно то, что использует наш адаптер."""

    def __init__(self, html: str, *, url: str = "https://www.avito.ru/moskva?q=iphone",
                 content_errors: int = 0, status: int = 200):
        self._html = html
        self.url = url
        self._content_errors = content_errors
        self._status = status
        self.mouse = FakeMouse()
        self.keyboard = FakeKeyboard()
        self.visible: set[str] = set()
        self.boxes: dict[str, dict] = {}
        self.clicked: list[str] = []
        self.filled: list[tuple[str, str]] = []
        self.evaluated: list[tuple[str, object]] = []
        self.goto_calls: list[str] = []
        self.reloads = 0
        self.eval_result = True

    async def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        return type("Resp", (), {"status": self._status})()

    async def content(self):
        if self._content_errors > 0:
            self._content_errors -= 1
            raise RuntimeError("Execution context was destroyed")
        return self._html

    async def screenshot(self, **kwargs):
        return b"page-png"

    async def query_selector(self, selector):
        return FakeElement(self, selector)

    async def evaluate(self, script, arg=None):
        self.evaluated.append((script, arg))
        return self.eval_result

    async def reload(self, **kwargs):
        self.reloads += 1

    async def wait_for_load_state(self, *args, **kwargs):
        return None


# ------------------------------------------------------------------ прокси
@pytest.mark.parametrize("raw,expected", [
    ("http://user:pass@1.2.3.4:8080", {"server": "http://1.2.3.4:8080",
                                       "username": "user", "password": "pass"}),
    ("5.6.7.8:3128", {"server": "http://5.6.7.8:3128"}),
    ("", None),
    ("мусор", None),
])
def test_proxy_settings(raw, expected):
    engine = BrowserEngine(BrowserConfig(proxy=raw))
    assert engine._proxy_settings() == expected


# ------------------------------------------------------------------ адаптер страницы
async def test_page_html_retries_during_navigation():
    raw = FakeRawPage(load_html("listing_state"), content_errors=2)
    page = PlaywrightPage(raw)
    assert "7654321098" in await page.html()


async def test_page_html_gives_up_quietly():
    raw = FakeRawPage("", content_errors=10)
    assert await PlaywrightPage(raw).html() == ""


async def test_page_screenshot_and_visibility():
    raw = FakeRawPage(load_html("firewall_captcha"))
    raw.visible = {".geetest_slider_button"}
    raw.boxes[".geetest_slider_button"] = {"x": 10.0, "y": 20.0, "width": 40.0, "height": 40.0}
    page = PlaywrightPage(raw)
    assert await page.screenshot() == b"page-png"
    assert await page.element_screenshot(".geetest_bg") == b"element-png"
    assert await page.is_visible(".geetest_slider_button") is True
    assert await page.is_visible(".nope") is False
    assert (await page.bounding_box(".geetest_slider_button"))["width"] == 40.0


async def test_click_lands_inside_element_not_dead_center():
    raw = FakeRawPage("<html></html>")
    raw.visible = {"button"}
    raw.boxes["button"] = {"x": 100.0, "y": 100.0, "width": 200.0, "height": 60.0}
    page = PlaywrightPage(raw)
    assert await page.click("button") is True
    x, y = raw.mouse.clicks[-1]
    assert 100 < x < 300 and 100 < y < 160
    assert not (x == 200.0 and y == 130.0)      # не строго центр


async def test_fill_types_like_human():
    raw = FakeRawPage("<html></html>")
    page = PlaywrightPage(raw)
    assert await page.fill("#form-input", "ab12") is True
    assert raw.filled == [("#form-input", "")]
    assert raw.keyboard.typed == ["a", "b", "1", "2"]


async def test_submit_firewall_form_sends_answer_to_page_code():
    raw = FakeRawPage(load_html("firewall_image"))
    page = PlaywrightPage(raw)
    assert await page.submit_firewall_form("ab12cd", "#form-input") is True
    script, arg = raw.evaluated[-1]
    assert "js-firewall-form" in script and arg == ["ab12cd", "#form-input"]
    raw.eval_result = False
    assert await page.submit_firewall_form("x") is False


async def test_drag_presses_moves_and_releases():
    raw = FakeRawPage("<html></html>")
    page = PlaywrightPage(raw)
    assert await page.drag((100.0, 200.0), 180.0, duration=0.2) is True
    assert raw.mouse.downs == 1 and raw.mouse.ups == 1
    assert len(raw.mouse.moves) > 30
    assert abs(raw.mouse.moves[-1][0] - 280.0) < 1.0     # доехали до цели


async def test_static_page_cannot_solve_captcha():
    page = StaticPage("https://www.avito.ru/x", load_html("firewall_captcha"))
    assert page.supports_interaction is False
    assert await page.screenshot() is None
    assert await page.fill("#form-input", "abc") is False
    assert await page.submit_firewall_form() is False


# ------------------------------------------------------------------ fetch
async def test_browser_fetch_parses_content_without_launching_browser():
    engine = BrowserEngine(BrowserConfig(humanize=True, page_delay_sec=0))
    raw = FakeRawPage(load_html("listing_state"))
    engine._ctx, engine._page = object(), raw          # браузер уже «запущен»
    result = await engine.fetch("https://www.avito.ru/moskva?q=iphone")
    assert result.state is PageState.CONTENT
    assert result.ok and result.engine == "browser"
    assert engine._pages_fetched == 1
    assert raw.goto_calls == ["https://www.avito.ru/moskva?q=iphone"]
    assert any(HUMANIZE_JS in script for script, _ in raw.evaluated)


async def test_browser_fetch_reports_captcha_and_calls_handler():
    engine = BrowserEngine(BrowserConfig(humanize=False))
    raw = FakeRawPage(load_html("firewall_captcha"))
    engine._ctx, engine._page = object(), raw
    seen: list[int | None] = []

    async def handler(page, source_id):
        from avito_watch.models import SolveResult
        seen.append(source_id)
        assert isinstance(page, PlaywrightPage)
        return SolveResult(solved=False, method="timeout")

    result = await engine.fetch("https://www.avito.ru/x", source_id=7, captcha_handler=handler)
    assert result.state is PageState.CAPTCHA
    assert seen == [7]
    assert engine._allow_images is True      # картинки для капчи разблокированы


async def test_browser_fetch_survives_navigation_error():
    engine = BrowserEngine(BrowserConfig(humanize=False))
    raw = FakeRawPage("")

    async def boom(url, **kwargs):
        raise TimeoutError("Navigation timeout")

    raw.goto = boom
    engine._ctx, engine._page = object(), raw
    result = await engine.fetch("https://www.avito.ru/x")
    assert result.error and "TimeoutError" in result.error
    assert result.state is not PageState.CONTENT


async def test_health_before_start():
    health = await BrowserEngine(BrowserConfig()).health()
    assert health["running"] is False and health["engine"] == "browser"


# ------------------------------------------------------------------ http-движок
async def test_http_engine_loads_warm_cookies(tmp_path):
    session = tmp_path / "session.json"
    session.write_text(json.dumps({"cookies": [
        {"name": "ft", "value": "abc", "domain": ".avito.ru"},
        {"name": "bad"},                      # битая запись не должна ронять загрузку
    ]}), encoding="utf-8")
    engine = HttpEngine(BrowserConfig(), session_path=str(session))

    class Jar:
        def __init__(self):
            self.items = {}

        def set(self, name, value, domain=None):
            self.items[name] = value

    class Client:
        def __init__(self):
            self.cookies = Jar()

        async def get(self, url, headers=None):
            return type("R", (), {"status_code": 200, "text": load_html("listing_dom")})()

        async def close(self):
            pass

    engine._client = Client()
    engine._load_cookies()
    assert engine._client.cookies.items == {"ft": "abc"}
    result = await engine.fetch("https://www.avito.ru/spb?q=trek")
    assert result.state is PageState.CONTENT and result.engine == "http"
    assert (await engine.health())["experimental"] is True
    await engine.stop()


def test_stealth_patches_present():
    assert "webdriver" in STEALTH_JS and "plugins" in STEALTH_JS
    assert len(HUMANIZE_JS) > 50
