"""
Поиск мобов на экране через template matching (OpenCV).

Шаблон — маленькая картинка моба или его нейма (имени над головой).
Ищем все совпадения выше порога, возвращаем центры в координатах ЭКРАНА
(с поправкой на регион захвата), отсортированные по близости к ТОЧКЕ ПЕРСОНАЖА
(калиброванная зона character_anchor; если не задана — центр экрана).
"""
import os
import re
import difflib

import cv2
import numpy as np

import config
from vision import ocr
from logic import settings

# Папка со снимками-шаблонами ников мобов (аналог target_templates в LA2Pixel).
_TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "target_templates")


def _anchor_screen(frame):
    """
    Опорная точка «где персонаж» в координатах ЭКРАНА — от неё считаем, какой
    моб ближе. Это центр калиброванной зоны character_anchor (аналог зоны
    «Character» в LA2Pixel). Если не задана — центр экрана как запасной вариант.
    """
    a = settings.get("character_anchor")
    if a:
        return a["left"] + a["width"] / 2.0, a["top"] + a["height"] / 2.0
    rgn = config.CAPTURE_REGION or {"left": 0, "top": 0}
    h, w = frame.shape[:2]
    return rgn["left"] + w / 2.0, rgn["top"] + h / 2.0

# Ядро для склейки букв ника в единый блок (горизонтальное «размазывание»).
# Умеренная ширина: соединяет соседние буквы, но НЕ мостит далеко разнесённые
# яркие пятна травы в псевдо-таблички (широкое ядро давало «сетку» на траве).
_NAME_DILATE = cv2.getStructuringElement(cv2.MORPH_RECT, (16, 3))

# Форма таблички ника: горизонтальный текст разумного размера.
_NAME_MIN_W = 26      # ник — из нескольких букв; узкие пятна травы отсекаем
_NAME_MAX_W = 600
_NAME_MIN_H = 8
_NAME_MAX_H = 28
_NAME_MIN_ASPECT = 1.8   # ширина/высота: текст явно вытянут по горизонтали

# Плотность заливки бокса (доля ярких пикселей сырой маски):
#   выше MAX — сплошной яркий блоб (огонь/полоска/засветка), не текст;
#   ниже MIN — почти пусто (одиночные пятна шума), не текст.
_NAME_MAX_FILL = 0.62
_NAME_MIN_FILL = 0.06


def _nameplate_mask(bgr):
    """
    Бинарная маска яркого БЕЛЁСОГО текста ника из BGR-кропа.

    Ник — почти белый: высокая яркость (V) И низкая насыщенность (S). Огонь,
    частицы, скобки выделения цели, HP-бары над головой — яркие, но ЦВЕТНЫЕ
    (высокая S), поэтому в маску не попадают, даже пройдя порог яркости.

    Возвращает (mask, dil): сырая маска и она же после горизонтального дилейта.
    """
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    s = hsv[:, :, 1]
    v = hsv[:, :, 2]
    mask = ((v >= config.NAME_BRIGHT) & (s <= config.NAME_MAX_SAT)).astype(np.uint8) * 255
    dil = cv2.dilate(mask, _NAME_DILATE, iterations=1)
    return mask, dil


