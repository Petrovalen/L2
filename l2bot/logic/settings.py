"""
Пользовательские настройки из панели (не через config.py). Хранятся в
l2bot/settings.json.

ПРОФИЛИ. Настройки разделены на два независимых набора профилей + общий блок:

  * ПРОФИЛЬ КОМПЬЮТЕРА (machines) — всё, что зависит от экрана/железа:
    регион захвата и монитор, калибровки полосок bar_* (+ режим цифр),
    зоны target_name_region / search_region / buff_region, точка персонажа.
  * ПРОФИЛЬ ПЕРСОНАЖА (characters) — всё, что зависит от класса:
    клавиши (keys), способности (skills), баффы (buffs), лечение/мана.
  * ОБЩЕЕ (корень файла) — камера/мышь, смещение клика, тайминги поиска,
    число нажатий лута, Telegram, горячие клавиши.

Активный ПК и активный персонаж выбираются вручную (active_machine /
active_character). get()/set() сами маршрутизируют ключ в нужный профиль по
таблицам _MACHINE_KEYS / _CHARACTER_KEYS — поэтому остальной код (fsm/bars/
ocr/targets) продолжает звать settings.get("bar_hp") и т.п. без изменений.

Формат файла:
  {
    "active_machine": "default", "active_character": "default",
    "machines":   {"default": { <машинные ключи> }},
    "characters": {"default": { <персонажные ключи> }},
    <общие ключи прямо в корне>
  }

Старый «плоский» settings.json (все ключи в корне) мигрируется автоматически
при загрузке: машинные -> machines.default, персонажные -> characters.default.
"""
import json
import os

import config

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json")

# Ключи, относящиеся к ПРОФИЛЮ КОМПЬЮТЕРА (зависят от экрана/железа).
_MACHINE_KEYS = {
    "capture_region", "monitor_index",
    "bar_hp", "bar_mp", "bar_cp", "bar_target",
    "bar_hp_digits", "bar_mp_digits", "bar_cp_digits", "bar_target_digits",
    "target_name_region", "search_region", "buff_region", "character_anchor",
    "vision_exclude_region",
}
# Ключи, относящиеся к ПРОФИЛЮ ПЕРСОНАЖА (зависят от класса).
_CHARACTER_KEYS = {"keys", "skills", "heal", "mp_potion", "buffs"}

_DEFAULT = "default"

# Соответствие ОБЩИХ ключей (корень файла) и атрибутов config, которые можно
# переопределять из панели.
_CONFIG_OVERRIDES = {
    "vision_targeting": "VISION_TARGETING",
    "name_click_dx": "NAME_CLICK_DX",
    "name_click_dy": "NAME_CLICK_DY",
    "vision_interval": "VISION_INTERVAL",
    "target_name_filter": "TARGET_NAME_FILTER",
    "camera_search": "CAMERA_SEARCH",
    "search_camera_after": "SEARCH_CAMERA_AFTER",
    "camera_interval": "CAMERA_INTERVAL",
    "camera_drag_distance": "CAMERA_DRAG_DISTANCE",
    "camera_step_duration": "CAMERA_STEP_DURATION",
    "loot_presses_min": "LOOT_PRESSES_MIN",
    "loot_presses_max": "LOOT_PRESSES_MAX",
    "death_notify": "DEATH_NOTIFY",
    "cp_notify": "CP_NOTIFY",
    "telegram_token": "TELEGRAM_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
    "camera_arc": "CAMERA_ARC",
    "camera_settle": "CAMERA_SETTLE",
    "hotkey_stop": "HOTKEY_STOP",
    "assist_mode": "ASSIST_MODE",
}


# ---------------------------------------------------------------------------
# Загрузка / сохранение с нормализацией формата (миграция старого «плоского»).
# ---------------------------------------------------------------------------
def _normalize(data):
    """Привести данные к профильному формату (мигрировать старый плоский файл)."""
    if not isinstance(data, dict):
        data = {}
    if "machines" in data or "characters" in data:
        # уже новый формат — гарантируем непустые контейнеры и активные имена
        data.setdefault("machines", {_DEFAULT: {}})
        data.setdefault("characters", {_DEFAULT: {}})
    else:
        # старый плоский формат -> раскладываем по профилям default
        machine = {k: data.pop(k) for k in list(data) if k in _MACHINE_KEYS}
        character = {k: data.pop(k) for k in list(data) if k in _CHARACTER_KEYS}
        data["machines"] = {_DEFAULT: machine}
        data["characters"] = {_DEFAULT: character}
        data["active_machine"] = _DEFAULT
        data["active_character"] = _DEFAULT
    if not data["machines"]:
        data["machines"] = {_DEFAULT: {}}
    if not data["characters"]:
        data["characters"] = {_DEFAULT: {}}
    # активные имена должны указывать на существующий профиль
    if data.get("active_machine") not in data["machines"]:
        data["active_machine"] = next(iter(data["machines"]))
    if data.get("active_character") not in data["characters"]:
        data["active_character"] = next(iter(data["characters"]))
    return data


# Кэш распарсенного файла в памяти. get() зовётся десятками раз за тик бота и
# из потока оверлея — без кэша это был бы дисковый read+json.parse на каждый
# вызов (тормозило цикл и редко обновляло рамки мобов). Инвалидация — по mtime
# файла (внешняя правка подхватится) и явным сбросом в save().
_cache = None
_cache_key = None


