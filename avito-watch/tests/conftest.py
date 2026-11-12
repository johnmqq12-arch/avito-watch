"""Общие фикстуры: конфиг во временной папке, БД, фейковые движок и Telegram."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from avito_watch.config import Config          # noqa: E402
from avito_watch.db import Database            # noqa: E402
from avito_watch.engine.base import Engine, PageAdapter  # noqa: E402
from avito_watch.models import FetchResult, Listing, PageState  # noqa: E402
from avito_watch.parser.state import detect_state  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


async def wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02):
    """Ждём условие вместо фиксированного sleep — тесты не зависят от скорости машины."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError(f"условие не выполнилось за {timeout} с")


def load_html(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


def make_card(item_id: str, title: str, price: int | None, image: str = "pic") -> str:
    price_html = (f'<span itemprop="price" content="{price}">{price} ₽</span>'
                  if price is not None else '<span itemprop="price">Цена не указана</span>')
    return f"""<div data-marker="item" data-item-id="{item_id}">
      <a itemprop="url" data-marker="item-title" href="/moskva/tovar_{item_id}"><h3 itemprop="name">{title}</h3></a>
      <img itemprop="image" src="https://00.img.avito.st/image/1/{image}.jpg">
      <div data-marker="item-price">{price_html}</div>
      <span data-marker="item-location">Москва</span>
      <span data-marker="item-date">10 минут назад</span>
    </div>"""


def make_listing_page(cards: list[str]) -> str:
    return ("<!DOCTYPE html><html><head><title>Авито</title></head><body>"
            f"<div data-marker=\"catalog\">{''.join(cards)}</div></body></html>")


@pytest.fixture
def cfg(tmp_path) -> Config:
    config = Config()
    config.config_path = str(tmp_path / "config.yaml")
    config.storage.db_path = str(tmp_path / "test.db")
    config.storage.captcha_dir = str(tmp_path / "captcha")
    config.logging.file = str(tmp_path / "test.log")
    config.browser.user_data_dir = str(tmp_path / "profile")
    config.telegram.bot_token = "123456:TEST"
    config.telegram.chat_id = "100500"
    config.telegram.admin_ids = ["100500"]
    config.captcha.poll_interval_sec = 0.05
    config.captcha.manual_wait_sec = 2
    # все паузы взаимодействия со страницей — минимальные, чтобы тесты шли секунды
    config.captcha.submit_wait_sec = 0.05
    config.captcha.slider_settle_sec = 0.05
    config.captcha.reload_wait_sec = 0.05
    config.captcha.widget_timeout_sec = 0.3
    config.captcha.click_wait_sec = 0.02
    config.captcha.ocr_backend = "none"
    config.watch.enrich_items = False
    config.watch.enrich_delay_sec = 0
    config.browser.page_delay_sec = 0
    config._normalize()
    return config


@pytest.fixture
def db(cfg):
    database = Database(cfg.storage.db_path)
    yield database
    database.close()


class FakeTelegram:
    """Записывает всё, что ушло бы пользователю."""

    def __init__(self):
        self.messages: list[str] = []
        self.listings: list[tuple[Listing, str]] = []
        self.photos: list[tuple[bytes | str | None, str, dict | None]] = []
        self.sent_count = 0
        self.error_count = 0
        self.enabled = True
        self.callbacks: list[tuple[str, str]] = []
        self.api_calls: list[tuple[str, dict | None, dict | None]] = []
        self.updates: list[dict] = []

    async def start(self): pass
    async def close(self): pass

    async def send_text(self, text, **kwargs):
        self.messages.append(text)
        return len(self.messages)

    async def send_photo(self, photo, caption, *, reply_markup=None, **kwargs):
        self.photos.append((photo, caption, reply_markup))
        self.messages.append(caption)
        return len(self.messages)

    async def send_listing(self, listing, *, source_label="", kind="new", old_price=None):
        self.listings.append((listing, kind))
        return True

    async def health(self):
        return {}

    async def answer_callback(self, callback_id, text=""):
        self.callbacks.append((callback_id, text))

    async def api(self, method, payload=None, *, files=None, retries=3):
        self.api_calls.append((method, payload, files))
        return {"message_id": len(self.messages) + 1}

    async def get_updates(self, offset=None, timeout=25):
        batch, self.updates = self.updates, []
        return batch

    async def get_me(self):
        return {"username": "avito_watch_test_bot", "id": 42}


class FakeEngine(Engine):
    """Отдаёт заранее подготовленный HTML, умеет изображать капчу."""

    name = "fake"

    def __init__(self, responses: dict[str, str] | None = None, default: str | None = None):
        self.responses = responses or {}
        self.default = default if default is not None else load_html("listing_state")
        self.calls: list[str] = []
        self.captcha_calls = 0
        self.solve_result = None

    def set(self, url: str, html: str) -> None:
        self.responses[url] = html

    async def start(self): pass
    async def stop(self): pass
    async def save_session(self, path): pass

    async def fetch(self, url, *, is_item=False, source_id=None, captcha_handler=None) -> FetchResult:
        self.calls.append(url)
        html = self.responses.get(url, self.default)
        state = detect_state(html, is_item_page=is_item)
        if state is PageState.CAPTCHA and captcha_handler is not None:
            self.captcha_calls += 1
            page = FakePage(html)
            result = await captcha_handler(page, source_id)
            self.solve_result = result
            if result.solved:
                html = self.responses.get("__after_captcha__", load_html("listing_state"))
                state = detect_state(html, is_item_page=is_item)
        return FetchResult(url=url, html=html, state=state, engine=self.name)

    async def health(self):
        return {"engine": "fake", "running": True}


class FakePage(PageAdapter):
    """Страница для тестов решателя капчи."""

    supports_interaction = True

    def __init__(self, html: str, *, accept_answer: str | None = None,
                 after_html: str | None = None):
        self._html = html
        self._after = after_html if after_html is not None else load_html("listing_state")
        self.accept_answer = accept_answer
        self.filled: list[tuple[str, str]] = []
        self.submits = 0
        self.drags: list[tuple[tuple[float, float], float]] = []
        self.clicks: list[str] = []
        self.url = "https://www.avito.ru/moskva/test"
        self.visible: set[str] = set()

    async def html(self) -> str:
        return self._html

    async def screenshot(self, *, full_page=False) -> bytes | None:
        return b"\x89PNG\r\n\x1a\nfake"

    async def element_screenshot(self, selector: str) -> bytes | None:
        return b"\x89PNG\r\n\x1a\nfake-element"

    async def fill(self, selector: str, value: str) -> bool:
        self.filled.append((selector, value))
        return True

    async def submit_firewall_form(self, payload=None, input_selector="input[name=captcha-response]") -> bool:
        self.submits += 1
        if self.accept_answer and self.filled and self.filled[-1][1] == self.accept_answer:
            self._html = self._after
        return True

    async def is_visible(self, selector: str) -> bool:
        return selector in self.visible

    async def click(self, selector: str) -> bool:
        self.clicks.append(selector)
        return True

    async def bounding_box(self, selector: str):
        return {"x": 100.0, "y": 200.0, "width": 300.0, "height": 40.0}

    async def drag(self, start, distance_px, *, duration=1.0) -> bool:
        self.drags.append((start, distance_px))
        return True

    async def reload(self) -> None:
        pass

    async def evaluate(self, script, arg=None):
        return None


@pytest.fixture
def fake_telegram():
    return FakeTelegram()


@pytest.fixture
def fake_engine():
    return FakeEngine()