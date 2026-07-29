"""
Простые пользовательские настройки, задаваемые из панели (не через config.py).
Хранятся в l2bot/settings.json. Сейчас: зона поиска мобов (search_region).
"""
import json
import os

import config

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json")

# Соответствие ключей в settings.json и атрибутов config, которые можно
# переопределять из панели (настройки визуального поиска).
_CONFIG_OVERRIDES = {
    "vision_targeting": "VISION_TARGETING",
    "name_click_dy": "NAME_CLICK_DY",
    "vision_interval": "VISION_INTERVAL",
    "target_name_filter": "TARGET_NAME_FILTER",
    "camera_search": "CAMERA_SEARCH",
    "search_camera_after": "SEARCH_CAMERA_AFTER",
    "camera_interval": "CAMERA_INTERVAL",
    "camera_drag_distance": "CAMERA_DRAG_DISTANCE",
    "camera_step_duration": "CAMERA_STEP_DURATION",
    "camera_arc": "CAMERA_ARC",
    "camera_settle": "CAMERA_SETTLE",
    "hotkey_stop": "HOTKEY_STOP",
}


def load():
    try:
        with open(_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save(data):
    with open(_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get(key, default=None):
    return load().get(key, default)


def set(key, value):
    data = load()
    data[key] = value
    save(data)
    return data


def apply_to_config():
    """Применить сохранённые в панели переопределения к модулю config."""
    data = load()
    for key, attr in _CONFIG_OVERRIDES.items():
        if key in data:
            setattr(config, attr, data[key])
    # клавиши: сливаем с дефолтами, чтобы не терять действия, которых нет в файле
    if isinstance(data.get("keys"), dict):
        merged = dict(config.KEYS)
        for k, v in data["keys"].items():
            merged[k] = (v or None)          # пустая строка -> None (действие выкл.)
        config.KEYS = merged
    # способности: полный список из панели заменяет дефолтный
    if isinstance(data.get("skills"), list):
        config.SKILLS = data["skills"]
    # лечение/банки: сливаем с дефолтами (чтобы не потерять недостающие поля)
    if isinstance(data.get("heal"), dict):
        config.HEAL = {**config.HEAL, **data["heal"]}
    if isinstance(data.get("mp_potion"), dict):
        config.MP_POTION = {**config.MP_POTION, **data["mp_potion"]}
    # самобаффы: полный список из панели
    if isinstance(data.get("buffs"), list):
        config.BUFFS = data["buffs"]
