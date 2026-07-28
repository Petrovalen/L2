"""
Чтение полосок HP/MP/CP по цвету пикселей.

Идея: вдоль горизонтальной линии полоски берём ряд пикселей и считаем долю
тех, что близки к "цвету заполнения". Доля * 100 = процент полоски.
Это надёжнее чем один пиксель и терпимо к антиалиасингу/градиенту.
"""
import numpy as np
import cv2

import config
from logic import settings


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
    """Вернуть процент (0..100) для одной полоски по её конфигу (старый формат)."""
    ratio = _fill_ratio(
        frame,
        bar_cfg["x1"], bar_cfg["x2"], bar_cfg["y"],
        bar_cfg["color"], bar_cfg["tol"],
    )
    return round(ratio * 100.0, 1)


# ---------------------------------------------------------------------------
# Калиброванные рамкой полоски (прямоугольная область + цвет), из settings.json.
# Формат spec: {"left","top","width","height","color":[b,g,r],"tol"}.
# Если полоска откалибрована в панели — используем её, иначе старый config.
# ---------------------------------------------------------------------------
def _region_density(frame, spec):
    """Доля совпавших с цветом пикселей по высоте для каждого столбца области."""
    rgn = config.CAPTURE_REGION or {"left": 0, "top": 0}
    x = spec["left"] - rgn["left"]
    y = spec["top"] - rgn["top"]
    h, w = frame.shape[:2]
    x0 = max(0, x); y0 = max(0, y)
    x1 = min(w, x + spec["width"]); y1 = min(h, y + spec["height"])
    if x1 <= x0 or y1 <= y0:
        return np.empty(0)
    reg = frame[y0:y1, x0:x1, :].astype(np.int16)
    color = np.array(spec["color"], dtype=np.int16)
    tol = spec.get("tol", 60)
    matched = np.all(np.abs(reg - color) <= tol, axis=2)
    return matched.mean(axis=0)


def detect_fill_color(frame, rect):
    """
    Определить цвет заливки полоски по обведённой области (полоска должна быть
    ПОЛНОЙ). Берём медиану насыщенных ярких пикселей -> цвет заполнения (BGR).
    """
    rgn = config.CAPTURE_REGION or {"left": 0, "top": 0}
    x = rect["left"] - rgn["left"]; y = rect["top"] - rgn["top"]
    h, w = frame.shape[:2]
    x0 = max(0, x); y0 = max(0, y)
    x1 = min(w, x + rect["width"]); y1 = min(h, y + rect["height"])
    crop = frame[y0:y1, x0:x1]
    if crop.size == 0:
        return [0, 0, 0]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = (hsv[:, :, 1] >= 60) & (hsv[:, :, 2] >= 60)   # насыщенные и яркие
    px = crop[mask] if np.count_nonzero(mask) >= 10 else crop.reshape(-1, 3)
    med = np.median(px.reshape(-1, 3), axis=0)
    return [int(med[0]), int(med[1]), int(med[2])]


def _bar_spec(name):
    """Взять калиброванную рамку полоски из settings (или None)."""
    return settings.get("bar_" + name)


def read_self_bars(frame):
    """HP/MP/CP персонажа в процентах. Калиброванные рамки в приоритете."""
    result = {}
    for name in ("hp", "mp", "cp"):
        spec = _bar_spec(name)
        if spec:
            result[name] = round(_fill_edge(_region_density(frame, spec)) * 100.0, 1)
        else:
            cfg = config.BARS.get(name)
            result[name] = read_bar(frame, cfg) if cfg else 0.0
    return result


def has_target(frame):
    """
    Есть ли выбранная живая цель. Возвращает (present, target_hp_percent).

    Наличие — по красному в ЛЕВОЙ части полоски цели (реальная полоска начинается
    слева; случайный цвет справа полоску не образует).
    """
    spec = _bar_spec("target")
    if spec:
        col_density = _region_density(frame, spec)
    else:
        cfg = config.TARGET_BAR
        col_density = _column_density(frame, cfg["x1"], cfg["x2"], cfg["y"],
                                      cfg["color"], cfg["tol"])
    if col_density.size == 0:
        return False, 0.0
    # длиннейший непрерывный участок красного (реальная полоска — сплошной
    # отрезок; иконки слева и разрозненный красный окружения его не образуют).
    dense = col_density >= _COL_DENSE_MIN
    longest = cur = 0
    for v in dense:
        if v:
            cur += 1
            if cur > longest:
                longest = cur
        else:
            cur = 0
    if longest / dense.size < config.TARGET_PRESENT_MIN:
        return False, 0.0
    # HP цели — по правому краю заливки (иконки слева на это не влияют).
    return True, round(_fill_edge(col_density) * 100.0, 1)
