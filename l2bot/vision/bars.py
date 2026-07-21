"""
Чтение полосок HP/MP/CP по цвету пикселей.

Идея: вдоль горизонтальной линии полоски берём ряд пикселей и считаем долю
тех, что близки к "цвету заполнения". Доля * 100 = процент полоски.
Это надёжнее чем один пиксель и терпимо к антиалиасингу/градиенту.
"""
import numpy as np

import config


def _to_frame_coords(x, y):
    """Перевести абсолютные экранные координаты в координаты внутри кадра."""
    region = config.CAPTURE_REGION or {"left": 0, "top": 0}
    return x - region["left"], y - region["top"]


def _fill_ratio(frame, x1, x2, y, color, tol):
    """Доля залитых пикселей (0..1) вдоль линии y от x1 до x2."""
    fx1, fy = _to_frame_coords(x1, y)
    fx2, _ = _to_frame_coords(x2, y)
    h, w = frame.shape[:2]
    if not (0 <= fy < h) or fx1 >= fx2:
        return 0.0
    fx1 = max(0, fx1)
    fx2 = min(w, fx2)
    strip = frame[fy, fx1:fx2, :].astype(np.int16)  # (N,3) BGR
    target = np.array(color, dtype=np.int16)
    diff = np.abs(strip - target)
    matched = np.all(diff <= tol, axis=1)
    if matched.size == 0:
        return 0.0
    return float(np.count_nonzero(matched)) / matched.size


def read_bar(frame, bar_cfg):
    """Вернуть процент (0..100) для одной полоски по её конфигу."""
    ratio = _fill_ratio(
        frame,
        bar_cfg["x1"], bar_cfg["x2"], bar_cfg["y"],
        bar_cfg["color"], bar_cfg["tol"],
    )
    return round(ratio * 100.0, 1)


def read_self_bars(frame):
    """HP/MP/CP персонажа в процентах. Возвращает dict."""
    result = {}
    for name, cfg in config.BARS.items():
        result[name] = read_bar(frame, cfg)
    return result


def has_target(frame):
    """Есть ли выбранная живая цель (по полоске HP цели вверху экрана)."""
    ratio = _fill_ratio(
        frame,
        config.TARGET_BAR["x1"], config.TARGET_BAR["x2"], config.TARGET_BAR["y"],
        config.TARGET_BAR["color"], config.TARGET_BAR["tol"],
    )
    return ratio >= config.TARGET_PRESENT_MIN, round(ratio * 100.0, 1)
