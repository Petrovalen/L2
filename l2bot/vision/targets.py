"""
Поиск мобов на экране через template matching (OpenCV).

Шаблон — маленькая картинка моба или его нейма (имени над головой).
Ищем все совпадения выше порога, возвращаем центры в координатах ЭКРАНА
(с поправкой на регион захвата), отсортированные "по близости к центру экрана"
как прокси к "ближайший к персонажу".
"""
import os
import difflib

import cv2
import numpy as np

import config
from vision import ocr

# Ядро для склейки букв ника в единый блок (горизонтальное «размазывание»).
_NAME_DILATE = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 3))


def _match_name(text, wanted_lower):
    """Совпадает ли распознанный text с одним из имён белого списка."""
    t = text.lower().strip()
    if not t:
        return None
    for orig, low in wanted_lower:
        if t == low or low in t or t in low:
            return orig
        if difflib.SequenceMatcher(None, t, low).ratio() >= config.NAME_MATCH_RATIO:
            return orig
    return None


def name_in_list(text, names):
    """True, если распознанное имя text похоже на одно из имён белого списка."""
    return _match_name(text, [(n, n.lower()) for n in names]) is not None


def scan_nameplates(frame, region, names=None):
    """
    Найти ВСЕ таблички имён в области `region`, распознать и (если задан
    белый список names) отметить совпавшие. Для детекции и отладочной отрисовки.

    Возвращает список, отсортированный по близости к центру экрана:
        [{"box": (left, top, w, h),   # прямоугольник ника в координатах экрана
          "text": распознанный_текст,
          "name": имя_из_списка_или_None,   # None = не из белого списка
          "x", "y": точка клика (центр по X, чуть ниже — тело моба)}, ...]
    """
    if not region:
        return []
    rgn = config.CAPTURE_REGION or {"left": 0, "top": 0}
    ox = region["left"] - rgn["left"]
    oy = region["top"] - rgn["top"]
    h, w = frame.shape[:2]
    x0 = max(0, ox); y0 = max(0, oy)
    x1 = min(w, ox + region["width"]); y1 = min(h, oy + region["height"])
    if x1 <= x0 or y1 <= y0:
        return []

    sub = frame[y0:y1, x0:x1]
    gray = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, config.NAME_BRIGHT, 255, cv2.THRESH_BINARY)
    dil = cv2.dilate(mask, _NAME_DILATE, iterations=1)
    num, _lbl, stats, _cent = cv2.connectedComponentsWithStats(dil, connectivity=8)

    fh, fw = mask.shape
    scr_cx, scr_cy = w / 2.0, h / 2.0     # центр экрана ≈ позиция персонажа
    wanted_lower = [(n, n.lower()) for n in (names or [])]
    out = []
    for i in range(1, num):
        bx, by, bw, bh, _area = stats[i]
        # форма таблички: горизонтальная, разумного размера
        if bh < 6 or bh > 40 or bw < 12 or bw > 500 or bw < bh:
            continue
        pad = 3
        scr = {
            "left": region["left"] + max(0, bx - pad),
            "top": region["top"] + max(0, by - pad),
            "width": min(fw, bx + bw + pad) - max(0, bx - pad),
            "height": min(fh, by + bh + pad) - max(0, by - pad),
        }
        text = ocr.read_name(frame, scr, trim=False)
        matched = _match_name(text, wanted_lower) if wanted_lower else None
        left = region["left"] + bx
        top = region["top"] + by
        cx = left + bw // 2
        cy = top + bh + config.NAME_CLICK_DY
        out.append({
            "box": (left, top, int(bw), int(bh)),
            "text": text, "name": matched,
            "x": cx, "y": cy,
            "_d": (cx - scr_cx) ** 2 + (cy - scr_cy) ** 2,
        })
    out.sort(key=lambda d: d["_d"])
    return out


