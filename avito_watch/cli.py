"""CLI: run / once / add / list / remove / warmup / doctor / export / test-telegram."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import importlib.util
import json
import logging
import signal
import sys
from pathlib import Path

from . import __version__
from .captcha.manual import ManualCenter
from .captcha.solver import CaptchaSolver
from .config import Config, load_dotenv
from .db import Database
from .logging_setup import setup_logging
from .models import SourceStatus
from .notify.telegram import Telegram
from .pipeline import Pipeline
from .watcher import Watcher

log = logging.getLogger("avito_watch.cli")


def build_engine(cfg: Config):
    if cfg.browser.engine == "http":
        from .engine.http import HttpEngine
        return HttpEngine(cfg.browser)
    from .engine.browser import BrowserEngine
    return BrowserEngine(cfg.browser)


def build_stack(cfg: Config):
    db = Database(cfg.storage.db_path)
    stored_chat = db.get_meta("chat_id")
    if stored_chat and not cfg.telegram.chat_id:
        cfg.telegram.chat_id = stored_chat
        if not cfg.telegram.admin_ids:
            cfg.telegram.admin_ids = [stored_chat]
    telegram = Telegram(cfg.telegram)
    engine = build_engine(cfg)
    manual = ManualCenter()
    solver = CaptchaSolver(cfg, db, telegram, manual, engine=engine)
    pipeline = Pipeline(cfg, db, engine, telegram, solver)
    watcher = Watcher(cfg, db, engine, telegram, pipeline, manual=manual, solver=solver)
    return db, telegram, engine, manual, solver, pipeline, watcher


# ---------------------------------------------------------------------- commands
async def cmd_run(cfg: Config) -> int:
    db, telegram, engine, manual, solver, pipeline, watcher = build_stack(cfg)
    from .bot.commands import BotController

    problems = cfg.validate()
    for problem in problems:
        log.warning("Конфигурация: %s", problem)
    added = watcher.sync_config_sources()
    if added:
        log.info("Добавлено источников из config.yaml: %d", added)
    sources = db.list_sources()
    if not sources:
        log.warning("Источников нет. Добавьте ссылку: python -m avito_watch add <URL> "
                    "или командой /add в Telegram")

    await telegram.start()
    if telegram.enabled:
        with contextlib.suppress(Exception):
            me = await telegram.get_me()
            log.info("Telegram-бот: @%s", me.get("username"))
        await telegram.send_text(
            f"🚀 <b>Avito Watch запущен</b> (v{__version__})\n"
            f"Источников: {len(sources)}\n"
            f"Интервал по умолчанию: {cfg.watch.default_interval_sec}с\n"
            f"Локальный OCR капчи: {solver.ocr.backend}\n"
            f"Команды: /help")
    else:
        log.warning("Telegram не настроен — уведомления только в лог. Заполните .env")

    bot = BotController(cfg, db, telegram, watcher, manual)
    bot.start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, watcher.stop)

    log.info("Поехали. Ctrl+C — остановка.")
    await watcher.run()
    await bot.stop()
    return 0


async def cmd_once(cfg: Config, source_id: int | None) -> int:
    db, telegram, engine, manual, solver, pipeline, watcher = build_stack(cfg)
    watcher.sync_config_sources()
    await engine.start()
    await telegram.start()
    try:
        targets = [db.get_source(source_id)] if source_id else db.list_sources()
        for source in targets:
            if source is None:
                continue
            report = await pipeline.check_source(source)
            print(json.dumps({
                "source": source.label, "state": report.state.value, "found": report.found,
                "new": report.new, "notified": report.notified, "seeded": report.seeded,
                "enriched": report.enriched, "error": report.error,
            }, ensure_ascii=False))
            db.update_source(source.id, last_check_at=__import__("time").time())
    finally:
        await engine.stop()
        await telegram.close()
        db.close()
    return 0


async def cmd_warmup(cfg: Config, url: str, seconds: int) -> int:
    """Один раз пройти файрвол/капчу руками, чтобы профиль запомнил сессию."""
    cfg.browser.headless = False
    from .engine.browser import BrowserEngine

    engine = BrowserEngine(cfg.browser)
    print("Открываю браузер. Пройдите капчу/войдите в аккаунт вручную.")
    print(f"Профиль сохранится в {cfg.browser.user_data_dir}. Окно можно закрыть, когда увидите выдачу.")
    try:
        await engine.open_manual(url, seconds=seconds)
    finally:
        await engine.stop()
    print("Готово: сессия сохранена.")
    return 0


async def cmd_test_telegram(cfg: Config) -> int:
    telegram = Telegram(cfg.telegram)
    await telegram.start()
    try:
        me = await telegram.get_me()
        print(f"Бот на связи: @{me.get('username')} (id {me.get('id')})")
        message_id = await telegram.send_text("✅ Тестовое сообщение от Avito Watch. Всё работает.")
        print("Сообщение отправлено" if message_id else "Не удалось отправить (проверьте AW_CHAT_ID)")
        return 0 if message_id else 1
    finally:
        await telegram.close()


def cmd_doctor(cfg: Config) -> int:
    print(f"Avito Watch v{__version__}\n")
    print(f"Python: {sys.version.split()[0]}")
    ok = True

    print("\nЗависимости:")
    required = {"bs4": "beautifulsoup4", "lxml": "lxml", "yaml": "pyyaml", "httpx": "httpx"}
    optional = {"playwright": "playwright (движок браузера)", "cv2": "opencv-python-headless (слайдер)",
                "numpy": "numpy", "PIL": "pillow", "ddddocr": "ddddocr (локальный OCR капчи)",
                "onnxruntime": "onnxruntime (своя OCR-модель)", "curl_cffi": "curl_cffi (engine=http)"}
    for module, label in required.items():
        present = importlib.util.find_spec(module) is not None
        print(f"  {'✅' if present else '❌'} {label}")
        ok = ok and present
    for module, label in optional.items():
        present = importlib.util.find_spec(module) is not None
        print(f"  {'✅' if present else '⚪️'} {label}{'' if present else ' — не установлено'}")

    print("\nБраузер:")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            path = Path(pw.chromium.executable_path)
            print(f"  {'✅' if path.exists() else '❌'} Chromium: {path}")
            if not path.exists():
                print("     выполните: playwright install chromium")
                ok = False
    except Exception as exc:
        print(f"  ⚪️ Playwright недоступен: {exc}")

    print("\nКонфигурация:")
    problems = cfg.validate()
    if problems:
        for problem in problems:
            print(f"  ⚠️ {problem}")
    else:
        print("  ✅ Ошибок нет")
    print(f"  Файл: {cfg.config_path}")
    print(f"  БД: {cfg.storage.db_path}")
    print(f"  Профиль браузера: {cfg.browser.user_data_dir}")
    print(f"  Прокси: {'задан' if cfg.browser.proxy else 'нет (лучше резидентный RU IP)'}")
    print(f"  Движок: {cfg.browser.engine}, headless={cfg.browser.headless}")

    print("\nБаза данных:")
    try:
        db = Database(cfg.storage.db_path)
        stats = db.stats()
        print(f"  ✅ Источников: {stats['sources']}, объявлений: {stats['listings']}, "
              f"капч: {stats['captcha_total']}")
        for source in db.list_sources():
            print(f"     [{source.id}] {source.label} · {source.interval_sec}с · {source.status}")
        db.close()
    except Exception as exc:
        print(f"  ❌ {exc}")
        ok = False
    return 0 if ok else 1


def cmd_export(cfg: Config, out: str, fmt: str) -> int:
    db = Database(cfg.storage.db_path)
    rows = db.export_listings()
    db.close()
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            if rows:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
    print(f"Сохранено {len(rows)} строк → {path}")
    return 0


# ---------------------------------------------------------------------- entrypoint
def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser("avito_watch", description="Личный парсер Авито → Telegram")
    parser.add_argument("--config", default=None, help="путь к config.yaml")
    parser.add_argument("--env", default=".env", help="путь к .env")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="запустить мониторинг (основной режим)")
    once = sub.add_parser("once", help="один проход по источникам и выход")
    once.add_argument("--source", type=int, default=None)
    add = sub.add_parser("add", help="добавить ссылку")
    add.add_argument("url")
    add.add_argument("--name", default="")
    add.add_argument("--interval", type=int, default=None)
    add.add_argument("--pages", type=int, default=None)
    add.add_argument("--no-seed", action="store_true", help="прислать и текущие объявления тоже")
    sub.add_parser("list", help="список источников")
    remove = sub.add_parser("remove", help="удалить источник")
    remove.add_argument("source_id", type=int)
    warmup = sub.add_parser("warmup", help="открыть браузер и пройти капчу вручную (прогрев профиля)")
    warmup.add_argument("--url", default="https://www.avito.ru/")
    warmup.add_argument("--seconds", type=int, default=600)
    sub.add_parser("doctor", help="диагностика окружения и конфигурации")
    sub.add_parser("test-telegram", help="проверить токен и отправить тестовое сообщение")
    export = sub.add_parser("export", help="выгрузить объявления")
    export.add_argument("--out", default="data/export.csv")
    export.add_argument("--format", choices=["csv", "json"], default="csv")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    load_dotenv(args.env)
    cfg = Config.load(args.config)
    setup_logging(cfg.logging)
    command = args.command or "run"

    if command == "run":
        return asyncio.run(cmd_run(cfg))
    if command == "once":
        return asyncio.run(cmd_once(cfg, args.source))
    if command == "warmup":
        return asyncio.run(cmd_warmup(cfg, args.url, args.seconds))
    if command == "test-telegram":
        return asyncio.run(cmd_test_telegram(cfg))
    if command == "doctor":
        return cmd_doctor(cfg)
    if command == "export":
        return cmd_export(cfg, args.out, args.format)
    if command == "add":
        db = Database(cfg.storage.db_path)
        source = db.add_source(args.url, name=args.name,
                               interval_sec=args.interval or cfg.watch.default_interval_sec,
                               pages=args.pages or cfg.watch.pages_per_source,
                               seed_existing=not args.no_seed and cfg.watch.seed_existing)
        db.close()
        print(f"Добавлен источник [{source.id}] {source.label} · {source.interval_sec}с")
        return 0
    if command == "list":
        db = Database(cfg.storage.db_path)
        for source in db.list_sources():
            print(f"[{source.id}] {source.label} · {source.interval_sec}с · страниц {source.pages} · "
                  f"{source.status} · найдено {source.found_total}\n     {source.url}")
        db.close()
        return 0
    if command == "remove":
        db = Database(cfg.storage.db_path)
        ok = db.remove_source(args.source_id)
        db.close()
        print("Удалено" if ok else "Не найдено")
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())