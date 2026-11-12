"""Центр ручного решения капчи: связывает Telegram-кнопки/ответы с ожидающим парсером."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from ..models import CaptchaVariant

log = logging.getLogger(__name__)


@dataclass
class PendingCaptcha:
    event_id: int
    source_id: int | None
    variant: CaptchaVariant
    source_label: str = ""
    created_at: float = field(default_factory=time.time)
    message_id: int | None = None
    answers: asyncio.Queue = field(default_factory=asyncio.Queue)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    skip: asyncio.Event = field(default_factory=asyncio.Event)

    def age(self) -> float:
        return time.time() - self.created_at


class ManualCenter:
    def __init__(self) -> None:
        self._by_event: dict[int, PendingCaptcha] = {}
        self._by_source: dict[int, PendingCaptcha] = {}
        self._latest: PendingCaptcha | None = None

    def register(self, event_id: int, source_id: int | None, variant: CaptchaVariant,
                 source_label: str = "") -> PendingCaptcha:
        pending = PendingCaptcha(event_id=event_id, source_id=source_id, variant=variant,
                                 source_label=source_label)
        self._by_event[event_id] = pending
        if source_id is not None:
            self._by_source[source_id] = pending
        self._latest = pending
        return pending

    def clear(self, event_id: int) -> None:
        pending = self._by_event.pop(event_id, None)
        if pending and pending.source_id is not None:
            self._by_source.pop(pending.source_id, None)
        if self._latest is pending:
            self._latest = None

    def get(self, event_id: int) -> PendingCaptcha | None:
        return self._by_event.get(event_id)

    def for_source(self, source_id: int) -> PendingCaptcha | None:
        return self._by_source.get(source_id)

    @property
    def latest(self) -> PendingCaptcha | None:
        return self._latest

    @property
    def active(self) -> list[PendingCaptcha]:
        return list(self._by_event.values())

    # ---- команды из Telegram ----
    def mark_done(self, event_id: int | None = None) -> PendingCaptcha | None:
        pending = self._by_event.get(event_id) if event_id is not None else self._latest
        if pending:
            pending.done.set()
        return pending

    def mark_skip(self, event_id: int | None = None) -> PendingCaptcha | None:
        pending = self._by_event.get(event_id) if event_id is not None else self._latest
        if pending:
            pending.skip.set()
        return pending

    def push_answer(self, text: str, source_id: int | None = None) -> PendingCaptcha | None:
        pending = self._by_source.get(source_id) if source_id is not None else self._latest
        if pending:
            pending.answers.put_nowait(text.strip())
        return pending