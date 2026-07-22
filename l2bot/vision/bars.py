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
# Доля ширины слева, по которой проверяем НАЛИЧИЕ полоски цели.
_TARGET_LEFT_FRAC = 0.12


def _column_density(frame, x1, x2, y, color, tol):
    """
    Для отрезка [x1,x2] вокруг линии y вернуть массив долей совпавших с цветом
    пикселей по высоте для каждого столбца (полоса высотой 2*_SAMPLE_BAND+1).
    Пустой массив — если область вне кадра.
    """
    fx1, fy = _to_frame_coords(x1, y)
    fx2, _ = _to_frame_coords(x2, y)
    h, w = frame.shape[:2]
    fx1 = max(0, fx1)
    fx2 = min(w, fx2)
    y0 = max(0, fy - _SAMPLE_BAND)
    y1 = min(h, fy + _SAMPLE_BAND + 1)
    if fx1 >= fx2 or y0 >= y1:
        return np.empty(0)
    region = frame[y0:y1, fx1:fx2, :].astype(np.int16)        # (rows, cols, 3) BGR
    target = np.array(color, dtype=np.int16)
    matched = np.all(np.abs(region - target) <= tol, axis=2)  # (rows, cols) bool
    return matched.mean(axis=0)                               # доля по высоте на столбец


def _fill_edge(col_density):
    """Уровень заполнения (0..1) = позиция самого правого «плотного» столбца."""
    if col_density.size == 0:
        return 0.0
    dense = np.where(col_density >= _COL_DENSE_MIN)[0]
    if dense.size == 0:
        return 0.0
    return float(dense.max() + 1) / col_density.size


def _fill_ratio(frame, x1, x2, y, color, tol):
    """
    Уровень заполнения полоски (0..1) на отрезке [x1,x2] вокруг линии y.

    Полоски L2 заполняются слева направо и опустошаются справа. Поэтому
    уровень = как далеко вправо доходит «плотный красный» (правый край).
    Устойчиво к тексту поверх полоски (числа HP/MP по центру создают «дыры»,
    но не сдвигают правую границу заливки).
    """
    return _fill_edge(_column_density(frame, x1, x2, y, color, tol))


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
    """
    Есть ли выбранная живая цель. Возвращает (present, target_hp_percent).

    Наличие определяем по красному в ЛЕВОЙ части полоски цели: настоящая
    полоска начинается от левого края (x1), а случайный красный в окружении
    (справа в области) полоску не образует. Так уходит ложное срабатывание,
    когда позади области полоски цели попадаются красные детали сцены.
    """
    cfg = config.TARGET_BAR
    col_density = _column_density(frame, cfg["x1"], cfg["x2"], cfg["y"],
                                  cfg["color"], cfg["tol"])
    if col_density.size == 0:
        return False, 0.0
    left_k = max(1, int(col_density.size * _TARGET_LEFT_FRAC))
    left_red = float((col_density[:left_k] >= _COL_DENSE_MIN).mean())
    if left_red < config.TARGET_PRESENT_MIN:
        return False, 0.0
    return True, round(_fill_edge(col_density) * 100.0, 1)