def _find_plate_boxes(bgr):
    """
    Кандидаты-таблички ников в BGR-кропе: [(bx, by, bw, bh), ...] в координатах
    кропа. Фильтр по форме таблички + отсев сплошных ярких блобов по плотности.
    """
    mask, dil = _nameplate_mask(bgr)
    num, _lbl, stats, _cent = cv2.connectedComponentsWithStats(dil, connectivity=8)
    out = []
    for i in range(1, num):
        bx, by, bw, bh, _area = stats[i]
        # форма таблички: горизонтальный текст разумного размера
        if not (_NAME_MIN_H <= bh <= _NAME_MAX_H):
            continue
        if not (_NAME_MIN_W <= bw <= _NAME_MAX_W):
            continue
        if bw < _NAME_MIN_ASPECT * bh:      # слишком «квадратные» пятна — не текст
            continue
        # плотность сырой (недилейтнутой) маски: сплошной блоб (засветка) или
        # почти пусто (шум) — не текст.
        sub = mask[by:by + bh, bx:bx + bw]
        fill = sub.mean() / 255.0 if sub.size else 0.0
        if fill > _NAME_MAX_FILL or fill < _NAME_MIN_FILL:
            continue
        out.append((int(bx), int(by), int(bw), int(bh)))
    return out


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

    Возвращает список, отсортированный по близости к ТОЧКЕ ПЕРСОНАЖА (ближайший
    моб — первым):
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
    fh, fw = sub.shape[:2]
    ax, ay = _anchor_screen(frame)        # точка персонажа (экранные координаты)
    wanted_lower = [(n, n.lower()) for n in (names or [])]

    # Кандидаты-боксы + расстояние до персонажа. OCR дорог (запуск Tesseract),
    # поэтому распознаём НЕ все, а только ближайшие VISION_MAX_OCR боксов — нам и
    # нужен ближайший моб, а не перепись всего экрана. Это держит скан быстрым
    # даже при шуме (ложные боксы на траве дальше почти всегда отсекаются).
    cand = []
    for (bx, by, bw, bh) in _find_plate_boxes(sub):
        left = region["left"] + bx
        top = region["top"] + by
        cx = left + bw // 2 + config.NAME_CLICK_DX
        cy = top + bh + config.NAME_CLICK_DY
        d2 = (cx - ax) ** 2 + (cy - ay) ** 2
        cand.append((d2, bx, by, bw, bh, left, top, cx, cy))
    cand.sort(key=lambda c: c[0])

    out = []
    for d2, bx, by, bw, bh, left, top, cx, cy in cand[:config.VISION_MAX_OCR]:
        pad = 3
        scr = {
            "left": region["left"] + max(0, bx - pad),
            "top": region["top"] + max(0, by - pad),
            "width": min(fw, bx + bw + pad) - max(0, bx - pad),
            "height": min(fh, by + bh + pad) - max(0, by - pad),
        }
        text = ocr.read_name(frame, scr, trim=False)
        matched = _match_name(text, wanted_lower) if wanted_lower else None
        out.append({
            "box": (left, top, int(bw), int(bh)),
            "text": text, "name": matched,
            "x": cx, "y": cy, "_d": d2,
        })
    return out   # уже по возрастанию расстояния до персонажа


