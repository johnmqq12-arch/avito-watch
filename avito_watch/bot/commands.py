"""Управление парсером из Telegram: long polling + команды и кнопки."""
from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import logging
import time

from ..captcha.manual import ManualCenter
from ..config import Config
from ..db import Database
from ..models import SourceStatus
from ..notify.telegram import Telegram
from ..utils import MSK, format_price, human_date
from ..watcher import Watcher

log = logging.getLogger(__name__)

HELP = """<b>Avito Watch — команды</b>

/list — источники и их состояние
/add &lt;ссылка&gt; [название] — добавить ссылку
/remove &lt;id&gt; — удалить источник
/interval &lt;id&gt; &lt;сек&gt; — свой интервал обновления
/check &lt;id&gt; — проверить прямо сейчас
/pause [id] — пауза (без id — весь парсер)
/resume [id] — продолжить
/answer &lt;текст&gt; — ответ на капчу-картинку
/solved — «я прошёл капчу»
/stats — статистика и капчи
/export — выгрузка объявлений CSV
/id — ваш chat_id
/status — состояние движка и планировщика"""


class BotController:
    def __init__(self, cfg: Config, db: Database, telegram: Telegram, watcher: Watcher,
                 manual: ManualCenter):
        self.cfg = cfg
        self.db = db
        self.telegram = telegram
        self.watcher = watcher
        self.manual = manual
        self._offset: int | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if not (self.cfg.telegram.commands_enabled and self.cfg.telegram.bot_token):
            return
        stored = self.db.get_meta("tg_offset")
        self._offset = int(stored) if stored and stored.isdigit() else None
        self._task = asyncio.create_task(self._loop(), name="telegram-polling")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        log.info("Telegram-команды активны")
        while True:
            try:
                updates = await self.telegram.get_updates(self._offset, timeout=25)
                for update in updates:
                    self._offset = update["update_id"] + 1
                    self.db.set_meta("tg_offset", str(self._offset))
                    await self._handle(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Ошибка polling: %s", exc)
                await asyncio.sleep(5)

    # ------------------------------------------------------------------ auth
    def _authorized(self, chat_id: str, user_id: str) -> bool:
        admins = self.cfg.telegram.admin_ids
        if not admins:
            return True
        return chat_id in admins or user_id in admins

    # ------------------------------------------------------------------ routing
    async def _handle(self, update: dict) -> None:
        if "callback_query" in update:
            await self._callback(update["callback_query"])
            return
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        text = (message.get("text") or "").strip()
        chat_id = str(message["chat"]["id"])
        user_id = str((message.get("from") or {}).get("id", ""))

        if not text:
            return
        if not text.startswith("/"):
            # ответ на сообщение с капчей: просто текст = ответ на капчу
            reply_to = message.get("reply_to_message") or {}
            if reply_to and self.manual.latest:
                await self._answer(chat_id, text)
            return

        command, _, args = text.partition(" ")
        command = command.split("@")[0].lower()
        if command in ("/start", "/id", "/help") and not self.cfg.telegram.chat_id:
            self.cfg.telegram.chat_id = chat_id
            self.db.set_meta("chat_id", chat_id)
            if not self.cfg.telegram.admin_ids:
                self.cfg.telegram.admin_ids = [chat_id]
            await self.telegram.send_text(
                f"✅ Чат подключён: <code>{chat_id}</code>\n"
                f"Теперь уведомления будут приходить сюда.\n\n{HELP}", chat_id=chat_id)
            return
        if not self._authorized(chat_id, user_id):
            log.warning("Отклонена команда от неавторизованного chat_id=%s", chat_id)
            return

        handlers = {
            "/start": self._cmd_start, "/help": self._cmd_help, "/id": self._cmd_id,
            "/list": self._cmd_list, "/add": self._cmd_add, "/remove": self._cmd_remove,
            "/interval": self._cmd_interval, "/check": self._cmd_check,
            "/pause": self._cmd_pause, "/resume": self._cmd_resume,
            "/answer": self._cmd_answer, "/solved": self._cmd_solved,
            "/stats": self._cmd_stats, "/status": self._cmd_status, "/export": self._cmd_export,
        }
        handler = handlers.get(command)
        if handler is None:
            await self.telegram.send_text(f"Не знаю команду {command}. /help", chat_id=chat_id)
            return
        try:
            await handler(chat_id, args.strip())
        except Exception as exc:
            log.exception("Команда %s упала", command)
            await self.telegram.send_text(f"⚠️ Ошибка: {exc}", chat_id=chat_id)

    async def _callback(self, callback: dict) -> None:
        data = callback.get("data") or ""
        chat_id = str((callback.get("message") or {}).get("chat", {}).get("id", ""))
        user_id = str((callback.get("from") or {}).get("id", ""))
        callback_id = callback.get("id", "")
        if not self._authorized(chat_id, user_id):
            await self.telegram.answer_callback(callback_id, "Нет доступа")
            return
        parts = data.split(":")
        if len(parts) == 3 and parts[0] == "cap":
            event_id = int(parts[2]) if parts[2].isdigit() else None
            if parts[1] == "done":
                pending = self.manual.mark_done(event_id)
                await self.telegram.answer_callback(callback_id, "Проверяю…" if pending else "Капча уже неактуальна")
            elif parts[1] == "skip":
                self.manual.mark_skip(event_id)
                await self.telegram.answer_callback(callback_id, "Пропускаю цикл")
        elif len(parts) == 3 and parts[0] == "src":
            source_id = int(parts[2]) if parts[2].isdigit() else None
            if source_id is not None:
                if parts[1] == "pause":
                    self.watcher.pause_source(source_id)
                    await self.telegram.answer_callback(callback_id, f"Источник {source_id} на паузе")
                elif parts[1] == "resume":
                    self.watcher.resume_source(source_id)
                    await self.telegram.answer_callback(callback_id, f"Источник {source_id} возобновлён")
        else:
            await self.telegram.answer_callback(callback_id)

    # ------------------------------------------------------------------ commands
    async def _cmd_start(self, chat_id: str, args: str) -> None:
        await self.telegram.send_text(
            f"👋 На связи. Источников: {len(self.db.list_sources())}.\n\n{HELP}", chat_id=chat_id)

    async def _cmd_help(self, chat_id: str, args: str) -> None:
        await self.telegram.send_text(HELP, chat_id=chat_id)

    async def _cmd_id(self, chat_id: str, args: str) -> None:
        await self.telegram.send_text(f"Ваш chat_id: <code>{chat_id}</code>", chat_id=chat_id)

    async def _cmd_list(self, chat_id: str, args: str) -> None:
        sources = self.db.list_sources()
        if not sources:
            await self.telegram.send_text("Источников нет. /add <ссылка>", chat_id=chat_id)
            return
        icons = {SourceStatus.ACTIVE.value: "🟢", SourceStatus.PAUSED.value: "⏸",
                 SourceStatus.CAPTCHA.value: "🧩", SourceStatus.ERROR.value: "🔴"}
        lines = ["<b>Источники</b>"]
        for s in sources:
            next_in = max(0, int(s.next_check_at - time.time()))
            lines.append(
                f"{icons.get(s.status, '⚪')} <b>{s.id}</b> {s.label} · {s.interval_sec}с · "
                f"страниц: {s.pages} · след. через {next_in}с · найдено {s.found_total}")
            if s.last_error:
                lines.append(f"   ⚠️ {s.last_error}")
        await self.telegram.send_text("\n".join(lines), chat_id=chat_id)

    async def _cmd_add(self, chat_id: str, args: str) -> None:
        if not args:
            await self.telegram.send_text("Формат: /add <ссылка> [название]", chat_id=chat_id)
            return
        parts = args.split(" ", 1)
        url = parts[0]
        name = parts[1] if len(parts) > 1 else ""
        if "avito.ru" not in url:
            await self.telegram.send_text("Это не ссылка Авито.", chat_id=chat_id)
            return
        source = self.db.add_source(url, name=name,
                                    interval_sec=self.cfg.watch.default_interval_sec,
                                    pages=self.cfg.watch.pages_per_source,
                                    seed_existing=self.cfg.watch.seed_existing)
        await self.telegram.send_text(
            f"➕ Добавил источник <b>{source.id}</b> ({source.label}), интервал {source.interval_sec}с.\n"
            f"Первый проход только зафиксирует текущие объявления.", chat_id=chat_id)

    async def _cmd_remove(self, chat_id: str, args: str) -> None:
        if not args.isdigit():
            await self.telegram.send_text("Формат: /remove <id>", chat_id=chat_id)
            return
        ok = self.db.remove_source(int(args))
        await self.telegram.send_text("🗑 Удалил" if ok else "Не нашёл такой источник", chat_id=chat_id)

    async def _cmd_interval(self, chat_id: str, args: str) -> None:
        parts = args.split()
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            await self.telegram.send_text("Формат: /interval <id> <секунды>", chat_id=chat_id)
            return
        source_id, seconds = int(parts[0]), int(parts[1])
        if seconds < self.cfg.watch.min_interval_sec:
            await self.telegram.send_text(
                f"⚠️ {seconds}с — слишком часто, Авито начнёт капчить. "
                f"Минимум {self.cfg.watch.min_interval_sec}с. Всё равно ставлю, но будьте готовы.",
                chat_id=chat_id)
        if self.db.get_source(source_id) is None:
            await self.telegram.send_text("Не нашёл такой источник", chat_id=chat_id)
            return
        self.db.update_source(source_id, interval_sec=max(15, seconds), next_check_at=0)
        await self.telegram.send_text(f"⏱ Интервал источника {source_id}: {max(15, seconds)}с", chat_id=chat_id)

    async def _cmd_check(self, chat_id: str, args: str) -> None:
        if not args.isdigit():
            await self.telegram.send_text("Формат: /check <id>", chat_id=chat_id)
            return
        source_id = int(args)
        if self.db.get_source(source_id) is None:
            await self.telegram.send_text("Не нашёл такой источник", chat_id=chat_id)
            return
        await self.telegram.send_text(f"🔄 Проверяю источник {source_id}…", chat_id=chat_id)
        self.watcher.resume_source(source_id)
        report = await self.watcher.check_now(source_id)
        if report:
            await self.telegram.send_text(
                f"Готово: {report.label}\nСостояние: {report.state.value}\n"
                f"Найдено: {report.found}, новых: {report.new}, отправлено: {report.notified}"
                + (f"\n⚠️ {report.error}" if report.error else ""), chat_id=chat_id)

    async def _cmd_pause(self, chat_id: str, args: str) -> None:
        if args.isdigit():
            ok = self.watcher.pause_source(int(args))
            await self.telegram.send_text(f"⏸ Источник {args} на паузе" if ok else "Нет такого источника",
                                          chat_id=chat_id)
        else:
            self.watcher.paused_globally = True
            await self.telegram.send_text("⏸ Парсер на паузе целиком. /resume — продолжить", chat_id=chat_id)

    async def _cmd_resume(self, chat_id: str, args: str) -> None:
        if args.isdigit():
            ok = self.watcher.resume_source(int(args))
            await self.telegram.send_text(f"▶️ Источник {args} возобновлён" if ok else "Нет такого источника",
                                          chat_id=chat_id)
        else:
            self.watcher.paused_globally = False
            for source in self.db.list_sources():
                if source.status in (SourceStatus.PAUSED.value, SourceStatus.CAPTCHA.value):
                    self.watcher.resume_source(source.id)
            await self.telegram.send_text("▶️ Продолжаю работу", chat_id=chat_id)

    async def _cmd_answer(self, chat_id: str, args: str) -> None:
        await self._answer(chat_id, args)

    async def _answer(self, chat_id: str, text: str) -> None:
        if not text:
            await self.telegram.send_text("Формат: /answer <текст с картинки>", chat_id=chat_id)
            return
        pending = self.manual.push_answer(text)
        if pending is None:
            await self.telegram.send_text("Сейчас нет капчи, ожидающей ответа.", chat_id=chat_id)
        else:
            await self.telegram.send_text(f"Пробую ответ <code>{text}</code>…", chat_id=chat_id)

    async def _cmd_solved(self, chat_id: str, args: str) -> None:
        pending = self.manual.mark_done()
        await self.telegram.send_text("Проверяю страницу…" if pending else "Активной капчи нет.",
                                      chat_id=chat_id)

    async def _cmd_stats(self, chat_id: str, args: str) -> None:
        stats = self.db.stats()
        captcha = self.watcher.solver.stats() if self.watcher.solver else {}
        lines = [
            "<b>Статистика</b>",
            f"Объявлений в базе: {stats['listings']} (за сутки +{stats['last24h_new']})",
            f"Отправлено: {stats['notified']}, в очереди: {stats['pending']}",
            f"Источников: {stats['sources']} (активных {stats['sources_active']})",
            f"Размер БД: {stats['db_size_mb']} МБ",
            "",
            "<b>Капча</b>",
            f"Всего случаев: {stats['captcha_total']}",
            f"Решено автоматически: {stats['captcha_solved_auto']}",
            f"Решено вручную: {stats['captcha_solved_manual']}",
            f"Локальный OCR: {captcha.get('ocr_backend', '—')}",
            f"Ждут решения сейчас: {captcha.get('pending', 0)}",
        ]
        await self.telegram.send_text("\n".join(lines), chat_id=chat_id)

    async def _cmd_status(self, chat_id: str, args: str) -> None:
        health = await self.watcher.engine.health()
        uptime = int(time.time() - self.watcher.started_at) if self.watcher.started_at else 0
        lines = [
            "<b>Состояние</b>",
            f"Аптайм: {uptime // 3600}ч {(uptime % 3600) // 60}м, циклов: {self.watcher.cycles}",
            f"Движок: {health.get('engine')} "
            f"({'headless' if health.get('headless') else 'headed'}, страниц: {health.get('pages_fetched', 0)})",
            f"Прокси: {'да' if health.get('proxy') else 'нет'}",
            f"Пауза: {'да' if self.watcher.paused_globally else 'нет'}",
            f"Последнее состояние страницы: {health.get('last_state', '—')}",
            f"Telegram: отправлено {self.telegram.sent_count}, ошибок {self.telegram.error_count}",
        ]
        pending = self.manual.active
        if pending:
            lines.append(f"🧩 Ждут капчу: {', '.join(p.source_label for p in pending)}")
        await self.telegram.send_text("\n".join(lines), chat_id=chat_id)

    async def _cmd_export(self, chat_id: str, args: str) -> None:
        rows = self.db.export_listings(limit=2000)
        if not rows:
            await self.telegram.send_text("База пуста.", chat_id=chat_id)
            return
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        data = buffer.getvalue().encode("utf-8-sig")
        try:
            await self.telegram.api("sendDocument", {"chat_id": chat_id, "caption": "Выгрузка объявлений"},
                                    files={"document": ("avito_listings.csv", data, "text/csv")})
        except Exception as exc:
            await self.telegram.send_text(f"Не смог отправить файл: {exc}", chat_id=chat_id)