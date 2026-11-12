"""Локальное CV слайдера и человеческая траектория."""
import io
import math

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from avito_watch.captcha.slider import find_gap          # noqa: E402
from avito_watch.captcha.trajectory import human_track   # noqa: E402


def synth_captcha(width=340, height=170, gap_x=210, piece=52):
    """Синтетический пазл: фон с текстурами, выемка и кусочек с альфа-каналом."""
    rng = np.random.default_rng(42)
    bg = rng.integers(60, 200, size=(height, width, 3), dtype=np.uint8)
    for _ in range(18):
        x, y = int(rng.integers(0, width - 40)), int(rng.integers(0, height - 40))
        color = tuple(int(c) for c in rng.integers(0, 255, size=3))
        cv2.rectangle(bg, (x, y), (x + 35, y + 25), color, -1)
    bg = cv2.GaussianBlur(bg, (5, 5), 0)

    y0 = (height - piece) // 2
    patch = bg[y0:y0 + piece, gap_x:gap_x + piece].copy()
    # выемка: затемнённый прямоугольник с bright контуром
    cv2.rectangle(bg, (gap_x, y0), (gap_x + piece, y0 + piece), (25, 25, 25), -1)
    cv2.rectangle(bg, (gap_x, y0), (gap_x + piece, y0 + piece), (240, 240, 240), 2)
    # кусочек слева, как в виджете
    cv2.rectangle(bg, (10, y0), (10 + piece, y0 + piece), (250, 250, 250), 2)

    piece_img = np.dstack([patch, np.full((piece, piece), 255, dtype=np.uint8)])
    ok, bg_png = cv2.imencode(".png", bg)
    ok2, piece_png = cv2.imencode(".png", piece_img)
    assert ok and ok2
    return bg_png.tobytes(), piece_png.tobytes(), gap_x, piece


def test_template_gap_detection():
    bg_png, piece_png, gap_x, piece = synth_captcha()
    result = find_gap(bg_png, piece_png, method="template")
    assert result is not None
    assert abs(result.x - gap_x) <= 6, f"ожидали ~{gap_x}, получили {result.x}"
    assert result.bg_width == 340
    assert result.confidence > 0.2


def test_auto_method_finds_gap():
    bg_png, piece_png, gap_x, _ = synth_captcha(gap_x=250)
    result = find_gap(bg_png, piece_png, method="auto")
    assert result is not None
    assert abs(result.x - gap_x) <= 12


def test_edge_only_detection_in_right_region():
    bg_png, _, gap_x, _ = synth_captcha(gap_x=230)
    result = find_gap(bg_png, None, method="edge")
    assert result is not None
    assert result.x > 60          # не путает с исходным положением кусочка слева
    assert result.confidence >= 0.0


def test_css_scaling():
    bg_png, piece_png, gap_x, piece = synth_captcha()
    result = find_gap(bg_png, piece_png, method="template")
    # виджет отрисован в 170 CSS-px при картинке 340px → смещение вдвое меньше
    css = result.to_css(170)
    assert abs(css - gap_x / 2) <= 4


def test_garbage_input_is_safe():
    assert find_gap(b"not an image", None) is None
    assert find_gap(b"", b"") is None


def test_human_track_shape():
    points = human_track(100.0, 200.0, 180.0, duration=1.0)
    assert len(points) > 30
    assert abs(points[-1].x - 280.0) < 0.5           # пришли точно в цель
    assert abs(points[-1].y - 200.0) < 0.5
    # есть перелёт
    assert max(p.x for p in points) > 281.0
    # движение неравномерное: первый шаг меньше последнего «разгона»
    deltas = [points[i].x - points[i - 1].x for i in range(1, len(points))]
    assert max(deltas) > 3 * min(abs(d) for d in deltas if d > 0)
    # дрожание по Y присутствует
    assert any(abs(p.y - 200.0) > 0.1 for p in points[:-1])
    assert sum(p.delay for p in points) > 0.5


def test_track_is_not_linear():
    points = human_track(0.0, 0.0, 200.0, duration=1.2)
    xs = [p.x for p in points]
    linear = [200 * (i + 1) / len(xs) for i in range(len(xs))]
    deviation = sum(abs(a - b) for a, b in zip(xs, linear)) / len(xs)
    assert deviation > 5, "траектория слишком похожа на равномерное движение бота"