def find_named_mobs(frame, names, region):
    """
    Ники мобов из белого списка на экране, ближайшие к центру — первыми:
        [{"x": screen_x, "y": screen_y, "name": имя}, ...]
    """
    if not names or not region:
        return []
    return [{"x": d["x"], "y": d["y"], "name": d["name"]}
            for d in scan_nameplates(frame, region, names) if d["name"]]


def boxes_in_crop(crop, off_left, off_top):
    """
    Найти боксы ников в УЖЕ вырезанной области `crop` (BGR). Быстро, без OCR.
    off_left/off_top — экранные координаты левого-верхнего угла кропа.
    Возврат: [(left, top, w, h), ...] в координатах экрана.
    """
    if crop is None or crop.size == 0:
        return []
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, config.NAME_BRIGHT, 255, cv2.THRESH_BINARY)
    dil = cv2.dilate(mask, _NAME_DILATE, iterations=1)
    num, _lbl, stats, _cent = cv2.connectedComponentsWithStats(dil, connectivity=8)
    boxes = []
    for i in range(1, num):
        bx, by, bw, bh, _area = stats[i]
        if bh < 6 or bh > 40 or bw < 12 or bw > 500 or bw < bh:
            continue
        boxes.append((off_left + int(bx), off_top + int(by), int(bw), int(bh)))
    return boxes


def scan_nameplate_boxes(frame, region):
    """Боксы ников (без OCR) на полном кадре, координаты экрана."""
    if not region:
        return []
    rgn = config.CAPTURE_REGION or {"left": 0, "top": 0}
    ox = region["left"] - rgn["left"]
    oy = region["top"] - rgn["top"]
    h, w = frame.shape[:2]
    x0 = max(0, ox); y0 = max(0, oy)
    x1 = min(w, ox + region["width"]); y1 = min(h, oy + region["height"])
    if x1 <= x0 or y1 <= y0:
        return []
    return boxes_in_crop(frame[y0:y1, x0:x1], region["left"], region["top"])

# Кэш загруженных шаблонов: путь -> grayscale-массив.
_template_cache = {}


def _load_template(path):
    if path in _template_cache:
        return _template_cache[path]
    if not os.path.exists(path):
        _template_cache[path] = None
        return None
    tpl = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    _template_cache[path] = tpl
    return tpl


def _frame_to_screen(x, y):
    region = config.CAPTURE_REGION or {"left": 0, "top": 0}
    return x + region["left"], y + region["top"]


def find_mobs(frame, templates=None, threshold=None):
    """
    Вернуть список кандидатов-мобов:
        [{"x": screen_x, "y": screen_y, "score": float}, ...]
    Отсортирован по близости к центру экрана.
    """
    templates = templates if templates is not None else config.MOB_TEMPLATES
    threshold = threshold if threshold is not None else config.TEMPLATE_MATCH_THRESHOLD

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    cx, cy = w / 2.0, h / 2.0
    hits = []

    for path in templates:
        tpl = _load_template(path)
        if tpl is None:
            continue
        th, tw = tpl.shape[:2]
        if th > h or tw > w:
            continue
        res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(res >= threshold)
        for (mx, my) in zip(xs, ys):
            center_x = mx + tw / 2.0
            center_y = my + th / 2.0
            sx, sy = _frame_to_screen(int(center_x), int(center_y))
            hits.append({
                "x": sx,
                "y": sy,
                "score": float(res[my, mx]),
                "_dist": (center_x - cx) ** 2 + (center_y - cy) ** 2,
            })

    hits = _dedupe(hits)
    hits.sort(key=lambda h: h["_dist"])
    for h in hits:
        h.pop("_dist", None)
    return hits


def _dedupe(hits, min_dist=25):
    """Схлопнуть близкие совпадения (один моб = один хит)."""
    kept = []
    for h in sorted(hits, key=lambda x: -x["score"]):
        if all((h["x"] - k["x"]) ** 2 + (h["y"] - k["y"]) ** 2 > min_dist ** 2
               for k in kept):
            kept.append(h)
    return kept
