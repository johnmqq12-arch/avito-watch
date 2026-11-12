"""Локальный OCR капчи-картинки: предобработка, CTC и реальная офлайн-модель.

Тест с ddddocr помечен как необязательный: он проверяет, что «ИИ на своей машине»
действительно читает капчу, но не требует установки модели для обычных тестов.
"""
import io

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from avito_watch.captcha.image_ocr import LocalOCR, _ctc_decode, preprocess_variants
from avito_watch.config import CaptchaConfig


def render_captcha(text: str, *, noise: bool = True, size=(180, 64)) -> bytes:
    """Рисуем капчу сами: шрифт берётся встроенный в Pillow, внешних файлов нет."""
    img = Image.new("RGB", size, (250, 250, 250))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=38)
    for i, char in enumerate(text):
        draw.text((10 + i * 26, 8 + (i % 2) * 5), char, font=font, fill=(25 + i * 8, 40, 95))
    if noise:
        for x in range(-20, size[0], 9):
            draw.line([(x, 0), (x + 24, size[1])], fill=(170, 175, 200), width=1)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_preprocess_variants_are_valid_images():
    png = render_captcha("ab12cd")
    variants = preprocess_variants(png, count=3)
    assert len(variants) == 3
    assert variants[0] == png                       # оригинал всегда первый
    for variant in variants:
        with Image.open(io.BytesIO(variant)) as img:
            img.load()
            assert img.width > 0


def test_preprocess_survives_garbage():
    garbage = "не картинка".encode("utf-8")
    assert preprocess_variants(garbage, 3) == [garbage]


def test_preprocess_respects_count():
    assert len(preprocess_variants(render_captcha("x9f3"), count=1)) == 1


def test_ctc_decode_collapses_repeats_and_drops_blank():
    charset = "0123456789"                 # blank = последний индекс (9)
    # a a 0 1 1 blank 2 → "012"
    rows = [[0] * 10 for _ in range(7)]
    for step, idx in enumerate([0, 0, 0, 1, 1, 9, 2]):
        rows[step][idx] = 1.0
    assert _ctc_decode(np.array([rows], dtype="float32"), charset) == "012"


def test_ctc_decode_handles_bad_shape():
    assert _ctc_decode(np.zeros((5,), dtype="float32"), "abc") == ""


def test_backend_none_is_honest():
    ocr = LocalOCR(CaptchaConfig(ocr_backend="none"))
    assert ocr.available is False and ocr.backend == "none"
    assert ocr.recognize(render_captcha("ab12cd")) == []


def test_missing_onnx_model_does_not_crash(tmp_path):
    ocr = LocalOCR(CaptchaConfig(ocr_backend="onnx",
                                 onnx_model_path=str(tmp_path / "нет-такой.onnx")))
    assert ocr.available is False


def test_candidates_are_deduped_and_sorted(monkeypatch):
    ocr = LocalOCR(CaptchaConfig(ocr_backend="none"))
    ocr._backend = "fake"                            # притворяемся, что модель есть
    answers = iter(["toolonganswer", "ab12", "ab12", "x9"])
    monkeypatch.setattr(ocr, "_run", lambda png: next(answers, ""))
    monkeypatch.setattr("avito_watch.captcha.image_ocr.preprocess_variants",
                        lambda png, count: [png] * 4)
    candidates = ocr.recognize(b"png")
    assert candidates[0] == "ab12"                   # 4-6 символов — самые вероятные
    assert candidates.count("ab12") == 1             # дубли убраны
    assert "toolonganswe" in candidates              # ответ обрезается до 12 символов


def test_normalize_strips_junk(monkeypatch):
    ocr = LocalOCR(CaptchaConfig(ocr_backend="none"))
    assert ocr._normalize(" A B-1_2! ") == "ab12"
    assert ocr._normalize(None) == ""


@pytest.mark.parametrize("text", ["ab12cd", "7k4m2", "x9f3", "82hd"])
def test_real_local_model_reads_captcha(text):
    """Главное обещание проекта: капча-картинка решается ЛОКАЛЬНО, без сервисов."""
    pytest.importorskip("ddddocr", reason="локальная модель не установлена (requirements-ocr.txt)")
    ocr = LocalOCR(CaptchaConfig(ocr_backend="auto"))
    if ocr.backend != "ddddocr":
        pytest.skip(f"доступен другой бэкенд: {ocr.backend}")
    candidates = ocr.recognize(render_captcha(text))
    assert text in candidates, f"ожидали {text}, модель дала {candidates}"
