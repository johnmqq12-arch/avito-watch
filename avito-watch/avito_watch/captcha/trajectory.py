"""Человеческая траектория мыши для слайдер-капчи.

Ровномерное линейное движение за 3 шага — самый obvious признак бота.
Здесь: разгон, торможение, лёгкий перелёт с возвратом, дрожание по Y,
микропаузы и неравномерные интервалы.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(slots=True)
class Point:
    x: float
    y: float
    delay: float  # пауза ПЕРЕД этим шагом, сек


def human_track(start_x: float, start_y: float, distance: float, *,
                duration: float | None = None, overshoot: bool = True) -> list[Point]:
    duration = duration if duration is not None else random.uniform(0.75, 1.65)
    steps = random.randint(38, 72)
    over = random.uniform(4.0, 11.0) if overshoot and distance > 40 else 0.0
    target = distance + over
    points: list[Point] = []

    # профиль скорости: быстрый старт, длинное торможение (ease-out quint)
    def progress(t: float) -> float:
        return 1 - math.pow(1 - t, random.uniform(3.2, 4.4))

    pause_at = {random.randint(int(steps * 0.3), int(steps * 0.8)) for _ in range(random.randint(1, 3))}
    prev = 0.0
    for i in range(1, steps + 1):
        t = i / steps
        cur = target * progress(t)
        # дрожание руки
        wobble = random.gauss(0, 0.35)
        x = start_x + cur + wobble
        y = start_y + random.gauss(0, 0.9) * math.sin(math.pi * t)
        dt = duration / steps * random.uniform(0.55, 1.55)
        if i in pause_at:
            dt += random.uniform(0.04, 0.13)
        points.append(Point(round(x, 1), round(y, 1), dt))
        prev = cur

    # возврат с перелёта — медленно, как будто целимся
    if over > 0:
        back_steps = random.randint(4, 9)
        for i in range(1, back_steps + 1):
            frac = i / back_steps
            x = start_x + target - over * frac
            points.append(Point(round(x, 1), round(start_y + random.gauss(0, 0.4), 1),
                                random.uniform(0.03, 0.09)))
    points.append(Point(round(start_x + distance, 1), round(start_y, 1), random.uniform(0.06, 0.18)))
    return points


def press_delays() -> tuple[float, float]:
    """Пауза после нажатия кнопки мыши и перед отпусканием."""
    return random.uniform(0.06, 0.18), random.uniform(0.09, 0.28)