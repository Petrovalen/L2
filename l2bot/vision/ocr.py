"""
OCR через Tesseract. Нужен, когда состояние нельзя понять по цвету:
имя выбранной цели, числовые значения, системные сообщения ("Cannot see target"
и т.п.). Использовать точечно — OCR медленный по сравнению с CV.
"""
import re
import time

import cv2
import numpy as np
import pytesseract

import config
from logic import settings
from vision import digits

if config.TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD

# Параметры чтения имени цели.
_NAME_SCALE = 3          # во сколько раз увеличиваем перед OCR
_NAME_GAP_STOP = 22      # пробел (px до масштаба) шире этого = конец имени (дальше фон)


def _preprocess(frame, region):
    """Вырезать регион и подготовить под OCR (grayscale + threshold + скейл)."""
    rgn = config.CAPTURE_REGION or {"left": 0, "top": 0}
    x = region["left"] - rgn["left"]
    y = region["top"] - rgn["top"]
    crop = frame[y:y + region["height"], x:x + region["width"]]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return thresh


def read_text(frame, region, lang="eng", config_str="--psm 7"):
    """
    Прочитать текст из прямоугольного региона экрана.
    region = {"left","top","width","height"} в координатах экрана.
    psm 7 = "одна строка текста".
    """
    img = _preprocess(frame, region)
    text = pytesseract.image_to_string(img, lang=lang, config=config_str)
    return text.strip()


def _clean_name(raw):
    """Оставить только буквы/пробел/апостроф, схлопнуть пробелы."""
    clean = re.sub(r"[^A-Za-z' ]", " ", raw)
    return re.sub(r"\s+", " ", clean).strip()


def read_name(frame, region, trim=True):
    """
    Прочитать имя-текст (светлый на «живом» фоне) из прямоугольной области экрана.
    region = {"left","top","width","height"} в координатах экрана.

    Выделяем яркий текст порогом NAME_BRIGHT, при trim=True обрезаем всё правее
    большого пробела (там уже фон), распознаём и чистим. '' если не прочиталось.
    """
    rgn = config.CAPTURE_REGION or {"left": 0, "top": 0}
    x = region["left"] - rgn["left"]
    y = region["top"] - rgn["top"]
    h, w = frame.shape[:2]
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(w, x + region["width"])
    y1 = min(h, y + region["height"])
    if x1 <= x0 or y1 <= y0:
        return ""
    crop = frame[y0:y1, x0:x1]

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=_NAME_SCALE, fy=_NAME_SCALE,
                      interpolation=cv2.INTER_CUBIC)
    _, br = cv2.threshold(gray, config.NAME_BRIGHT, 255, cv2.THRESH_BINARY)

    if trim:
        # обрезка по тексту: режем всё правее широкого пробела (фон)
        col = br.sum(axis=0) > 0
        xs = np.where(col)[0]
        if xs.size:
            start = xs[0]
            end = start
            gap = 0
            stop = _NAME_GAP_STOP * _NAME_SCALE
            for cx in range(start, len(col)):
                if col[cx]:
                    end = cx
                    gap = 0
                else:
                    gap += 1
                    if gap > stop:
                        break
            pad = 4
            br = br[:, max(0, start - pad):min(br.shape[1], end + pad)]

    img = cv2.bitwise_not(br)   # тёмный текст на белом — так tesseract точнее
    raw = pytesseract.image_to_string(img, lang="eng", config="--psm 7").strip()
    return _clean_name(raw)


def read_target_name(frame):
    """Прочитать имя выделенной цели из откалиброванной области (или из config)."""
    region = settings.get("target_name_region") or config.TARGET_NAME_REGION
    return read_name(frame, region)


# ---------------------------------------------------------------------------
# Чтение ЧИСЕЛ (режим «цифры» для баров HP/MP/CP/цели).
# ---------------------------------------------------------------------------
_NUM_SCALE = 3   # увеличение перед OCR


def _crop_region(frame, region):
    """Вырезать прямоугольную область экрана из кадра (с клиппингом). None вне кадра."""
    rgn = config.CAPTURE_REGION or {"left": 0, "top": 0}
    x = region["left"] - rgn["left"]
    y = region["top"] - rgn["top"]
    h, w = frame.shape[:2]
    x0 = max(0, x); y0 = max(0, y)
    x1 = min(w, x + region["width"]); y1 = min(h, y + region["height"])
    if x1 <= x0 or y1 <= y0:
        return None
    return frame[y0:y1, x0:x1]


def _parse_number(raw):
    """
    Разобрать распознанный текст в (current, maximum|None).

    Поддерживает форматы «1234/5678» (оба числа) и «1234» (только текущее).
    None — если чисел нет.
    """
    s = raw.strip()
    # приоритет — явный формат «тек/макс»
    m = re.search(r"(\d+)\s*/\s*(\d+)", s)
    if m:
        mx = int(m.group(2))
        return int(m.group(1)), (mx if mx > 0 else None)
    # OCR мог потерять тонкий штрих «/» (увидел «1500 3000» / «15003000» слитно).
    # Если чисел ДВА — считаем их «тек» и «макс».
    nums = re.findall(r"\d+", s)
    if len(nums) >= 2:
        mx = int(nums[1])
        return int(nums[0]), (mx if mx > 0 else None)
    if len(nums) == 1:
        return int(nums[0]), None
    return None


def _read_number_tesseract(frame, region):
    """Сырой текст числа из региона через Tesseract (медленно, ~333мс). '' если пусто."""
    crop = _crop_region(frame, region)
    if crop is None or crop.size == 0:
        return ""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=_NUM_SCALE, fy=_NUM_SCALE,
                      interpolation=cv2.INTER_CUBIC)
    _, br = cv2.threshold(gray, config.NAME_BRIGHT, 255, cv2.THRESH_BINARY)
    img = cv2.bitwise_not(br)
    return pytesseract.image_to_string(
        img, lang="eng",
        config="--psm 7 -c tessedit_char_whitelist=0123456789/").strip()


# Троттлинг медленного Tesseract-отката: даже если быстрый распознаватель не
# смог, НЕ зовём Tesseract на каждом кадре (иначе нераспознаваемый бар стопорит
# цикл на 333мс каждый тик). Зовём изредка — только чтобы дообучить эталоны.
_last_tess = 0.0
_TESS_LEARN_GAP = 2.0        # сек между попытками дообучения через Tesseract


def read_number(frame, region):
    """
    Прочитать число(а) из региона. Возврат: (current, maximum|None) или None.

    Сначала — БЫСТРЫЙ распознаватель цифр по эталонам (~1мс, vision.digits).
    Если встретился незнакомый глиф — РЕДКО (раз в _TESS_LEARN_GAP) дочитываем
    медленным Tesseract и обучаем эталоны; между попытками возвращаем None,
    чтобы не стопорить цикл (вызывающий возьмёт последнее значение/заливку).
    """
    global _last_tess
    text, norms = digits.read(frame, region)
    if text is not None:
        return _parse_number(text)
    now = time.monotonic()
    if now - _last_tess < _TESS_LEARN_GAP:
        return None                       # не мучаем Tesseract каждый кадр
    _last_tess = now
    raw = _read_number_tesseract(frame, region)
    if raw:
        digits.learn(norms, raw)          # запоминаем начертания для быстрого пути
        return _parse_number(raw)
    return None
