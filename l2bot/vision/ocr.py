"""
OCR через Tesseract. Нужен, когда состояние нельзя понять по цвету:
имя выбранной цели, числовые значения, системные сообщения ("Cannot see target"
и т.п.). Использовать точечно — OCR медленный по сравнению с CV.
"""
import cv2
import pytesseract

import config

if config.TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD


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
