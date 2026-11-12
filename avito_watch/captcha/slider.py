"""Локальное CV для слайдер-капчи (GeeTest v4): ищем, куда двигать пазл.

Никаких внешних сервисов — только OpenCV на вашей машине:
  * template — сопоставление контуров кусочка и фона (точнее, если кусочек виден);
  * edge     — поиск «выемки» по вертикальным границам, когда кусочка нет.

Важно: найденное смещение — только половина дела. GeeTest оценивает ещё и
поведение (траекторию, тайминги, отпечаток). Поэтому автопопытка ограничена,
а при неудаче управление уходит человеку с уведомлением в Telegram.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(slots=True)
class GapResult:
    x: float                  # координата левого края выемки в пикселях картинки фона
    confidence: float         # 0..1
    method: str
    bg_width: int
    piece_width: int
    piece_x: float = 0.0      # текущее положение кусочка на картинке фона

    @property
    def distance(self) -> float:
        return max(0.0, self.x - self.piece_x)

    def to_css(self, css_width: float) -> float:
        """Пересчёт смещения из пикселей картинки в CSS-пиксели элемента."""
        if self.bg_width <= 0 or css_width <= 0:
            return self.distance
        return self.distance * (css_width / self.bg_width)


def _decode(png: bytes):
    import cv2
    import numpy as np
    arr = np.frombuffer(png, dtype="uint8")
    return cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)


def _gray(image):
    import cv2
    if image is None:
        return None
    if image.ndim == 2:
        return image
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def _alpha_bbox(image):
    """Обрезка кусочка по альфа-каналу: у PNG пазла фон прозрачный."""
    import cv2
    import numpy as np
    if image is None or image.ndim != 3 or image.shape[2] != 4:
        return image, None
    alpha = image[:, :, 3]
    ys, xs = np.where(alpha > 32)
    if len(xs) == 0:
        return image, None
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    crop = image[y0:y1, x0:x1]
    mask = (crop[:, :, 3] > 32).astype("uint8") * 255
    return crop, mask


def find_gap(bg_png: bytes, piece_png: bytes | None = None, *, method: str = "auto") -> GapResult | None:
    try:
        import cv2
        import numpy as np
    except ImportError:
        log.info("OpenCV не установлен — авто-решение слайдера недоступно (pip install opencv-python-headless)")
        return None

    bg = _decode(bg_png)
    if bg is None:
        return None
    bg_gray = _gray(bg)
    bg_h, bg_w = bg_gray.shape[:2]

    if piece_png and method in ("auto", "template"):
        result = _template_match(bg_gray, piece_png)
        if result is not None and (result.confidence >= 0.35 or method == "template"):
            return result
    if method in ("auto", "edge"):
        return _edge_search(bg_gray)
    return None


def _template_match(bg_gray, piece_png: bytes) -> GapResult | None:
    import cv2
    import numpy as np

    piece = _decode(piece_png)
    if piece is None:
        return None
    piece_crop, mask = _alpha_bbox(piece)
    piece_gray = _gray(piece_crop)
    if piece_gray is None:
        return None

    bg_h, bg_w = bg_gray.shape[:2]
    ph, pw = piece_gray.shape[:2]
    if ph < 8 or pw < 8 or ph > bg_h or pw > bg_w:
        # кусочек пришёл в другом масштабе — приводим к высоте фона
        scale = bg_h / max(ph, 1)
        if 0.2 < scale < 5:
            piece_gray = cv2.resize(piece_gray, (max(8, int(pw * scale)), max(8, int(ph * scale))))
            if mask is not None:
                mask = cv2.resize(mask, piece_gray.shape[::-1])
            ph, pw = piece_gray.shape[:2]
        if ph > bg_h or pw > bg_w:
            return None

    bg_edges = cv2.Canny(cv2.GaussianBlur(bg_gray, (3, 3), 0), 60, 160)
    piece_edges = cv2.Canny(cv2.GaussianBlur(piece_gray, (3, 3), 0), 60, 160)

    if mask is not None:
        piece_edges = cv2.bitwise_and(piece_edges, piece_edges, mask=mask)
        response = cv2.matchTemplate(bg_edges, piece_edges, cv2.TM_CCORR_NORMED, mask=mask)
    else:
        response = cv2.matchTemplate(bg_edges, piece_edges, cv2.TM_CCOEFF_NORMED)
    response = np.nan_to_num(response, nan=0.0, posinf=0.0, neginf=0.0)

    # выемка не может быть левее исходного положения кусочка
    min_x = max(1, int(pw * 0.9))
    if min_x < response.shape[1]:
        response[:, :min_x] = -1.0
    _, max_val, _, max_loc = cv2.minMaxLoc(response)
    if max_val <= 0:
        return None
    return GapResult(x=float(max_loc[0]), confidence=float(max(0.0, min(1.0, max_val))),
                     method="template", bg_width=bg_w, piece_width=pw, piece_x=0.0)


def _edge_search(bg_gray) -> GapResult | None:
    """Ищем пару вертикальных границ на расстоянии «ширины пазла» друг от друга."""
    import cv2
    import numpy as np

    bg_h, bg_w = bg_gray.shape[:2]
    blur = cv2.GaussianBlur(bg_gray, (5, 5), 0)
    sobel = cv2.Sobel(blur, cv2.CV_32F, 1, 0, ksize=3)
    profile = np.abs(sobel).mean(axis=0)
    if profile.max() <= 0:
        return None
    profile = profile / profile.max()
    kernel = np.ones(3) / 3
    profile = np.convolve(profile, kernel, mode="same")

    start = int(bg_w * 0.25)          # слева обычно стоит сам кусочек
    candidates = []
    for width in range(max(24, int(bg_w * 0.10)), max(40, int(bg_w * 0.28))):
        for x in range(start, bg_w - width - 2):
            left, right = profile[x], profile[x + width]
            inner = profile[x + 4:x + width - 4].mean() if width > 10 else 0.0
            score = (left + right) / 2 - 0.55 * inner
            candidates.append((score, x, width, min(left, right)))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    score, x, width, strength = candidates[0]
    confidence = float(max(0.0, min(1.0, 0.45 * score + 0.55 * strength)))
    return GapResult(x=float(x), confidence=confidence, method="edge",
                     bg_width=bg_w, piece_width=width, piece_x=0.0)