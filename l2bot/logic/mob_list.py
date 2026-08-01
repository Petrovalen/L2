"""
Белый список имён мобов, которых бот должен бить.
Хранится в l2bot/mobs.json (рядом с кодом). Пользователь наполняет его
кнопкой "Добавить моба" в панели (по выделенной в игре цели).
"""
import json
import os

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mobs.json")


# Кэш списка в памяти: load() зовётся из цикла бота и потока оверлея каждый
# кадр — без кэша это read+json.parse на каждый вызов. Инвалидация по mtime
# файла (+ явный сброс в save()). Наружу отдаём КОПИЮ, чтобы вызывающий
# (add/remove) не мутировал кэш.
_cache = None
_cache_key = None


def load():
    """Вернуть список имён (уникальные, с сохранением порядка). Кэшируется в
    памяти; файл перечитывается только при изменении mtime."""
    global _cache, _cache_key
    try:
        key = os.stat(_PATH).st_mtime_ns
    except OSError:
        key = None
    if _cache is not None and key == _cache_key:
        return list(_cache)
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        data = []
    names = list(dict.fromkeys(str(x).strip() for x in data if str(x).strip()))
    _cache, _cache_key = names, key
    return list(names)


def save(names):
    global _cache, _cache_key
    with open(_PATH, "w", encoding="utf-8") as f:
        json.dump(list(names), f, ensure_ascii=False, indent=2)
    _cache = None            # следующий load() перечитает свежий файл
    _cache_key = None


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
