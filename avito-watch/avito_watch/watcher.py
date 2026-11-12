"""Планировщик: несколько ссылок, у каждой свой интервал, джиттер и бэкофф.

Один браузер, одна страница за раз: Авито считает частоту запросов по IP,
параллелить выгоднее прокси, а не потоками.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path

from .config import Config
from .db import Database
from .engine.base import Engine
from .models import PageState, Source, SourceStatus
from .notify.telegram import Telegram
from .pipeline import CycleReport, Pipeline
from .utils import jitter

log = logging.getLogger(__name__)

HEARTBEAT_PATH = Path("data/heartbeat.json")   # значение по умолчанию, задаётся конфигом


class Watcher:
    def __init__(self, cfg: Config, db: Database, engine: Engine, telegram: Telegram,
                 pipeline: Pipeline, *, manual=None, solver=None):
        self.cfg = cfg
        self.db = db
        self.engine = engine
        self.telegram = telegram
        self.pipeline = pipeline
        self.manual = manual
        self.solver = solver
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self.cycles = 0
        self.started_at = 0.0
        self.last_report: CycleReport | None = None
        self.paused_globally = False

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        self.started_at = time.time()
        await self.engine.start()
        await self.telegram.start()
        await self.pipeline.flush_backlog()
        self._tasks.append(asyncio.create_task(self._heartbeat_loop(), name="heartbeat"))
        try:
            await self._loop()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        with contextlib.suppress(Exception):
            await self.engine.stop()
        with contextlib.suppress(Exception):
            await self.telegram.close()
        self.db.close()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ loop
    async def _loop(self) -> None:
        while not self._stop.is_set():
            if self.paused_globally:
                await self._sleep(5)
                continue
            due = self.db.due_sources()
            if not due:
                await self._sleep(self._next_wait())
                continue
            for source in due:
                if self._stop.is_set():
                    break
                await self._run_source(source)
            await self._sleep(1)

    def _next_wait(self) -> float:
        sources = [s for s in self.db.list_sources() if s.status == SourceStatus.ACTIVE.value]
        if not sources:
            return 10.0
        delta = min(s.next_check_at for s in sources) - time.time()
        return max(1.0, min(delta, 30.0))

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.5, seconds))

    async def _run_source(self, source: Source) -> CycleReport:
        log.info("Проверяю источник %s (%s)", source.label, source.url)
        try:
            report = await self.pipeline.check_source(source)
        except Exception as exc:
            log.exception("Цикл источника %s упал", source.label)
            report = CycleReport(source_id=source.id, label=source.label,
                                 state=PageState.UNKNOWN, error=f"{type(exc).__name__}: {exc}")
        self.cycles += 1
        self.last_report = report
        self._reschedule(source, report)
        return report

    def _reschedule(self, source: Source, report: CycleReport) -> None:
        wcfg = self.cfg.watch
        now = time.time()
        errors = source.consecutive_errors
        status = SourceStatus.ACTIVE.value
        delay = jitter(source.interval_sec or wcfg.default_interval_sec, wcfg.interval_jitter)
        error_text = report.error

        if report.state is PageState.CAPTCHA:
            error_text = "капча не решена"
            delay = max(delay, wcfg.error_backoff_sec)
            if self.cfg.captcha.pause_source_on_captcha:
                status = SourceStatus.CAPTCHA.value
        elif report.state is PageState.BLOCKED:
            error_text = "файрвол: «подождите немного» — нужен другой IP"
            delay = max(delay, wcfg.blocked_backoff_sec)
            errors += 1
        elif report.state is PageState.CHALLENGE:
            error_text = "proof-of-work не завершился"
            delay = max(delay, wcfg.error_backoff_sec)
            errors += 1
        elif report.error:
            errors += 1
            delay = min(wcfg.error_backoff_max_sec,
                        max(delay, wcfg.error_backoff_sec * (2 ** min(errors - 1, 4))))
        else:
            errors = 0
            error_text = None

        self.db.update_source(source.id, last_check_at=now, next_check_at=now + delay,
                              status=status, consecutive_errors=errors, last_error=error_text)
        if status == SourceStatus.CAPTCHA.value:
            log.warning("Источник %s на паузе из-за капчи (/resume %d)", source.label, source.id)
        else:
            log.info("Следующая проверка %s через %.0f с", source.label, delay)

    # ------------------------------------------------------------------ heartbeat
    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            payload = {
                "ts": time.time(),
                "uptime_sec": round(time.time() - self.started_at, 1),
                "cycles": self.cycles,
                "paused": self.paused_globally,
                "engine": await self.engine.health(),
                "captcha": self.solver.stats() if self.solver else {},
                "telegram": {"sent": self.telegram.sent_count, "errors": self.telegram.error_count},
                "stats": self.db.stats(),
                "sources": [
                    {"id": s.id, "label": s.label, "status": s.status,
                     "next_in_sec": round(max(0.0, s.next_check_at - time.time())),
                     "interval_sec": s.interval_sec, "last_error": s.last_error}
                    for s in self.db.list_sources()
                ],
            }
            path = Path(self.cfg.storage.heartbeat_path or HEARTBEAT_PATH)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                # пишем атомарно: healthcheck не должен увидеть недописанный файл
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(path)
            except OSError as exc:
                log.debug("heartbeat: %s", exc)
            await self._sleep(self.cfg.watch.watchdog_sec)

    # ------------------------------------------------------------------ manual ops
    async def check_now(self, source_id: int) -> CycleReport | None:
        source = self.db.get_source(source_id)
        if source is None:
            return None
        return await self._run_source(source)

    def resume_source(self, source_id: int) -> bool:
        source = self.db.get_source(source_id)
        if source is None:
            return False
        self.db.update_source(source_id, status=SourceStatus.ACTIVE.value, next_check_at=0,
                              consecutive_errors=0, last_error=None)
        return True

    def pause_source(self, source_id: int) -> bool:
        if self.db.get_source(source_id) is None:
            return False
        self.db.update_source(source_id, status=SourceStatus.PAUSED.value)
        return True

    def sync_config_sources(self) -> int:
        """Добавляет ссылки из config.yaml (существующие не дублируются)."""
        added = 0
        for entry in self.cfg.sources:
            if isinstance(entry, str):
                entry = {"url": entry}
            url = (entry.get("url") or "").strip()
            if not url:
                continue
            existing = next((s for s in self.db.list_sources() if s.url == url), None)
            if existing:
                continue
            self.db.add_source(
                url,
                name=entry.get("name", ""),
                interval_sec=int(entry.get("interval_sec", self.cfg.watch.default_interval_sec)),
                pages=int(entry.get("pages", self.cfg.watch.pages_per_source)),
                seed_existing=bool(entry.get("seed_existing", self.cfg.watch.seed_existing)),
                min_price=entry.get("min_price"),
                max_price=entry.get("max_price"),
                keywords_include=entry.get("keywords_include") or [],
                keywords_exclude=entry.get("keywords_exclude") or [],
            )
            added += 1
        return added