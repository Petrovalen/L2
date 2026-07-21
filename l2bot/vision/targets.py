"""
Поиск мобов на экране через template matching (OpenCV).

Шаблон — маленькая картинка моба или его нейма (имени над головой).
Ищем все совпадения выше порога, возвращаем центры в координатах ЭКРАНА
(с поправкой на регион захвата), отсортированные "по близости к центру экрана"
как прокси к "ближайший к персонажу".
"""
import os

import cv2
import numpy as np

import config

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
