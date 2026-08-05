"""
Белый список имён мобов, которых бот должен бить — СВОЙ для каждого профиля
персонажа. Хранится в settings.json в активном профиле персонажа (ключ "mobs"),
поэтому у разных ДД/классов свои списки.

Старый общий l2bot/mobs.json используется как источник МИГРАЦИИ: если у профиля
своего списка ещё нет, берём имена оттуда и закрепляем за профилем (одна запись).
Шаблоны ников (target_templates/<имя>.png) остаются ОБЩИМИ — они привязаны к
имени моба, а не к профилю, и переиспользуются между профилями.
"""
import json
import os

from logic import settings

# Старый общий файл — только для миграции/наполнения нового профиля.
_LEGACY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mobs.json")


def _normalize(data):
    """Список уникальных непустых имён с сохранением порядка."""
    if not isinstance(data, list):
        return []
    return list(dict.fromkeys(str(x).strip() for x in data if str(x).strip()))


def _load_legacy():
    """Прочитать старый общий mobs.json (источник миграции)."""
    try:
        with open(_LEGACY_PATH, encoding="utf-8") as f:
            return _normalize(json.load(f))
    except (FileNotFoundError, ValueError):
        return []


def load():
    """Белый список мобов АКТИВНОГО профиля персонажа. Если у профиля своего
    списка ещё нет — мигрируем из общего mobs.json и закрепляем за профилем.
    Данные берутся из кэша settings, так что вызов дешёвый (можно каждый тик)."""
    names = settings.get("mobs")
    if names is None:                    # у профиля ещё нет своего списка
        names = _load_legacy()           # засеваем из общего файла (миграция)
        settings.set("mobs", names)      # закрепить за текущим профилем (одна запись)
    return _normalize(names)


def save(names):
    """Сохранить список в АКТИВНЫЙ профиль персонажа."""
    settings.set("mobs", _normalize(names))


def add(name):
    """Добавить имя в список текущего профиля (если ещё нет). Вернуть список."""
    name = (name or "").strip()
    names = load()
    if name and name not in names:
        names.append(name)
        save(names)
    return names


def remove(name):
    """Удалить имя из списка текущего профиля. Вернуть обновлённый список."""
    names = [n for n in load() if n != name]
    save(names)
    return names


def name_used_anywhere(name):
    """Имя есть в списке мобов ХОТЯ БЫ ОДНОГО профиля персонажа? Нужно, чтобы не
    удалять ОБЩИЙ шаблон ника, если его ещё использует другой профиль."""
    name = (name or "").strip()
    if not name:
        return False
    data = settings.load()
    for prof in (data.get("characters") or {}).values():
        for n in (prof.get("mobs") or []):
            if str(n).strip() == name:
                return True
    return False
