"""Локальное распознавание капчи-картинки (#inner-captcha).

Никаких внешних сервисов: модель крутится на вашей машине.
Поддерживаются два бэкенда:
  * ddddocr  — готовая лёгкая ONNX-модель (~15 МБ), ставится pip, работает офлайн;
  * onnx     — своя модель (CNN+CTC), путь и алфавит задаются в конфиге.
Если ни одного бэкенда нет — модуль честно возвращает «не могу» и капча
уходит на ручное решение с уведомлением в Telegram.
"""
from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Any

from ..config import CaptchaConfig

log = logging.getLogger(__name__)


def preprocess_variants(png: bytes, count: int = 3) -> list[bytes]:
    """Несколько вариантов предобработки: разная капча любит разную подготовку."""
    try:
        import numpy as np
        from PIL import Image, ImageFilter, ImageOps
    except ImportError:
        return [png]

    try:
        img = Image.open(io.BytesIO(png)).convert("RGB")
    except Exception:
        return [png]

    out: list[bytes] = [png]

    def save(image) -> bytes:
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()

    # 1) апскейл + серый + автоконтраст
    gray = ImageOps.grayscale(img)
    scale = 2 if max(img.size) < 400 else 1
    if scale > 1:
        gray = gray.resize((gray.width * scale, gray.height * scale), Image.LANCZOS)
    out.append(save(ImageOps.autocontrast(gray)))

    # 2) медианный фильтр + бинаризация по Оцу
    try:
        arr = np.asarray(gray.filter(ImageFilter.MedianFilter(3)))
        hist = np.histogram(arr, bins=256, range=(0, 256))[0]
        total = arr.size
        sum_all = float(np.dot(np.arange(256), hist))
        sum_b = weight_b = 0.0
        best_t, best_var = 127, -1.0
        for t in range(256):
            weight_b += hist[t]
            if weight_b == 0:
                continue
            weight_f = total - weight_b
            if weight_f == 0:
                break
            sum_b += t * hist[t]
            m_b = sum_b / weight_b
            m_f = (sum_all - sum_b) / weight_f
            var = weight_b * weight_f * (m_b - m_f) ** 2
            if var > best_var:
                best_var, best_t = var, t
        binary = (arr > best_t) * 255
        out.append(save(Image.fromarray(binary.astype("uint8"))))
    except Exception as exc:
        log.debug("Бинаризация не удалась: %s", exc)

    # 3) резкость
    out.append(save(gray.filter(ImageFilter.UnsharpMask(radius=2, percent=180, threshold=2))))
    return out[:max(1, count)]


class LocalOCR:
    """Распознаёт текст на капче локально. Потокобезопасен для одного воркера."""

    def __init__(self, cfg: CaptchaConfig):
        self.cfg = cfg
        self._backend: str = "none"
        self._dd: Any = None
        self._session: Any = None
        self._input_name: str = ""
        self._output_name: str = ""
        self._charset: str = cfg.onnx_charset
        self._init_backend()

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def available(self) -> bool:
        return self._backend != "none"

    def _init_backend(self) -> None:
        want = (self.cfg.ocr_backend or "auto").lower()
        if want == "none":
            return
        if want in ("auto", "ddddocr"):
            try:
                import ddddocr  # type: ignore
                self._dd = ddddocr.DdddOcr(show_ad=False)
                self._backend = "ddddocr"
                log.info("Локальный OCR: ddddocr (офлайн)")
                return
            except Exception as exc:
                if want == "ddddocr":
                    log.warning("ddddocr недоступен: %s", exc)
                else:
                    log.debug("ddddocr недоступен: %s", exc)
        if want in ("auto", "onnx"):
            path = Path(self.cfg.onnx_model_path)
            if path.exists():
                try:
                    import onnxruntime as ort  # type: ignore
                    self._session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
                    self._input_name = self._session.get_inputs()[0].name
                    self._output_name = self._session.get_outputs()[0].name
                    self._backend = "onnx"
                    log.info("Локальный OCR: ONNX-модель %s", path)
                    return
                except Exception as exc:
                    log.warning("ONNX-модель не загрузилась: %s", exc)
            elif want == "onnx":
                log.warning("ONNX-модель не найдена: %s", path)
        log.info("Локальный OCR не настроен — капча-картинка будет решаться вручную "
                 "(поставьте: pip install ddddocr)")

    # ------------------------------------------------------------------ recognize
    def recognize(self, png: bytes) -> list[str]:
        """Возвращает кандидатов, отсортированных по правдоподобности."""
        if not self.available or not png:
            return []
        candidates: list[str] = []
        for variant in preprocess_variants(png, self.cfg.ocr_variants):
            text = self._run(variant)
            text = self._normalize(text)
            if text and text not in candidates:
                candidates.append(text)
        # короткие/длинные ответы менее вероятны, чем 4-6 символов
        candidates.sort(key=lambda t: (0 if 4 <= len(t) <= 6 else 1, len(t)))
        return candidates

    def _normalize(self, text: str | None) -> str:
        if not text:
            return ""
        text = re.sub(r"[^0-9a-zA-Zа-яА-Я]", "", text).lower()
        return text[:12]

    def _run(self, png: bytes) -> str:
        if self._backend == "ddddocr":
            try:
                return str(self._dd.classification(png) or "")
            except Exception as exc:
                log.debug("ddddocr: %s", exc)
                return ""
        if self._backend == "onnx":
            return self._run_onnx(png)
        return ""

    def _run_onnx(self, png: bytes) -> str:
        try:
            import numpy as np
            from PIL import Image
        except ImportError:
            return ""
        try:
            img = Image.open(io.BytesIO(png)).convert("L")
            shape = self._session.get_inputs()[0].shape
            height = shape[2] if isinstance(shape[2], int) else 64
            width = shape[3] if isinstance(shape[3], int) else 192
            img = img.resize((width, height), Image.BILINEAR)
            arr = np.asarray(img, dtype="float32") / 255.0
            arr = (arr - 0.5) / 0.5
            tensor = arr[None, None, :, :]
            outputs = self._session.run([self._output_name], {self._input_name: tensor})[0]
            return _ctc_decode(outputs, self._charset)
        except Exception as exc:
            log.warning("ONNX-инференс не удался: %s", exc)
            return ""


def _ctc_decode(output: Any, charset: str) -> str:
    """Жадный CTC-декодер: (T, C) или (1, T, C), blank = последний индекс."""
    import numpy as np

    arr = np.asarray(output)
    if arr.ndim == 3:
        arr = arr[0]
    if arr.ndim != 2:
        return ""
    blank = arr.shape[1] - 1
    ids = arr.argmax(axis=1)
    chars: list[str] = []
    prev = -1
    for idx in ids:
        idx = int(idx)
        if idx != prev and idx != blank and 0 <= idx < len(charset):
            chars.append(charset[idx])
        prev = idx
    return "".join(chars)