"""
Чтение полосок HP/MP/CP по цвету пикселей.

Идея: вдоль горизонтальной линии полоски берём ряд пикселей и считаем долю
тех, что близки к "цвету заполнения". Доля * 100 = процент полоски.
Это надёжнее чем один пиксель и терпимо к антиалиасингу/градиенту.
"""
import time

import numpy as np
import cv2

import config
from logic import settings
from vision import ocr


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


# ---------------------------------------------------------------------------
# Детекция ДВИЖЕНИЯ персонажа (для умного сбора лута): когда персонаж бежит к
# предмету, мир в зоне поиска прокручивается — большая разница между кадрами.
# Берём уменьшенную ч/б «сигнатуру» зоны поиска и сравниваем средним модулем
# разницы. Стоит на месте — разница мелкая (только анимации), бежит — крупная.
# ---------------------------------------------------------------------------
def world_signature(frame, size=28):
    """Уменьшенная (size×size) ч/б сигнатура зоны поиска (или центра кадра)."""
    spec = settings.get("search_region")
    h, w = frame.shape[:2]
    if spec:
        rgn = config.CAPTURE_REGION or {"left": 0, "top": 0}
        x = spec["left"] - rgn["left"]; y = spec["top"] - rgn["top"]
        x0 = max(0, x); y0 = max(0, y)
        x1 = min(w, x + spec["width"]); y1 = min(h, y + spec["height"])
        if x1 <= x0 or y1 <= y0:
            return None
        crop = frame[y0:y1, x0:x1]
    else:
        crop = frame[h // 4:3 * h // 4, w // 4:3 * w // 4]   # центр как запас
    if crop.size == 0:
        return None
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA)


def signature_diff(a, b):
    """Средний модуль разницы двух сигнатур (0..255). Больше = сильнее движение."""
    if a is None or b is None:
        return 0.0
    return float(np.mean(np.abs(a.astype(np.int16) - b.astype(np.int16))))


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


# ---------------------------------------------------------------------------
# Режим «цифры»: числа вшиты в сам бар (формат «тек/макс», напр. 2186/2186),
# поэтому OCR читает ИЗ ТОЙ ЖЕ области, что калибруется под бар (bar_<name>).
# Процент = тек/макс × 100. Максимум берётся авто из «тек/макс»; если на баре
# видно только текущее — можно задать ручной Max.
# Конфиг в settings: bar_<name>_digits = {"enabled": bool, "max": N|0}.
# OCR медленный, поэтому троттлим и кэшируем результат на DIGIT_OCR_INTERVAL.
# ---------------------------------------------------------------------------
_digit_cache = {}       # name -> (mono_time, percent|None)
_last_ocr_mono = 0.0    # момент последнего OCR любого бара (глобальный стаггер)


def _digit_percent(frame, name, region, max_manual):
    """
    Процент (0..100) полоски по числам из OCR (из области бара `region`),
    с троттлингом и кэшем. None — если распознать/посчитать не удалось (тогда
    вызывающий откатится на пиксельный режим).

    Два ограничителя нагрузки (OCR = запуск процесса Tesseract, дорого):
      * на каждый бар — не чаще DIGIT_OCR_INTERVAL (кэш между чтениями);
      * глобально — не больше ОДНОГО распознавания за DIGIT_OCR_MIN_GAP, чтобы
        включённые сразу несколько баров не читались в одном тике и не топили
        цикл. Бары естественно распознаются по очереди (round-robin).
    """
    global _last_ocr_mono
    now = time.monotonic()
    prev = _digit_cache.get(name)
    # Бар ЦЕЛИ — приоритетный: свой короткий интервал и БЕЗ общей очереди
    # (не ждём DIGIT_OCR_MIN_GAP и не занимаем слот), чтобы HP цели обновлялось
    # часто и порог скилла ловился точнее. Остальные бары — по очереди, как раньше.
    is_target = (name == "target")
    interval = (config.DIGIT_OCR_INTERVAL_TARGET if is_target
                else config.DIGIT_OCR_INTERVAL)
    if prev and now - prev[0] < interval:
        return prev[1]
    if not is_target:
        if now - _last_ocr_mono < config.DIGIT_OCR_MIN_GAP:
            return prev[1] if prev else None  # в этот тик уже читался другой бар
        _last_ocr_mono = now
    percent = None
    try:
        parsed = ocr.read_number(frame, region)
    except Exception:
        parsed = None                        # сбой OCR не должен ронять цикл бота
    if parsed is not None:
        cur, mx = parsed
        max_val = mx or max_manual or 0          # авто «тек/макс», иначе ручной Max
        if cur is not None and max_val:
            percent = round(max(0.0, min(100.0, cur / max_val * 100.0)), 1)
    value = percent if percent is not None else (prev[1] if prev else None)
    _digit_cache[name] = (now, value)
    return value


def _digit_on(name):
    """Конфиг режима цифр, если он включён для бара (иначе None)."""
    d = settings.get("bar_%s_digits" % name)
    return d if (d and d.get("enabled")) else None


def read_self_bars(frame):
    """
    HP/MP/CP персонажа в процентах. Приоритет: режим цифр (OCR) -> калиброванная
    рамка (пиксели) -> старый config. Если цифры не прочитались — откат на пиксели.
    """
    result = {}
    for name in ("hp", "mp", "cp"):
        spec = _bar_spec(name)
        don = _digit_on(name)
        if don and spec:                 # числа читаем из области самого бара
            val = _digit_percent(frame, name, spec, don.get("max"))
            if val is not None:
                result[name] = val
                continue                 # цифры прочитались — берём их
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
    # HP цели: если включён режим цифр и число прочиталось — берём его (из
    # области бара цели); иначе по правому краю заливки.
    don = _digit_on("target")
    if don and spec:
        val = _digit_percent(frame, "target", spec, don.get("max"))
        if val is not None:
            return True, val
    return True, round(_fill_edge(col_density) * 100.0, 1)
