"""
OCR через Tesseract. Нужен, когда состояние нельзя понять по цвету:
имя выбранной цели, числовые значения, системные сообщения ("Cannot see target"
и т.п.). Использовать точечно — OCR медленный по сравнению с CV.
"""
import re

import cv2
import numpy as np
import pytesseract

import config

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
    """Прочитать имя выделенной цели из config.TARGET_NAME_REGION (частный случай)."""
    return read_name(frame, config.TARGET_NAME_REGION)
