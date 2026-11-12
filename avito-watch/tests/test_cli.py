"""CLI: add / list / remove / export / doctor / once / run — без сети и браузера."""
from __future__ import annotations

import csv
import json

import pytest

from avito_watch import cli
from avito_watch.config import Config
from avito_watch.db import Database


@pytest.fixture
def env_cfg(cfg, monkeypatch, tmp_path):
    """Config.load возвращает наш тестовый конфиг; .env не читаем."""
    monkeypatch.setattr(cli, "load_dotenv", lambda path: None)
    monkeypatch.setattr(cli.Config, "load", classmethod(lambda cls, path=None: cfg))
    monkeypatch.setattr(cli, "setup_logging", lambda logging_cfg: None)
    return cfg


def run_cli(*args) -> int:
    return cli.main(list(args))


def test_add_list_remove(env_cfg, db, capsys):
    assert run_cli("add", "https://www.avito.ru/moskva?q=iphone", "--name", "iPhone",
                   "--interval", "240") == 0
    assert "Добавлен источник" in capsys.readouterr().out

    source = db.list_sources()[0]
    assert source.label == "iPhone" and source.interval_sec == 240

    assert run_cli("list") == 0
    out = capsys.readouterr().out
    assert "iPhone" in out and "240с" in out

    assert run_cli("remove", str(source.id)) == 0
    assert db.list_sources() == []
    assert run_cli("remove", "999") == 1


def test_add_no_seed_flag(env_cfg, db):
    run_cli("add", "https://www.avito.ru/moskva?q=iphone", "--no-seed")
    assert db.list_sources()[0].seed_existing is False


def test_export_csv_and_json(env_cfg, db, tmp_path, capsys):
    from avito_watch.models import Listing
    db.upsert_listing(Listing(item_id="1234567890", url="https://www.avito.ru/x_1234567890",
                              title="iPhone", price=1000))
    csv_path = tmp_path / "out.csv"
    assert run_cli("export", "--out", str(csv_path)) == 0
    rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8-sig").splitlines()))
    assert rows and rows[0]["item_id"] == "1234567890"

    json_path = tmp_path / "out.json"
    assert run_cli("export", "--out", str(json_path), "--format", "json") == 0
    assert json.loads(json_path.read_text(encoding="utf-8"))[0]["title"] == "iPhone"
    assert "Сохранено 1 строк" in capsys.readouterr().out


def test_doctor_reports_environment(env_cfg, capsys):
    code = cli.cmd_doctor(env_cfg)
    out = capsys.readouterr().out
    assert "Avito Watch" in out
    assert "Зависимости" in out and "Конфигурация" in out and "База данных" in out
    assert code in (0, 1)


def test_doctor_flags_missing_token(cfg, capsys):
    cfg.telegram.bot_token = ""
    assert cli.cmd_doctor(cfg) in (0, 1)
    assert "AW_TELEGRAM_TOKEN" in capsys.readouterr().out


async def test_once_runs_pipeline(env_cfg, db, fake_engine, fake_telegram, monkeypatch, capsys):
    manual = cli.ManualCenter()
    from avito_watch.pipeline import Pipeline
    from avito_watch.watcher import Watcher
    pipeline = Pipeline(env_cfg, db, fake_engine, fake_telegram)
    watcher = Watcher(env_cfg, db, fake_engine, fake_telegram, pipeline, manual=manual)
    monkeypatch.setattr(cli, "build_stack",
                        lambda cfg: (db, fake_telegram, fake_engine, manual, None, pipeline, watcher))
    db.add_source("https://www.avito.ru/moskva?q=iphone", name="iPhone")

    assert await cli.cmd_once(env_cfg, None) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["state"] == "content"
    assert payload["source"] == "iPhone"
    assert fake_engine.calls


async def test_run_starts_bot_and_stops_cleanly(env_cfg, db, fake_engine, fake_telegram, monkeypatch):
    """run: поднимает стек, шлёт приветствие, крутит планировщик и корректно гасится."""
    import asyncio

    from avito_watch.pipeline import Pipeline
    from avito_watch.watcher import Watcher

    env_cfg.storage.heartbeat_path = str(env_cfg.storage.db_path).replace("test.db", "heartbeat.json")
    manual = cli.ManualCenter()
    pipeline = Pipeline(env_cfg, db, fake_engine, fake_telegram)
    watcher = Watcher(env_cfg, db, fake_engine, fake_telegram, pipeline, manual=manual)
    from avito_watch.captcha.solver import CaptchaSolver
    solver = CaptchaSolver(env_cfg, db, fake_telegram, manual, engine=fake_engine)
    monkeypatch.setattr(cli, "build_stack",
                        lambda cfg: (db, fake_telegram, fake_engine, manual, solver, pipeline, watcher))
    env_cfg.sources = [{"url": "https://www.avito.ru/moskva?q=iphone", "name": "Из конфига"}]

    started = asyncio.Event()
    original_loop = watcher._loop

    async def fast_loop(self=None):
        started.set()
        await asyncio.sleep(0.05)
        watcher.stop()

    monkeypatch.setattr(watcher, "_loop", fast_loop)
    task = asyncio.create_task(cli.cmd_run(env_cfg))
    await asyncio.wait_for(started.wait(), timeout=5)
    assert await asyncio.wait_for(task, timeout=10) == 0
    assert any("Avito Watch запущен" in m for m in fake_telegram.messages)
    assert db.list_sources()[0].label == "Из конфига"
    assert original_loop is not None


async def test_test_telegram_command(env_cfg, fake_telegram, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Telegram", lambda cfg: fake_telegram)
    fake_telegram.messages.clear()

    async def send_text(text, **kwargs):
        fake_telegram.messages.append(text)
        return 7

    monkeypatch.setattr(fake_telegram, "send_text", send_text)
    assert await cli.cmd_test_telegram(env_cfg) == 0
    out = capsys.readouterr().out
    assert "Бот на связи" in out and "Сообщение отправлено" in out


async def test_warmup_uses_headed_browser(env_cfg, monkeypatch, capsys):
    calls = {}

    class FakeBrowser:
        def __init__(self, browser_cfg):
            calls["headless"] = browser_cfg.headless

        async def open_manual(self, url, *, seconds):
            calls["url"], calls["seconds"] = url, seconds

        async def stop(self):
            calls["stopped"] = True

    import avito_watch.engine.browser as browser_module
    monkeypatch.setattr(browser_module, "BrowserEngine", FakeBrowser)
    assert await cli.cmd_warmup(env_cfg, "https://www.avito.ru/", 120) == 0
    assert calls == {"headless": False, "url": "https://www.avito.ru/", "seconds": 120, "stopped": True}
    assert "сессия сохранена" in capsys.readouterr().out


def test_build_engine_switches_by_config(cfg):
    cfg.browser.engine = "browser"
    assert cli.build_engine(cfg).name == "browser"
    cfg.browser.engine = "http"
    assert cli.build_engine(cfg).name == "http"


def test_parser_defaults_to_run():
    args = cli.make_parser().parse_args([])
    assert args.command is None            # main() превращает это в run
    assert cli.make_parser().parse_args(["once", "--source", "3"]).source == 3


def test_stored_chat_id_is_restored(cfg, monkeypatch):
    database = Database(cfg.storage.db_path)
    database.set_meta("chat_id", "555")
    cfg.telegram.chat_id = ""
    cfg.telegram.admin_ids = []
    monkeypatch.setattr(cli, "Database", lambda path: database)
    cli.build_stack(cfg)
    assert cfg.telegram.chat_id == "555"
    assert cfg.telegram.admin_ids == ["555"]
