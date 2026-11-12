"""Планировщик: интервалы, бэкофф, паузы, синхронизация ссылок из конфига."""
import time

import pytest

from avito_watch.models import PageState, SourceStatus
from avito_watch.pipeline import CycleReport, Pipeline
from avito_watch.watcher import Watcher


@pytest.fixture
def watcher(cfg, db, fake_engine, fake_telegram):
    pipeline = Pipeline(cfg, db, fake_engine, fake_telegram)
    return Watcher(cfg, db, fake_engine, fake_telegram, pipeline)


def make_report(source, state=PageState.CONTENT, error=None):
    return CycleReport(source_id=source.id, label=source.label, state=state, error=error)


def test_success_reschedules_with_interval(watcher, db):
    source = db.add_source("https://www.avito.ru/x", interval_sec=300)
    watcher._reschedule(source, make_report(source))
    updated = db.get_source(source.id)
    delay = updated.next_check_at - time.time()
    assert 200 < delay < 400          # интервал ± джиттер
    assert updated.status == SourceStatus.ACTIVE.value
    assert updated.consecutive_errors == 0


def test_captcha_pauses_source(watcher, db):
    source = db.add_source("https://www.avito.ru/x", interval_sec=60)
    watcher._reschedule(source, make_report(source, PageState.CAPTCHA))
    updated = db.get_source(source.id)
    assert updated.status == SourceStatus.CAPTCHA.value
    assert "капча" in (updated.last_error or "")
    assert db.due_sources() == []


def test_blocked_uses_long_backoff(watcher, db):
    source = db.add_source("https://www.avito.ru/x", interval_sec=60)
    watcher._reschedule(source, make_report(source, PageState.BLOCKED))
    updated = db.get_source(source.id)
    assert updated.next_check_at - time.time() >= watcher.cfg.watch.blocked_backoff_sec - 5
    assert updated.consecutive_errors == 1


def test_errors_escalate_backoff(watcher, db):
    source = db.add_source("https://www.avito.ru/x", interval_sec=60)
    delays = []
    for _ in range(4):
        current = db.get_source(source.id)
        watcher._reschedule(current, make_report(current, PageState.UNKNOWN, error="timeout"))
        updated = db.get_source(source.id)
        delays.append(updated.next_check_at - time.time())
    assert delays[-1] > delays[0]
    assert delays[-1] <= watcher.cfg.watch.error_backoff_max_sec + 5


def test_pause_and_resume(watcher, db):
    source = db.add_source("https://www.avito.ru/x")
    assert watcher.pause_source(source.id) is True
    assert db.get_source(source.id).status == SourceStatus.PAUSED.value
    assert watcher.resume_source(source.id) is True
    updated = db.get_source(source.id)
    assert updated.status == SourceStatus.ACTIVE.value
    assert updated.next_check_at == 0
    assert watcher.pause_source(9999) is False


def test_sync_config_sources(watcher, db, cfg):
    cfg.sources = [
        {"url": "https://www.avito.ru/moskva?q=iphone", "name": "iPhone", "interval_sec": 180},
        "https://www.avito.ru/spb?q=trek",
    ]
    assert watcher.sync_config_sources() == 2
    assert watcher.sync_config_sources() == 0        # без дублей
    sources = db.list_sources()
    assert sources[0].interval_sec == 180
    assert sources[1].interval_sec == cfg.watch.default_interval_sec


async def test_run_source_records_report(watcher, db, fake_engine):
    source = db.add_source("https://www.avito.ru/moskva?q=iphone", name="iPhone")
    report = await watcher._run_source(source)
    assert report.state is PageState.CONTENT
    assert watcher.cycles == 1
    assert watcher.last_report is report


async def test_run_source_survives_exception(watcher, db, monkeypatch):
    source = db.add_source("https://www.avito.ru/x")

    async def boom(_source):
        raise RuntimeError("движок упал")

    monkeypatch.setattr(watcher.pipeline, "check_source", boom)
    report = await watcher._run_source(source)
    assert report.error and "движок упал" in report.error
    assert db.get_source(source.id).consecutive_errors == 1