"""Интеграционные тесты на НАСТОЯЩЕМ Chromium против локального «мини-Авито».

Проверяют то, что нельзя проверить фейковым движком: proof-of-work, живой
клик/ввод в капчу, перезагрузку страницы после решения, обогащение объявления.

Запуск:  pytest -m browser        (обычный `pytest -q` их пропускает)
Требования: playwright install chromium, шрифты (fonts-dejavu/liberation),
для теста автопроверки капчи — ddddocr.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from avito_watch.captcha.manual import ManualCenter
from avito_watch.captcha.solver import CaptchaSolver
from avito_watch.engine.browser import BrowserEngine
from avito_watch.models import PageState
from avito_watch.pipeline import Pipeline
from tests.conftest import wait_until
from tests.local_avito import LocalAvito, find_font

pytestmark = pytest.mark.browser


def _chromium_ready() -> bool:
    if importlib.util.find_spec("playwright") is None:
        return False
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            return Path(pw.chromium.executable_path).exists()
    except Exception:
        return False


CHROMIUM = _chromium_ready()
OCR_READY = importlib.util.find_spec("ddddocr") is not None
FONT_READY = find_font() is not None

skip_no_browser = pytest.mark.skipif(not CHROMIUM, reason="нет Chromium: playwright install chromium")
skip_no_ocr = pytest.mark.skipif(not (OCR_READY and FONT_READY), reason="нет ddddocr или TTF-шрифта")


@pytest.fixture
def site():
    server = LocalAvito()
    server.start()
    server.set_items({"7000000001": ("iPhone 13 128 ГБ", 42000),
                      "7000000002": ("iPhone 14 Pro 256 ГБ", 68000)})
    yield server
    server.stop()


@pytest.fixture
def bcfg(cfg, tmp_path):
    cfg.browser.headless = True
    cfg.browser.humanize = False
    cfg.browser.block_resources = False
    cfg.browser.nav_timeout_sec = 25
    cfg.browser.pow_wait_sec = 25
    cfg.browser.restart_after_pages = 0
    cfg.browser.user_data_dir = str(tmp_path / "browser-profile")
    cfg.captcha.submit_wait_sec = 2.5
    cfg.captcha.reload_wait_sec = 1.5
    cfg.captcha.poll_interval_sec = 0.3
    cfg.captcha.manual_wait_sec = 25
    cfg.captcha.notify_cooldown_sec = 0
    return cfg


@pytest.fixture
async def engine(bcfg):
    eng = BrowserEngine(bcfg.browser, session_path=str(Path(bcfg.browser.user_data_dir) / "session.json"))
    await eng.start()
    yield eng
    await eng.stop()


# --------------------------------------------------------------------- файрвол
@skip_no_browser
async def test_proof_of_work_is_waited_out(engine, site):
    """PoW: страница сама считает задачу и перезагружается. Движок должен дождаться."""
    result = await engine.fetch(site.url("/pow"))
    assert result.state is PageState.CONTENT, result.html[:300]
    assert "iPhone" in result.html
    assert site.hits.get("/verify", 0) >= 1        # JS действительно отработал
    assert site.hits.get("/pow", 0) >= 2           # страница перезагрузилась сама


@skip_no_browser
async def test_blocked_and_empty_states(engine, site):
    blocked = await engine.fetch(site.url("/blocked"))
    assert blocked.state is PageState.BLOCKED

    empty = await engine.fetch(site.url("/empty"))
    assert empty.state is PageState.EMPTY


@skip_no_browser
async def test_item_page_is_content(engine, site):
    result = await engine.fetch(site.url("/moskva/tovar_7000000001"), is_item=True)
    assert result.state is PageState.CONTENT
    assert "iPhone 13" in result.html


# --------------------------------------------------------------------- капча
@skip_no_browser
@skip_no_ocr
async def test_image_captcha_solved_by_local_ocr(bcfg, db, fake_telegram, site, engine):
    """Полный автоматический цикл: OCR → ввод ответа → submit → файрвол отпустил."""
    bcfg.captcha.ocr_backend = "auto"
    site.captcha_answer = "ab12cd"
    manual = ManualCenter()
    solver = CaptchaSolver(bcfg, db, fake_telegram, manual, engine=engine)

    result = await engine.fetch(site.url("/captcha"), captcha_handler=solver)

    assert result.state is PageState.CONTENT, f"капча не пройдена: {result.html[:300]}"
    assert solver.auto_success == 1
    assert site.captcha_answer in site.answers_tried
    assert any("решена автоматически" in m for m in fake_telegram.messages)
    assert manual.active == []                   # ожидание человека снято
    assert db.stats()["captcha_solved_auto"] == 1


@skip_no_browser
async def test_captcha_manual_answer_from_telegram(bcfg, db, fake_telegram, site, engine):
    """Автопопытка выключена → уведомление с фото → человек присылает текст → прошли."""
    bcfg.captcha.auto_solve = False
    site.captcha_answer = "x9f3"
    manual = ManualCenter()
    solver = CaptchaSolver(bcfg, db, fake_telegram, manual, engine=engine)

    async def user_answers():
        pending = await wait_until(lambda: manual.latest, timeout=20)
        assert fake_telegram.photos, "скриншот капчи не пришёл в Telegram"
        manual.push_answer("x9f3")
        return pending

    task = asyncio.create_task(user_answers())
    result = await engine.fetch(site.url("/captcha"), captcha_handler=solver)
    await task

    assert result.state is PageState.CONTENT
    assert solver.manual_success == 1
    assert any("Принято" in m for m in fake_telegram.messages)


@skip_no_browser
async def test_captcha_wrong_answer_then_skip(bcfg, db, fake_telegram, site, engine):
    """Неверный ответ → понятное сообщение; кнопка «пропустить» завершает цикл."""
    bcfg.captcha.auto_solve = False
    site.captcha_answer = "zz11"
    manual = ManualCenter()
    solver = CaptchaSolver(bcfg, db, fake_telegram, manual, engine=engine)

    async def user():
        await wait_until(lambda: manual.latest, timeout=20)
        manual.push_answer("ошибка")
        await wait_until(lambda: any("не принят" in m for m in fake_telegram.messages), timeout=20)
        manual.mark_skip()

    task = asyncio.create_task(user())
    result = await engine.fetch(site.url("/captcha"), captcha_handler=solver)
    await task

    assert result.state is PageState.CAPTCHA          # выдачу так и не отдали
    assert any("не принят" in m for m in fake_telegram.messages)
    assert solver.stats()["failed"] == 1


# --------------------------------------------------------------------- конвейер
@skip_no_browser
async def test_full_cycle_seed_then_only_new(bcfg, db, fake_telegram, site, engine):
    """Первый проход — фиксация, второй — только новое, третий — ничего. Плюс обогащение."""
    bcfg.watch.enrich_items = True
    bcfg.watch.enrich_delay_sec = 0
    pipeline = Pipeline(bcfg, db, engine, fake_telegram)
    source = db.add_source(site.url("/listing"), name="Локальный iPhone", interval_sec=60)

    first = await pipeline.check_source(source)
    assert first.state is PageState.CONTENT
    assert first.found == 2 and first.seeded == 2 and first.notified == 0
    assert fake_telegram.listings == []

    site.add_item("7000000003", "iPhone 15 Pro 256 ГБ", 91000)
    second = await pipeline.check_source(db.get_source(source.id))
    assert second.new == 1 and second.notified == 1
    assert len(fake_telegram.listings) == 1
    listing, kind = fake_telegram.listings[0]
    assert kind == "new" and listing.item_id == "7000000003"
    assert listing.price == 91000
    assert listing.image_url and listing.url.startswith(site.base_url)
    assert second.enriched == 1
    # в Telegram уходит уже обогащённое объявление: описание и дата со страницы товара
    assert listing.description and "полный комплект" in listing.description
    assert listing.published_at is not None
    stored = db.get_listing("7000000003")
    assert stored.description and "полный комплект" in stored.description
    assert stored.published_at is not None

    third = await pipeline.check_source(db.get_source(source.id))
    assert third.new == 0 and third.notified == 0
    assert len(fake_telegram.listings) == 1       # дублей нет


@skip_no_browser
async def test_price_drop_is_notified(bcfg, db, fake_telegram, site, engine):
    bcfg.watch.enrich_items = False
    bcfg.telegram.price_drop_min_pct = 5
    pipeline = Pipeline(bcfg, db, engine, fake_telegram)
    source = db.add_source(site.url("/listing"), name="Цены", interval_sec=60)

    await pipeline.check_source(source)
    site.set_items({"7000000001": ("iPhone 13 128 ГБ", 30000),
                    "7000000002": ("iPhone 14 Pro 256 ГБ", 68000)})
    report = await pipeline.check_source(db.get_source(source.id))

    assert report.price_drops == 1
    assert any(kind == "price_drop" for _, kind in fake_telegram.listings)
    history = db.price_history("7000000001")
    assert [price for _changed_at, price in history] == [30000, 42000]
