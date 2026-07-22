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


# Полуширина вертикальной полосы сэмплирования (строк вверх/вниз от y).
_SAMPLE_BAND = 7
# Столбец считаем «плотно красным» (реальная заливка, а не текст/шум),
# если цвет совпал хотя бы в этой доле его пикселей по высоте.
_COL_DENSE_MIN = 0.5


def _fill_ratio(frame, x1, x2, y, color, tol):
    """
    Уровень заполнения полоски (0..1) на отрезке [x1,x2] вокруг линии y.

    Полоски L2 заполняются слева направо и опустошаются справа. Поэтому
    уровень = как далеко вправо доходит «плотный красный». Считаем по полосе
    высотой 2*_SAMPLE_BAND+1: для каждого столбца — доля совпавших с цветом
    пикселей; «плотный» столбец = доля >= _COL_DENSE_MIN. Результат — позиция
    самого правого плотного столбца, делённая на ширину.

    Такой способ устойчив к тексту поверх полоски (числа HP/MP, подписи):
    цифры по центру создают «дыры», но не сдвигают правую границу заливки.
    """
    fx1, fy = _to_frame_coords(x1, y)
    fx2, _ = _to_frame_coords(x2, y)
    h, w = frame.shape[:2]
    fx1 = max(0, fx1)
    fx2 = min(w, fx2)
    y0 = max(0, fy - _SAMPLE_BAND)
    y1 = min(h, fy + _SAMPLE_BAND + 1)
    if fx1 >= fx2 or y0 >= y1:
        return 0.0
    region = frame[y0:y1, fx1:fx2, :].astype(np.int16)        # (rows, cols, 3) BGR
    target = np.array(color, dtype=np.int16)
    matched = np.all(np.abs(region - target) <= tol, axis=2)  # (rows, cols) bool
    col_density = matched.mean(axis=0)                        # доля по высоте на столбец
    dense = np.where(col_density >= _COL_DENSE_MIN)[0]
    if dense.size == 0:
        return 0.0
    return float(dense.max() + 1) / col_density.size


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