def load():
    """Прочитать настройки (нормализованные к профильному формату). Результат
    кэшируется в памяти; повторное чтение файла — только при изменении mtime."""
    global _cache, _cache_key
    try:
        key = os.stat(_PATH).st_mtime_ns
    except OSError:
        key = None
    if _cache is not None and key == _cache_key:
        return _cache
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        data = {}
    data = _normalize(data)
    _cache, _cache_key = data, key
    return data


def save(data):
    global _cache, _cache_key
    data = _normalize(data)
    with open(_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _cache = None            # следующий load() перечитает свежий файл
    _cache_key = None


# ---------------------------------------------------------------------------
# Профиле-зависимые get / set (маршрутизация по имени ключа).
# ---------------------------------------------------------------------------
def _active_profile(data, kind):
    """Словарь активного профиля ('machines'|'characters'). Создаётся при нужде."""
    active = "active_machine" if kind == "machines" else "active_character"
    name = data.get(active) or _DEFAULT
    return data.setdefault(kind, {}).setdefault(name, {})


def get(key, default=None):
    """Прочитать значение с учётом активного профиля (см. _MACHINE_KEYS/_CHARACTER_KEYS)."""
    data = load()
    if key in _MACHINE_KEYS:
        return _active_profile(data, "machines").get(key, default)
    if key in _CHARACTER_KEYS:
        return _active_profile(data, "characters").get(key, default)
    return data.get(key, default)


def set(key, value):
    """Записать значение в нужный профиль (или в общий корень) и сохранить."""
    data = load()
    if key in _MACHINE_KEYS:
        _active_profile(data, "machines")[key] = value
    elif key in _CHARACTER_KEYS:
        _active_profile(data, "characters")[key] = value
    else:
        data[key] = value
    save(data)
    return data


# ---------------------------------------------------------------------------
# Управление профилями (список / выбор / создание / переименование / удаление).
# ---------------------------------------------------------------------------
def _kind(profile_kind):
    """'machine'|'character' -> ('machines'|'characters', 'active_*')."""
    if profile_kind == "machine":
        return "machines", "active_machine"
    if profile_kind == "character":
        return "characters", "active_character"
    raise ValueError("profile_kind должен быть 'machine' или 'character'")


def list_profiles(profile_kind):
    """Имена профилей выбранного набора."""
    container, _ = _kind(profile_kind)
    return list(load().get(container, {}).keys())


def active_profile(profile_kind):
    """Имя активного профиля выбранного набора."""
    _, active = _kind(profile_kind)
    return load().get(active)


def set_active(profile_kind, name):
    """Сделать профиль активным (если существует)."""
    container, active = _kind(profile_kind)
    data = load()
    if name in data.get(container, {}):
        data[active] = name
        save(data)
    return data


def create_profile(profile_kind, name, copy_from=None):
    """Создать профиль (опц. копией существующего) и сделать активным."""
    container, active = _kind(profile_kind)
    data = load()
    profs = data.setdefault(container, {})
    if name in profs:
        return data                         # уже есть — не затираем
    src = profs.get(copy_from, {}) if copy_from else {}
    profs[name] = json.loads(json.dumps(src))   # глубокая копия
    data[active] = name
    save(data)
    return data


def rename_profile(profile_kind, old, new):
    """Переименовать профиль (и обновить активное имя, если он был активным)."""
    container, active = _kind(profile_kind)
    data = load()
    profs = data.get(container, {})
    if old not in profs or not new or new in profs:
        return data
    # сохранить порядок ключей: пересобираем словарь
    data[container] = {(new if k == old else k): v for k, v in profs.items()}
    if data.get(active) == old:
        data[active] = new
    save(data)
    return data


def delete_profile(profile_kind, name):
    """Удалить профиль (нельзя удалить последний; активный переносится)."""
    container, active = _kind(profile_kind)
    data = load()
    profs = data.get(container, {})
    if name not in profs or len(profs) <= 1:
        return data                         # последний профиль не удаляем
    profs.pop(name)
    if data.get(active) == name:
        data[active] = next(iter(profs))
    save(data)
    return data


# ---------------------------------------------------------------------------
# Применение к config (учитывает активные профили).
# ---------------------------------------------------------------------------
def apply_to_config():
    """Применить сохранённые настройки (общие + активные профили) к config."""
    data = load()
    # общие переопределения (корень файла)
    for key, attr in _CONFIG_OVERRIDES.items():
        if key in data:
            setattr(config, attr, data[key])
    # ПРОФИЛЬ КОМПЬЮТЕРА: параметры захвата экрана (если заданы)
    machine = _active_profile(data, "machines")
    if machine.get("capture_region") is not None:
        config.CAPTURE_REGION = machine["capture_region"]
    if machine.get("monitor_index"):
        config.MONITOR_INDEX = machine["monitor_index"]
    # ПРОФИЛЬ ПЕРСОНАЖА: клавиши/скиллы/баффы/лечение
    character = _active_profile(data, "characters")
    if isinstance(character.get("keys"), dict):
        merged = dict(config.KEYS)
        for k, v in character["keys"].items():
            merged[k] = (v or None)          # пустая строка -> None (действие выкл.)
        config.KEYS = merged
    if isinstance(character.get("skills"), list):
        config.SKILLS = character["skills"]
    if isinstance(character.get("heal"), dict):
        config.HEAL = {**config.HEAL, **character["heal"]}
    if isinstance(character.get("mp_potion"), dict):
        config.MP_POTION = {**config.MP_POTION, **character["mp_potion"]}
    if isinstance(character.get("buffs"), list):
        config.BUFFS = character["buffs"]
