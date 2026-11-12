"""Оркестратор капчи: сначала локальная автопопытка, потом человек — но всегда с уведомлением.

Порядок:
  1. определить тип капчи (GeeTest-слайдер / картинка Авито / hCaptcha);
  2. попробовать решить ЛОКАЛЬНО (OpenCV / локальная OCR-модель), без внешних сервисов;
  3. не вышло — прислать в Telegram скриншот + кнопки, ждать ручного решения
     (noVNC / headed-браузер / ответ текстом на капчу-картинку);
  4. как только файрвол отпустил — сохранить сессию и продолжить работу.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from ..config import Config
from ..db import Database
from ..engine.base import PageAdapter
from ..models import CaptchaChallenge, CaptchaVariant, PageState, SolveResult
from ..notify.formatting import captcha_keyboard, captcha_message
from ..notify.telegram import Telegram
from ..parser.state import detect_state
from .detect import build_challenge
from .image_ocr import LocalOCR
from .manual import ManualCenter
from .slider import find_gap

log = logging.getLogger(__name__)

# Селекторы GeeTest v4 меняются между сборками — держим несколько кандидатов.
_CONTINUE_SELECTORS = ("form.js-firewall-form button", ".js-firewall-form button",
                       "#captcha-submit", "button[type=submit]")
_SLIDER_SELECTORS = (".geetest_slider_button", ".geetest_btn", ".geetest_slider",
                     "[class*=slider_button]", "[class*=geetest_btn]")
_BG_SELECTORS = (".geetest_bg", "canvas.geetest_canvas_bg", "[class*=geetest_bg]",
                 ".geetest_canvas_img canvas", ".geetest_widget canvas")
_PIECE_SELECTORS = (".geetest_slice_bg", ".geetest_slice", "canvas.geetest_canvas_slice",
                    "[class*=geetest_slice]")
_REFRESH_SELECTORS = (".geetest_refresh", "[class*=geetest_refresh]")
_IMAGE_INPUT = "#form-input"


class CaptchaSolver:
    def __init__(self, cfg: Config, db: Database, telegram: Telegram, manual: ManualCenter,
                 *, engine=None, ocr: LocalOCR | None = None):
        self.cfg = cfg
        self.db = db
        self.telegram = telegram
        self.manual = manual
        self.engine = engine
        self.ocr = ocr if ocr is not None else LocalOCR(cfg.captcha)
        self.auto_success = 0
        self.manual_success = 0
        self.failures = 0

    # точка входа для движка
    async def __call__(self, page: PageAdapter, source_id: int | None) -> SolveResult:
        ccfg = self.cfg.captcha
        html = await page.html()
        challenge = await build_challenge(page, html)
        source = self.db.get_source(source_id) if source_id is not None else None
        label = source.label if source else "вне источника"
        shot_path = self._save_screenshot(challenge, source_id)
        prev_captcha_at = self.db.last_captcha_at(source_id)
        event_id = self.db.log_captcha(source_id, challenge.variant.value, page.url,
                                       screenshot=str(shot_path) if shot_path else None)

        # ---------- 1. локальная автопопытка ----------
        result = SolveResult(solved=False, method="none")
        if ccfg.auto_solve and page.supports_interaction:
            if challenge.variant is CaptchaVariant.IMAGE:
                result = await self._solve_image(page, challenge)
            elif challenge.variant is CaptchaVariant.GEETEST and ccfg.slider_enabled:
                result = await self._solve_slider(page, challenge)
            elif challenge.variant is CaptchaVariant.HCAPTCHA:
                result = SolveResult(solved=False, method="none",
                                     detail="hCaptcha локально не решается — нужен человек")
            else:
                result = SolveResult(solved=False, method="none", detail="тип капчи не распознан")
        elif not page.supports_interaction:
            result = SolveResult(solved=False, method="none",
                                 detail="HTTP-движок не умеет взаимодействовать со страницей")

        if result.solved:
            self.auto_success += 1
            self.db.resolve_captcha(event_id, result.method, True, result.detail)
            await self._save_session()
            await self.telegram.send_text(
                f"🤖 Капча решена автоматически (<code>{result.method}</code>).\n"
                f"🔎 {label}. Продолжаю работу.")
            return result

        # ---------- 2. уведомление + ручное решение ----------
        self.failures += 1
        self.db.resolve_captcha(event_id, result.method or "none", False, result.detail)
        if not ccfg.notify:
            return result
        manual_result = await self._wait_manual(page, challenge, event_id, source_id, label, result,
                                                prev_captcha_at=prev_captcha_at)
        if manual_result.solved:
            self.manual_success += 1
            self.db.resolve_captcha(event_id, manual_result.method, True, manual_result.detail)
            await self._save_session()
        else:
            self.db.resolve_captcha(event_id, manual_result.method, False, manual_result.detail)
        return manual_result

    # ------------------------------------------------------------------ image OCR
    async def _solve_image(self, page: PageAdapter, challenge: CaptchaChallenge) -> SolveResult:
        if not self.ocr.available:
            return SolveResult(solved=False, method="ocr-local",
                               detail="локальная OCR-модель не установлена (pip install ddddocr)")
        image = challenge.image_bytes
        if not image:
            return SolveResult(solved=False, method="ocr-local", detail="не нашёл картинку капчи")
        candidates = await asyncio.to_thread(self.ocr.recognize, image)
        if not candidates:
            return SolveResult(solved=False, method="ocr-local", detail="OCR не дал кандидатов")
        attempts = 0
        for answer in candidates[:max(1, self.cfg.captcha.max_auto_attempts)]:
            attempts += 1
            log.info("Пробую локальный OCR-ответ: %s", answer)
            if not await page.fill(_IMAGE_INPUT, answer):
                return SolveResult(solved=False, method="ocr-local", attempts=attempts,
                                   detail="не нашёл поле ввода капчи")
            await page.submit_firewall_form(input_selector=_IMAGE_INPUT)
            await asyncio.sleep(self.cfg.captcha.submit_wait_sec)
            if await self._passed(page):
                self._store_sample(challenge, answer)
                return SolveResult(solved=True, method="ocr-local", attempts=attempts,
                                   detail=f"ответ {answer}", answer=answer)
        return SolveResult(solved=False, method="ocr-local", attempts=attempts,
                           detail=f"OCR-ответы не приняты ({', '.join(candidates[:3])})")

    # ------------------------------------------------------------------ GeeTest slider
    async def _solve_slider(self, page: PageAdapter, challenge: CaptchaChallenge) -> SolveResult:
        ccfg = self.cfg.captcha
        attempts = 0
        last_detail = "виджет слайдера не появился"
        for attempt in range(max(1, ccfg.slider_max_tries)):
            attempts += 1
            if await self._passed(page):
                return SolveResult(solved=True, method="manual", attempts=attempts,
                                   detail="капча пройдена человеком, пока искали виджет")
            await self._click_continue(page)
            slider_selector = await self._wait_any(page, _SLIDER_SELECTORS,
                                                  timeout=ccfg.widget_timeout_sec)
            if not slider_selector:
                last_detail = "не дождался слайдер GeeTest"
                await self._refresh_puzzle(page)
                continue
            bg_selector = await self._first_visible(page, _BG_SELECTORS)
            if not bg_selector:
                last_detail = "не нашёл фон пазла"
                await self._refresh_puzzle(page)
                continue
            bg_png = await page.element_screenshot(bg_selector)
            if not bg_png:
                last_detail = "не снял фон пазла"
                continue
            piece_selector = await self._first_visible(page, _PIECE_SELECTORS)
            piece_png = await page.element_screenshot(piece_selector) if piece_selector else None

            gap = await asyncio.to_thread(find_gap, bg_png, piece_png, method=ccfg.slider_gap_method)
            if gap is None:
                last_detail = "OpenCV не нашёл выемку (или не установлен)"
                await self._refresh_puzzle(page)
                continue

            bg_box = await page.bounding_box(bg_selector)
            slider_box = await page.bounding_box(slider_selector)
            if not bg_box or not slider_box:
                last_detail = "не получил координаты элементов"
                continue
            scale = bg_box["width"] / gap.bg_width if gap.bg_width else 1.0
            gap_css = gap.x * scale
            piece_offset_css = 0.0
            if piece_selector:
                piece_box = await page.bounding_box(piece_selector)
                if piece_box:
                    piece_offset_css = max(0.0, piece_box["x"] - bg_box["x"])
            distance = gap_css - piece_offset_css
            if distance <= 3:
                last_detail = f"смещение слишком мало ({distance:.1f}px)"
                await self._refresh_puzzle(page)
                continue
            # небольшой человеческий недолёт/перелёт
            distance += (-1.5 + 3.0 * ((attempt % 3) / 2))
            start = (slider_box["x"] + slider_box["width"] / 2,
                     slider_box["y"] + slider_box["height"] / 2)
            log.info("Слайдер: метод=%s уверенность=%.2f смещение=%.1fpx", gap.method, gap.confidence, distance)
            await page.drag(start, distance, duration=1.0 + 0.25 * attempt)
            await asyncio.sleep(ccfg.slider_settle_sec)
            if await self._passed(page):
                return SolveResult(solved=True, method="slider-cv", attempts=attempts,
                                   detail=f"{gap.method}, conf={gap.confidence:.2f}, {distance:.0f}px")
            last_detail = f"GeeTest отклонил (conf={gap.confidence:.2f}, {distance:.0f}px)"
            await self._refresh_puzzle(page)
        return SolveResult(solved=False, method="slider-cv", attempts=attempts, detail=last_detail)

    async def _click_continue(self, page: PageAdapter) -> None:
        pause = self.cfg.captcha.click_wait_sec
        for selector in _CONTINUE_SELECTORS:
            if await page.is_visible(selector):
                await page.click(selector)
                await asyncio.sleep(pause)
                return
        clicked = await page.evaluate("""() => {
            const btn = [...document.querySelectorAll('button, input[type=submit], a')]
              .find(b => /продолжить/i.test(b.textContent || b.value || ''));
            if (btn) { btn.click(); return true; }
            return false;
        }""")
        if clicked:
            await asyncio.sleep(pause)

    async def _refresh_puzzle(self, page: PageAdapter) -> None:
        pause = self.cfg.captcha.click_wait_sec
        for selector in _REFRESH_SELECTORS:
            if await page.is_visible(selector):
                await page.click(selector)
                await asyncio.sleep(pause)
                return
        await asyncio.sleep(pause * 0.75)

    async def _wait_any(self, page: PageAdapter, selectors: tuple[str, ...], timeout: float) -> str | None:
        deadline = time.time() + timeout
        poll = max(0.05, min(0.6, self.cfg.captcha.poll_interval_sec))
        while time.time() < deadline:
            for selector in selectors:
                if await page.is_visible(selector):
                    return selector
            await asyncio.sleep(poll)
        return None

    async def _first_visible(self, page: PageAdapter, selectors: tuple[str, ...]) -> str | None:
        for selector in selectors:
            if await page.is_visible(selector):
                return selector
        return None

    # ------------------------------------------------------------------ manual
    async def _wait_manual(self, page: PageAdapter, challenge: CaptchaChallenge, event_id: int,
                           source_id: int | None, label: str, auto: SolveResult,
                           prev_captcha_at: float | None = None) -> SolveResult:
        ccfg = self.cfg.captcha
        pending = self.manual.register(event_id, source_id, challenge.variant, label)
        # антиспам: если капча прилетела только что, повторное фото не шлём
        should_notify = (prev_captcha_at is None
                         or time.time() - prev_captcha_at > ccfg.notify_cooldown_sec)
        message_id = None
        if should_notify:
            text = captcha_message(source_label=label, variant=challenge.variant.value,
                                   auto_tried=self.cfg.captcha.auto_solve,
                                   auto_detail=auto.detail or "—",
                                   manual_url=ccfg.manual_url,
                                   image_captcha=challenge.variant is CaptchaVariant.IMAGE)
            keyboard = captcha_keyboard(event_id=event_id, source_id=source_id, manual_url=ccfg.manual_url)
            message_id = await self.telegram.send_photo(challenge.screenshot, text, reply_markup=keyboard)
            pending.message_id = message_id

        deadline = time.time() + ccfg.manual_wait_sec
        log.warning("Капча требует человека: источник=%s вариант=%s, жду до %.0fs",
                    label, challenge.variant.value, ccfg.manual_wait_sec)
        try:
            while time.time() < deadline:
                if pending.skip.is_set():
                    return SolveResult(solved=False, method="skipped", detail="пользователь пропустил цикл")

                # ответ текстом на капчу-картинку
                try:
                    answer = pending.answers.get_nowait()
                except asyncio.QueueEmpty:
                    answer = None
                if answer:
                    await page.fill(_IMAGE_INPUT, answer)
                    await page.submit_firewall_form(input_selector=_IMAGE_INPUT)
                    await asyncio.sleep(ccfg.submit_wait_sec)
                    if await self._passed(page):
                        self._store_sample(challenge, answer)
                        await self.telegram.send_text(f"✅ Принято! Капча решена ответом <code>{answer}</code>. "
                                                      f"Продолжаю работу.")
                        return SolveResult(solved=True, method="manual-answer", detail=f"ответ {answer}",
                                           answer=answer)
                    await self.telegram.send_text("❌ Ответ не принят. Пришлите новый текст с картинки "
                                                  "или пройдите капчу в браузере.")

                # человек мог решить в браузере сам — проверяем состояние страницы
                if await self._passed(page):
                    await self.telegram.send_text("✅ Капча пройдена, файрвол отпустил. Продолжаю работу.")
                    return SolveResult(solved=True, method="manual", detail="решено человеком")

                if pending.done.is_set():
                    pending.done.clear()
                    await page.reload()
                    await asyncio.sleep(ccfg.reload_wait_sec)
                    if await self._passed(page):
                        await self.telegram.send_text("✅ Подтвердил: капча пройдена. Продолжаю работу.")
                        return SolveResult(solved=True, method="manual", detail="подтверждено кнопкой")
                    await self.telegram.send_text("Пока ещё вижу капчу — попробуйте решить и нажать кнопку снова.")

                await asyncio.sleep(ccfg.poll_interval_sec)

            await self.telegram.send_text(
                f"⏳ Не дождался решения капчи за {int(ccfg.manual_wait_sec / 60)} мин. "
                f"Источник <b>{label}</b> поставлен на паузу.\n"
                f"Когда будете готовы: пройдите капчу и отправьте /resume {source_id or ''}".strip())
            return SolveResult(solved=False, method="timeout", detail="истёк срок ожидания человека")
        finally:
            self.manual.clear(event_id)

    # ------------------------------------------------------------------ helpers
    async def _passed(self, page: PageAdapter) -> bool:
        html = await page.html()
        if not html:
            return False
        return detect_state(html) not in (PageState.CAPTCHA, PageState.CHALLENGE, PageState.BLOCKED)

    async def _save_session(self) -> None:
        if self.engine is not None:
            try:
                path = getattr(self.engine, "session_path", "data/session.json")
                await self.engine.save_session(path)
            except Exception as exc:
                log.debug("save_session после капчи: %s", exc)

    def _save_screenshot(self, challenge: CaptchaChallenge, source_id: int | None) -> Path | None:
        if not challenge.screenshot:
            return None
        directory = Path(self.cfg.storage.captcha_dir)
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{int(time.time())}_{source_id or 0}_{challenge.variant.value}.png"
        path = directory / name
        try:
            path.write_bytes(challenge.screenshot)
        except OSError as exc:
            log.debug("Не сохранил скриншот капчи: %s", exc)
            return None
        return path

    def _store_sample(self, challenge: CaptchaChallenge, label: str) -> None:
        """Копим размеченный датасет: ваши ручные ответы = метки для дообучения модели."""
        if not self.cfg.captcha.save_samples or not challenge.image_bytes:
            return
        directory = Path(self.cfg.storage.captcha_dir) / "dataset"
        directory.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in label if c.isalnum())[:12] or "unknown"
        path = directory / f"{int(time.time())}_{safe}.png"
        try:
            path.write_bytes(challenge.image_bytes)
            self.db.add_captcha_sample(str(path), challenge.variant.value, label)
        except OSError as exc:
            log.debug("Не сохранил образец капчи: %s", exc)

    def stats(self) -> dict[str, int | str]:
        return {
            "ocr_backend": self.ocr.backend,
            "auto_solved": self.auto_success,
            "manual_solved": self.manual_success,
            "failed": self.failures,
            "pending": len(self.manual.active),
        }