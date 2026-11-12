"""Абстракция движка загрузки страниц.

Нужна, чтобы парсер, планировщик и решатель капчи не зависели от Playwright
и чтобы тесты могли подставить фейковый движок без браузера и сети.
"""
from __future__ import annotations

import abc
from typing import Any, Awaitable, Callable

from ..models import FetchResult, SolveResult


class PageAdapter(abc.ABC):
    """Минимальный интерфейс живой страницы для решателей капчи."""

    supports_interaction: bool = False
    url: str = ""

    @abc.abstractmethod
    async def html(self) -> str: ...

    @abc.abstractmethod
    async def screenshot(self, *, full_page: bool = False) -> bytes | None: ...

    async def element_screenshot(self, selector: str) -> bytes | None:
        return None

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        raise NotImplementedError

    async def bounding_box(self, selector: str) -> dict[str, float] | None:
        return None

    async def is_visible(self, selector: str) -> bool:
        return False

    async def click(self, selector: str) -> bool:
        return False

    async def fill(self, selector: str, value: str) -> bool:
        return False

    async def drag(self, start: tuple[float, float], distance_px: float, *,
                   duration: float = 1.0) -> bool:
        return False

    async def reload(self) -> None:
        return None

    async def submit_firewall_form(self, payload: str | None = None,
                                   input_selector: str = "input[name=captcha-response]") -> bool:
        return False


CaptchaHandler = Callable[[PageAdapter, int | None], Awaitable[SolveResult]]


class Engine(abc.ABC):
    name = "base"

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def fetch(self, url: str, *, is_item: bool = False, source_id: int | None = None,
                    captcha_handler: CaptchaHandler | None = None) -> FetchResult: ...

    async def save_session(self, path: str) -> None:
        return None

    async def health(self) -> dict[str, Any]:
        return {"engine": self.name}