def find_named_mobs(frame, names, region):
    """
    Ники мобов из белого списка на экране, ближайшие к ТОЧКЕ ПЕРСОНАЖА — первыми:
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
    return [(off_left + bx, off_top + by, bw, bh)
            for (bx, by, bw, bh) in _find_plate_boxes(crop)]


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
# Кэш УМЕНЬШЕННЫХ шаблонов для быстрого матчинга: (путь, scale) -> массив.
_scaled_template_cache = {}


def _drop_scaled(path):
    """Сбросить кэш уменьшенных копий для шаблона (при его пересохранении/удалении)."""
    for k in [k for k in _scaled_template_cache if k[0] == path]:
        _scaled_template_cache.pop(k, None)


def _scaled_template(path, tpl, scale):
    """Уменьшенная копия шаблона (кэшируется). scale=1.0 -> сам шаблон."""
    if scale >= 0.999:
        return tpl
    key = (path, round(scale, 3))
    cached = _scaled_template_cache.get(key)
    if cached is None:
        cached = cv2.resize(tpl, None, fx=scale, fy=scale,
                            interpolation=cv2.INTER_AREA)
        _scaled_template_cache[key] = cached
    return cached


def _imread_gray(path):
    """
    Прочитать изображение в grayscale, БЕЗОПАСНО для не-ASCII путей (кириллица).
    cv2.imread на Windows не умеет Unicode-пути -> читаем через np.fromfile.
    """
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except (OSError, ValueError):
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)


def _imwrite(path, img):
    """Сохранить изображение, безопасно для не-ASCII путей (cv2.imwrite не умеет)."""
    ext = os.path.splitext(path)[1] or ".png"
    try:
        ok, buf = cv2.imencode(ext, img)
        if not ok:
            return False
        buf.tofile(path)
        return True
    except (OSError, ValueError):
        return False


def _load_template(path):
    if path in _template_cache:
        return _template_cache[path]
    if not os.path.exists(path):
        _template_cache[path] = None
        return None
    tpl = _imread_gray(path)
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
    ax, ay = _anchor_screen(frame)          # точка персонажа (экранные координаты)
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
                "_dist": (sx - ax) ** 2 + (sy - ay) ** 2,
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


# ---------------------------------------------------------------------------
# Поиск мобов по ШАБЛОНАМ ников (подход LA2Pixel).
# Для каждого моба хранится снимок его ника — БИНАРНАЯ маска яркого текста
# (фон убран). Ищем этот рисунок template matching’ом внутри зоны поиска: трава
# не совпадает с конкретным ником, поэтому ложных срабатываний на ней нет.
# ---------------------------------------------------------------------------
def _sanitize(name):
    return re.sub(r"[^\w\-]+", "_", (name or "").strip()) or "mob"


def template_path(name):
    """Путь к файлу шаблона ника моба."""
    return os.path.join(_TEMPLATE_DIR, _sanitize(name) + ".png")


def has_template(name):
    return os.path.exists(template_path(name))


def save_name_template(frame, region, name):
    """
    Снять шаблон ника из обведённой области `region` (экранные координаты) и
    сохранить в target_templates/<имя>.png как бинарную маску текста. Возвращает
    True при успехе, False — если в рамке почти нет текста.
    """
    rgn = config.CAPTURE_REGION or {"left": 0, "top": 0}
    x = region["left"] - rgn["left"]; y = region["top"] - rgn["top"]
    h, w = frame.shape[:2]
    x0 = max(0, x); y0 = max(0, y)
    x1 = min(w, x + region["width"]); y1 = min(h, y + region["height"])
    if x1 <= x0 or y1 <= y0:
        return False
    mask, _ = _nameplate_mask(frame[y0:y1, x0:x1])
    ys, xs = np.where(mask > 0)
    if xs.size < 8:                       # текста почти нет — нечего сохранять
        return False
    pad = 2
    bx0 = max(0, int(xs.min()) - pad); by0 = max(0, int(ys.min()) - pad)
    bx1 = min(mask.shape[1], int(xs.max()) + 1 + pad)
    by1 = min(mask.shape[0], int(ys.max()) + 1 + pad)
    tpl = mask[by0:by1, bx0:bx1]
    os.makedirs(_TEMPLATE_DIR, exist_ok=True)
    path = template_path(name)
    ok = _imwrite(path, tpl)
    _template_cache.pop(path, None)       # сбросить кэш, чтобы перечитать свежий
    _drop_scaled(path)
    return bool(ok)


def delete_template(name):
    """Удалить файл шаблона ника (при удалении моба из списка)."""
    path = template_path(name)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
    _template_cache.pop(path, None)
    _drop_scaled(path)


# ---------------------------------------------------------------------------
# Иконки САМОБАФФОВ: определяем, висит ли бафф, по его иконке в панели баффов.
# Иконка — цветная картинка, поэтому матчим по grayscale (а не по маске текста).
# ---------------------------------------------------------------------------
def buff_template_path(label):
    return os.path.join(_TEMPLATE_DIR, "buff_" + _sanitize(label) + ".png")


def save_buff_template(frame, region, label):
    """Снять иконку баффа из обведённой области и сохранить (grayscale)."""
    rgn = config.CAPTURE_REGION or {"left": 0, "top": 0}
    x = region["left"] - rgn["left"]; y = region["top"] - rgn["top"]
    h, w = frame.shape[:2]
    x0 = max(0, x); y0 = max(0, y)
    x1 = min(w, x + region["width"]); y1 = min(h, y + region["height"])
    if x1 <= x0 or y1 <= y0:
        return False
    gray = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    os.makedirs(_TEMPLATE_DIR, exist_ok=True)
    path = buff_template_path(label)
    ok = _imwrite(path, gray)
    _template_cache.pop(path, None)
    return bool(ok)


def delete_buff_template(label):
    path = buff_template_path(label)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
    _template_cache.pop(path, None)


def buff_present(frame, region, label, threshold=None):
    """
    Висит ли бафф `label`: ищем его иконку (шаблон) в зоне панели баффов.
    True — иконка найдена (бафф активен). Если шаблона нет или зона кривая —
    возвращаем True (проверить нечем; НЕ спамим кастом).
    """
    tpl = _load_template(buff_template_path(label))
    if tpl is None or not region:
        return True
    threshold = config.BUFF_MATCH_THRESHOLD if threshold is None else threshold
    rgn = config.CAPTURE_REGION or {"left": 0, "top": 0}
    ox = region["left"] - rgn["left"]; oy = region["top"] - rgn["top"]
    h, w = frame.shape[:2]
    x0 = max(0, ox); y0 = max(0, oy)
    x1 = min(w, ox + region["width"]); y1 = min(h, oy + region["height"])
    if x1 <= x0 or y1 <= y0:
        return True
    crop = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    th, tw = tpl.shape[:2]
    if th > crop.shape[0] or tw > crop.shape[1]:
        return True
    res = cv2.matchTemplate(crop, tpl, cv2.TM_CCOEFF_NORMED)
    return float(res.max()) >= threshold


def locate_buff(frame, region, label):
    """
    Лучшее совпадение иконки баффа в зоне панели баффов.
    Возврат: (score, (left, top, w, h)) в координатах экрана, либо (0.0, None).
    Для отладочного оверлея: показать, где найдена иконка и её score.
    """
    tpl = _load_template(buff_template_path(label))
    if tpl is None or not region:
        return 0.0, None
    rgn = config.CAPTURE_REGION or {"left": 0, "top": 0}
    ox = region["left"] - rgn["left"]; oy = region["top"] - rgn["top"]
    h, w = frame.shape[:2]
    x0 = max(0, ox); y0 = max(0, oy)
    x1 = min(w, ox + region["width"]); y1 = min(h, oy + region["height"])
    if x1 <= x0 or y1 <= y0:
        return 0.0, None
    crop = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    th, tw = tpl.shape[:2]
    if th > crop.shape[0] or tw > crop.shape[1]:
        return 0.0, None
    res = cv2.matchTemplate(crop, tpl, cv2.TM_CCOEFF_NORMED)
    _minv, maxv, _minl, maxl = cv2.minMaxLoc(res)
    left = region["left"] + int(maxl[0])
    top = region["top"] + int(maxl[1])
    return float(maxv), (left, top, int(tw), int(th))


# ---------------------------------------------------------------------------
# Готовность скилла по иконке хотбара: сравниваем текущий вид иконки с эталоном
# «скилл готов». На откате иконка затемнена/с наложением -> совпадение падает.
# ---------------------------------------------------------------------------
def skill_template_path(key):
    return os.path.join(_TEMPLATE_DIR, "skill_ready_" + _sanitize(str(key)) + ".png")


def save_skill_template(frame, region, key):
    """Снять эталон «скилл готов» из зоны иконки хотбара (grayscale)."""
    rgn = config.CAPTURE_REGION or {"left": 0, "top": 0}
    x = region["left"] - rgn["left"]; y = region["top"] - rgn["top"]
    h, w = frame.shape[:2]
    x0 = max(0, x); y0 = max(0, y)
    x1 = min(w, x + region["width"]); y1 = min(h, y + region["height"])
    if x1 <= x0 or y1 <= y0:
        return False
    gray = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    os.makedirs(_TEMPLATE_DIR, exist_ok=True)
    path = skill_template_path(key)
    ok = _imwrite(path, gray)
    _template_cache.pop(path, None)
    return bool(ok)


def delete_skill_template(key):
    path = skill_template_path(key)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
    _template_cache.pop(path, None)


def skill_ready(frame, region, key, threshold=None):
    """
    Готов ли скилл: похож ли текущий вид его иконки на эталон «готов».
    True — готов (или проверка не настроена: нет эталона/зоны — не блокируем).
    """
    tpl = _load_template(skill_template_path(key))
    if tpl is None or not region:
        return True
    threshold = config.SKILL_READY_THRESHOLD if threshold is None else threshold
    rgn = config.CAPTURE_REGION or {"left": 0, "top": 0}
    ox = region["left"] - rgn["left"]; oy = region["top"] - rgn["top"]
    h, w = frame.shape[:2]
    x0 = max(0, ox); y0 = max(0, oy)
    x1 = min(w, ox + region["width"]); y1 = min(h, oy + region["height"])
    if x1 <= x0 or y1 <= y0:
        return True
    crop = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    th, tw = tpl.shape[:2]
    if th > crop.shape[0] or tw > crop.shape[1]:
        return True
    res = cv2.matchTemplate(crop, tpl, cv2.TM_CCOEFF_NORMED)
    return float(res.max()) >= threshold


def match_templates_in_crop(crop, region, names, anchor_xy, threshold=None):
    """
    Общий матчинг шаблонов ников на УЖЕ вырезанной области зоны поиска `crop`
    (BGR). `region` — экранные координаты зоны (для пересчёта в экран),
    anchor_xy — точка персонажа (экран). Используется и ботом, и оверлеем, чтобы
    выбор совпадал.

    Возврат (ближайший к персонажу — первым):
        [{"box": (left, top, w, h),   # рамка ника в координатах экрана
          "x", "y": точка клика (центр по X, чуть ниже — тело моба),
          "name", "score", "nearest": bool}, ...]
    """
    if crop is None or crop.size == 0 or not names:
        return []
    threshold = config.TEMPLATE_NAME_THRESHOLD if threshold is None else threshold
    scale = float(getattr(config, "VISION_MATCH_SCALE", 1.0) or 1.0)
    scene, _ = _nameplate_mask(crop)                  # маска сцены (как у шаблона)
    sh, sw = scene.shape[:2]                           # полноразмерные габариты
    # Матчим на УМЕНЬШЕННОЙ сцене: стоимость matchTemplate ~ площадь×площадь,
    # поэтому scale=0.5 ускоряет ~в 16 раз (зона поиска бывает почти во весь
    # экран). Координаты матча делим на scale — возвращаемся в полное разрешение.
    scene_m = (scene if scale >= 0.999 else
               cv2.resize(scene, None, fx=scale, fy=scale,
                          interpolation=cv2.INTER_AREA))
    smh, smw = scene_m.shape[:2]
    ax, ay = anchor_xy
    hits = []
    for name in names:
        path = template_path(name)
        tpl = _load_template(path)
        if tpl is None:
            continue
        th, tw = tpl.shape[:2]
        if th < 4 or tw < 6 or th > sh or tw > sw:
            continue
        tpl_m = _scaled_template(path, tpl, scale)
        tmh, tmw = tpl_m.shape[:2]
        if tmh < 2 or tmw < 2 or tmh > smh or tmw > smw:
            continue
        res = cv2.matchTemplate(scene_m, tpl_m, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(res >= threshold)
        for (mx, my) in zip(xs, ys):
            left = region["left"] + int(round(mx / scale))
            top = region["top"] + int(round(my / scale))
            cx = left + tw // 2 + config.NAME_CLICK_DX
            cy = top + th + config.NAME_CLICK_DY
            hits.append({"box": (left, top, int(tw), int(th)),
                         "x": cx, "y": cy, "name": name,
                         "score": float(res[my, mx]),
                         "_d": (cx - ax) ** 2 + (cy - ay) ** 2})
    hits = _dedupe(hits)
    hits.sort(key=lambda hh: hh["_d"])
    for i, hh in enumerate(hits):
        hh["nearest"] = (i == 0)
        hh.pop("_d", None)
    return hits


def find_mobs_by_template(frame, names, region, threshold=None):
    """
    Найти мобов из белого списка по шаблонам их ников внутри зоны поиска.
    Возврат: как у match_templates_in_crop; ближайший — первым. Пусто, если ни
    один шаблон не совпал (тогда вызывающий откатится на OCR-скан).
    """
    if not names or not region:
        return []
    rgn = config.CAPTURE_REGION or {"left": 0, "top": 0}
    ox = region["left"] - rgn["left"]; oy = region["top"] - rgn["top"]
    h, w = frame.shape[:2]
    x0 = max(0, ox); y0 = max(0, oy)
    x1 = min(w, ox + region["width"]); y1 = min(h, oy + region["height"])
    if x1 <= x0 or y1 <= y0:
        return []
    return match_templates_in_crop(
        frame[y0:y1, x0:x1], region, names, _anchor_screen(frame), threshold)
