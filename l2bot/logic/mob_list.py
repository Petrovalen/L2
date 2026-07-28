"""
Белый список имён мобов, которых бот должен бить.
Хранится в l2bot/mobs.json (рядом с кодом). Пользователь наполняет его
кнопкой "Добавить моба" в панели (по выделенной в игре цели).
"""
import json
import os

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mobs.json")


def load():
    """Вернуть список имён (уникальные, с сохранением порядка)."""
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return []
    return list(dict.fromkeys(str(x).strip() for x in data if str(x).strip()))


def save(names):
    with open(_PATH, "w", encoding="utf-8") as f:
        json.dump(list(names), f, ensure_ascii=False, indent=2)


def add(name):
    """Добавить имя (если ещё нет). Вернуть обновлённый список."""
    name = (name or "").strip()
    names = load()
    if name and name not in names:
        names.append(name)
        save(names)
    return names


def remove(name):
    """Удалить имя. Вернуть обновлённый список."""
    names = [n for n in load() if n != name]
    save(names)
    return names
