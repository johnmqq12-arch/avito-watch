"""Решатель капчи: локальная автопопытка, уведомление, ручное решение."""
import asyncio

import pytest

from avito_watch.captcha.manual import ManualCenter
from avito_watch.captcha.solver import CaptchaSolver
from avito_watch.models import CaptchaVariant
from tests.conftest import FakePage, load_html, wait_until


class FakeOCR:
    backend = "fake"
    available = True

    def __init__(self, answers):
        self.answers = answers
        self.calls = 0

    def recognize(self, png):
        self.calls += 1
        return list(self.answers)


@pytest.fixture
def manual():
    return ManualCenter()


async def test_local_ocr_solves_image_captcha(cfg, db, fake_telegram, manual):
    page = FakePage(load_html("firewall_image"), accept_answer="ab12cd")
    solver = CaptchaSolver(cfg, db, fake_telegram, manual, ocr=FakeOCR(["ab12cd"]))
    result = await solver(page, None)
    assert result.solved is True
    assert result.method == "ocr-local"
    assert page.filled[-1] == ("#form-input", "ab12cd")
    assert db.stats()["captcha_solved_auto"] == 1
    assert any("решена автоматически" in m for m in fake_telegram.messages)


async def test_wrong_ocr_falls_back_to_manual_answer(cfg, db, fake_telegram, manual):
    cfg.captcha.manual_wait_sec = 3
    page = FakePage(load_html("firewall_image"), accept_answer="right1")
    solver = CaptchaSolver(cfg, db, fake_telegram, manual, ocr=FakeOCR(["wrong1"]))

    async def user_answers():
        # пользователь отвечает, когда бот прислал капчу и ждёт ответа
        await wait_until(lambda: manual.latest is not None)
        manual.push_answer("right1")

    task = asyncio.create_task(user_answers())
    result = await solver(page, None)
    await task
    assert result.solved is True
    assert result.method == "manual-answer"
    assert fake_telegram.photos, "пользователь должен получить скриншот капчи"
    assert any("/answer" in m or "картинки" in m for m in fake_telegram.messages)


async def test_manual_browser_solve_detected(cfg, db, fake_telegram, manual):
    cfg.captcha.manual_wait_sec = 3
    page = FakePage(load_html("firewall_captcha"))
    solver = CaptchaSolver(cfg, db, fake_telegram, manual, ocr=FakeOCR([]))

    async def user_solves_in_browser():
        await wait_until(lambda: manual.latest is not None)
        page._html = load_html("listing_state")   # человек прошёл капчу в noVNC

    task = asyncio.create_task(user_solves_in_browser())
    result = await solver(page, None)
    await task
    assert result.solved is True
    assert result.method == "manual"


async def test_timeout_notifies_and_reports(cfg, db, fake_telegram, manual):
    cfg.captcha.manual_wait_sec = 0.3
    page = FakePage(load_html("firewall_captcha"))
    solver = CaptchaSolver(cfg, db, fake_telegram, manual, ocr=FakeOCR([]))
    result = await solver(page, None)
    assert result.solved is False
    assert result.method in ("timeout", "none")
    assert any("Не дождался" in m for m in fake_telegram.messages)
    assert manual.active == []


async def test_skip_button(cfg, db, fake_telegram, manual):
    cfg.captcha.manual_wait_sec = 5
    page = FakePage(load_html("firewall_captcha"))
    solver = CaptchaSolver(cfg, db, fake_telegram, manual, ocr=FakeOCR([]))

    async def user_skips():
        await wait_until(lambda: manual.latest is not None)
        manual.mark_skip()

    task = asyncio.create_task(user_skips())
    result = await solver(page, None)
    await task
    assert result.solved is False
    assert result.method == "skipped"


async def test_hcaptcha_goes_straight_to_human(cfg, db, fake_telegram, manual):
    cfg.captcha.manual_wait_sec = 0.2
    html = load_html("firewall_captcha").replace('id="geetest_captcha"', 'id="geetest_captcha" style="display: none"') \
                                        .replace('id="h-captcha" style="display: none"', 'id="h-captcha"')
    page = FakePage(html)
    solver = CaptchaSolver(cfg, db, fake_telegram, manual, ocr=FakeOCR(["x"]))
    result = await solver(page, None)
    assert result.solved is False
    assert db.stats()["captcha_total"] == 1


async def test_captcha_without_interaction_support(cfg, db, fake_telegram, manual):
    from avito_watch.engine.http import StaticPage

    cfg.captcha.manual_wait_sec = 0.2
    page = StaticPage("https://www.avito.ru/x", load_html("firewall_captcha"))
    solver = CaptchaSolver(cfg, db, fake_telegram, manual, ocr=FakeOCR(["x"]))
    result = await solver(page, None)
    assert result.solved is False
    assert "HTTP" in result.detail or result.method in ("timeout", "none")


async def test_slider_uses_cv_and_human_drag(cfg, db, fake_telegram, manual, monkeypatch):
    """Проверяем механику: находим смещение → тянем слайдер → проверяем результат."""
    from avito_watch.captcha import solver as solver_module
    from avito_watch.captcha.slider import GapResult

    cfg.captcha.manual_wait_sec = 0.2
    page = FakePage(load_html("firewall_captcha"))
    page.visible = {".geetest_slider_button", ".geetest_bg", ".geetest_slice_bg",
                    "form.js-firewall-form button"}
    monkeypatch.setattr(solver_module, "find_gap",
                        lambda *a, **k: GapResult(x=180.0, confidence=0.8, method="template",
                                                  bg_width=300, piece_width=50))

    async def solve_after_drag(*args, **kwargs):
        page._html = load_html("listing_state")
        return True

    page.drag = solve_after_drag
    solver = CaptchaSolver(cfg, db, fake_telegram, manual, ocr=FakeOCR([]))
    result = await solver(page, None)
    assert result.solved is True
    assert result.method == "slider-cv"


def test_manual_center_routing():
    center = ManualCenter()
    first = center.register(1, 10, CaptchaVariant.IMAGE, "источник 10")
    second = center.register(2, 20, CaptchaVariant.GEETEST, "источник 20")
    assert center.latest is second
    assert center.for_source(10) is first
    center.push_answer("abc", source_id=10)
    assert first.answers.get_nowait() == "abc"
    center.mark_done(2)
    assert second.done.is_set()
    center.clear(2)
    assert center.latest is None or center.latest is first