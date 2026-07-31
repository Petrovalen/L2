"""
Быстрый распознаватель ЦИФР (0-9 и '/') для баров HP/MP/CP/цели.

Зачем: Tesseract запускает процесс на каждое чтение (~333мс) — слишком медленно,
из-за чего HP цели обновляется редко и порог скилла ловится неточно. Здесь —
лёгкое сопоставление глифов с эталонами (~1мс), без внешней программы.

Самообучение: эталоны глифов НЕ задаются вручную. При первом появлении незнакомой
цифры модуль возвращает None (не смог), вызывающий читает медленным Tesseract и
передаёт распознанный текст сюда в learn() — мы сегментируем ту же картинку и
запоминаем начертания. Через несколько чтений все цифры выучены → дальше быстро.
Эталоны кэшируются в digit_templates.json (шрифт/разрешение-зависимы, локальны).
"""
import json
import os

import numpy as np
import cv2

import config

_TPL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "digit_templates.json")

_GW, _GH = 12, 18            # нормализованный размер глифа
_MATCH_MIN = 0.86            # мин. доля совпавших пикселей для уверенного матча
_MAX_PER_CHAR = 3            # сколько эталонов храним на символ (устойчивость к дрожи)
_MIN_INK_COLS = 1            # мин. ширина глифа в столбцах

_templates = None            # {char: [np.uint8 (_GH,_GW) 0/1, ...]}


def _load():
    global _templates
    if _templates is not None:
        return _templates
    _templates = {}
    try:
        with open(_TPL_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        for ch, arrs in raw.items():
            _templates[ch] = [np.array(a, dtype=np.uint8) for a in arrs]
    except (FileNotFoundError, ValueError):
        pass
    return _templates


def _save():
    if _templates is None:
        return
    try:
        data = {ch: [a.tolist() for a in arrs] for ch, arrs in _templates.items()}
        with open(_TPL_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def _binary(frame, region):
    """Вырезать регион и получить бинарную маску яркого текста (1 = текст)."""
    rgn = config.CAPTURE_REGION or {"left": 0, "top": 0}
    x = region["left"] - rgn["left"]
    y = region["top"] - rgn["top"]
    h, w = frame.shape[:2]
    x0 = max(0, x); y0 = max(0, y)
    x1 = min(w, x + region["width"]); y1 = min(h, y + region["height"])
    if x1 <= x0 or y1 <= y0:
        return None
    crop = frame[y0:y1, x0:x1]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, b = cv2.threshold(gray, config.NAME_BRIGHT, 255, cv2.THRESH_BINARY)
    return (b > 0).astype(np.uint8)


def _segment(binary):
    """
    Разбить маску на глифы по вертикальным пробелам (слева-направо).

    Рвём НЕ на каждом пустом столбце: тонкие внутрибуквенные разрывы (от
    антиалиасинга/порога) не должны дробить цифру. Склеиваем соседние куски,
    если зазор между ними меньше _min_gap (доля высоты). Так «128» = 3 глифа,
    а не 5.
    """
    if binary is None or binary.size == 0:
        return []
    h = binary.shape[0]
    min_gap = max(2, int(round(h * 0.18)))     # зазор меньше => один глиф
    colink = binary.sum(axis=0) > 0
    W = len(colink)
    # 1) непрерывные участки «чернил» по столбцам
    runs = []
    x = 0
    while x < W:
        if colink[x]:
            x0 = x
            while x < W and colink[x]:
                x += 1
            runs.append([x0, x])
        else:
            x += 1
    if not runs:
        return []
    # 2) склеить участки, разделённые МАЛЫМ зазором (внутри одной цифры)
    merged = [runs[0][:]]
    for s, e in runs[1:]:
        if s - merged[-1][1] < min_gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    # 3) вырезать глифы (с обрезкой по вертикали)
    glyphs = []
    for s, e in merged:
        if e - s < _MIN_INK_COLS:
            continue
        sub = binary[:, s:e]
        rows = np.where(sub.sum(axis=1) > 0)[0]
        if rows.size == 0:
            continue
        glyphs.append(sub[rows[0]:rows[-1] + 1, :])
    return glyphs


def _norm(glyph):
    """Нормализовать глиф к фиксированному размеру и в 0/1."""
    g = cv2.resize(glyph.astype(np.uint8) * 255, (_GW, _GH),
                   interpolation=cv2.INTER_AREA)
    return (g >= 128).astype(np.uint8)


def _match(norm, tpls):
    """Лучший символ по доле совпавших пикселей; None если ниже порога."""
    best_ch, best = None, 0.0
    total = norm.size
    for ch, arrs in tpls.items():
        for t in arrs:
            score = float(np.count_nonzero(t == norm)) / total
            if score > best:
                best, best_ch = score, ch
    return best_ch if best >= _MATCH_MIN else None


def read(frame, region):
    """
    Быстро прочитать число из региона.
    Возврат: (текст|None, norms). текст=None если хоть один глиф не распознан
    (тогда вызывающий пусть читает Tesseract и позовёт learn(norms, текст)).
    """
    tpls = _load()
    binary = _binary(frame, region)
    glyphs = _segment(binary)
    if not glyphs:
        return None, []
    norms = [_norm(g) for g in glyphs]
    chars = [_match(n, tpls) for n in norms]
    text = "".join(chars) if all(c is not None for c in chars) else None
    return text, norms


def learn(norms, text):
    """Запомнить эталоны: символы text должны 1:1 соответствовать глифам norms."""
    text = (text or "").strip()
    if not norms or len(norms) != len(text):
        return                                  # выравнивание не сошлось — не учим
    if any(c not in "0123456789/" for c in text):
        return
    tpls = _load()
    changed = False
    for n, ch in zip(norms, text):
        arrs = tpls.setdefault(ch, [])
        # не плодим дубликаты почти одинаковых эталонов
        if any(float(np.count_nonzero(a == n)) / n.size >= 0.97 for a in arrs):
            continue
        arrs.append(n)
        if len(arrs) > _MAX_PER_CHAR:
            arrs.pop(0)
        changed = True
    if changed:
        _save()


def ready_chars():
    """Сколько символов уже выучено (для диагностики)."""
    return sorted(_load().keys())
