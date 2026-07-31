"""
Ввод под DirectInput. L2 (как и многие игры на DirectX) часто игнорирует
обычный pyautogui/keybd_event для клавиш — нужен pydirectinput (scan-коды).

Все действия слегка "очеловечены": случайное удержание клавиши и джиттер пауз,
чтобы поведение не было идеально ритмичным.
"""
import ctypes
import math
import random
import time

import pydirectinput

import config
from control import arduino_link

# Отключаем встроенную паузу pydirectinput между вызовами — управляем сами.
pydirectinput.PAUSE = 0.0
# failsafe: увод мыши в угол экрана прерывает — оставим включённым для страховки.
pydirectinput.FAILSAFE = True

# Момент (time.monotonic), когда действие снова разрешено — с учётом джиттера.
# Ключ — имя действия из config.KEYS.
_ready_at = {}

# Необязательный колбэк: вызывается с именем действия при КАЖДОМ реальном
# нажатии (после того как клавиша отправлена). GUI подписывается на него,
# чтобы показывать ленту действий. По умолчанию None — CLI-режим не трогает.
on_action = None

# Необязательный колбэк для свободных сообщений в ленту (напр. визуальный клик).
on_event = None


def emit(message):
    """Отправить свободное сообщение в ленту, если кто-то подписан."""
    if on_event is not None:
        try:
            on_event(message)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# ВЫБОР БЭКЕНДА ВВОДА: Arduino (аппаратный HID) или Windows (pydirectinput).
# ---------------------------------------------------------------------------
_arduino_init_done = False   # попытку подключения к Arduino делаем один раз


def _arduino():
    """
    Вернуть подключённый ArduinoLink, если бэкенд это разрешает и связь есть;
    иначе None (вызывающий уйдёт на ввод Windows, если бэкенд != 'arduino').
    Подключение пробуем ОДИН раз (open+reset занимает ~2 c) и запоминаем итог.
    """
    global _arduino_init_done
    backend = getattr(config, "INPUT_BACKEND", "auto")
    if backend == "windows":
        return None
    link = arduino_link.get_link()
    if link.is_connected():
        return link
    if not _arduino_init_done:
        _arduino_init_done = True
        if link.connect():
            emit("Arduino-мост ввода подключён (%s)" % link.port)
        else:
            emit("Arduino не найден — ввод через Windows" if backend == "auto"
                 else "Arduino не найден, а бэкенд 'arduino' — ввод не отправляется")
    return link if link.is_connected() else None


def reconnect_arduino():
    """Сбросить флаг и попытаться переподключиться (для кнопки в панели)."""
    global _arduino_init_done
    _arduino_init_done = False
    arduino_link.get_link().close()
    return _arduino() is not None


def _set_cursor(x, y):
    """Поставить системный курсор в точку (для клика Arduino по абсолютным коорд.)."""
    try:
        ctypes.windll.user32.SetCursorPos(int(x), int(y))
    except Exception:
        pass


def _backend_is_arduino_only():
    return getattr(config, "INPUT_BACKEND", "auto") == "arduino"


def cooldown_remaining(action_name):
    """Сколько ещё секунд ждать до готовности действия (0.0 — готово)."""
    return max(0.0, _ready_at.get(action_name, 0.0) - time.monotonic())


def reset_cooldowns():
    """Сбросить все кулдауны (например, при снятии с паузы)."""
    _ready_at.clear()


def reaction_delay():
    """Короткая человеческая пауза-реакция перед действием по событию."""
    time.sleep(random.uniform(config.REACTION_MIN, config.REACTION_MAX))


def _jittered(value):
    """Добавить случайный разброс ±HUMANIZE_JITTER к паузе."""
    j = config.HUMANIZE_JITTER
    return value * (1.0 + random.uniform(-j, j))


def sleep(seconds):
    time.sleep(max(0.0, _jittered(seconds)))


def press_key(key):
    """
    Нажать и отпустить клавишу. Через Arduino (аппаратный HID) — если бэкенд
    позволяет и мост подключён; иначе обычный ввод Windows (pydirectinput).
    Удержание при Arduino задаётся в скетче (человекоподобное).
    """
    a = _arduino()
    if a is not None:
        a.key(str(key))
        return
    if _backend_is_arduino_only():
        return                      # требовали только Arduino, а его нет — не шлём
    hold = random.uniform(config.KEY_PRESS_MIN, config.KEY_PRESS_MAX)
    pydirectinput.keyDown(key)
    time.sleep(hold)
    pydirectinput.keyUp(key)


def press_action(action_name, respect_cooldown=True):
    """
    Нажать клавишу по логическому имени из config.KEYS.

    Возвращает True, если клавиша реально нажата; False — если действие не
    задано (ключ None/пусто) или ещё не вышел его кулдаун.
    respect_cooldown=False — форсировать нажатие, игнорируя кулдаун.
    """
    key = config.KEYS.get(action_name)
    if not key:
        return False
    now = time.monotonic()
    if respect_cooldown and now < _ready_at.get(action_name, 0.0):
        return False
    press_key(key)
    # джиттер кулдауна: следующее срабатывание не строго по таймеру
    cd = config.ACTION_COOLDOWNS.get(action_name, config.DEFAULT_COOLDOWN)
    jitter = 1.0 + random.uniform(-config.COOLDOWN_JITTER, config.COOLDOWN_JITTER)
    _ready_at[action_name] = now + max(0.0, cd * jitter)
    if on_action is not None:
        try:
            on_action(action_name)
        except Exception:
            pass  # GUI-логгер не должен ронять бота
    return True


def press_skill(cd_key, key, cooldown):
    """
    Нажать клавишу способности с индивидуальным кулдауном.

    cd_key — уникальный ключ кулдауна (напр. 'skill_0'), чтобы у каждой
    способности был свой таймер. Возвращает True, если реально нажали.
    Логирование — на стороне вызывающего (через emit), т.к. подпись у скилла своя.
    """
    if not key:
        return False
    now = time.monotonic()
    if now < _ready_at.get(cd_key, 0.0):
        return False
    press_key(key)
    jitter = 1.0 + random.uniform(-config.COOLDOWN_JITTER, config.COOLDOWN_JITTER)
    _ready_at[cd_key] = now + max(0.0, cooldown * jitter)
    return True


def move_mouse(x, y, duration=0.15):
    """Плавно подвести курсор к точке экрана."""
    pydirectinput.moveTo(int(x), int(y), duration=max(0.0, _jittered(duration)))


def click(x=None, y=None, button="left"):
    """
    Клик (опц. с предварительным перемещением). Через Arduino: ставим курсор в
    точку системно (SetCursorPos), а сам клик шлём аппаратно (игра принимает).
    """
    a = _arduino()
    if a is not None:
        if x is not None and y is not None:
            _set_cursor(x, y)
            sleep(0.05)
        if button == "right":
            a.rclick()
        else:
            a.click()
        return
    if _backend_is_arduino_only():
        return
    if x is not None and y is not None:
        move_mouse(x, y)
        sleep(0.05)
    pydirectinput.click(button=button)


# Центр основного экрана (кэш). Нужен для реколибровки курсора перед поворотом.
_SCREEN_CENTER = None


def _screen_center():
    """Центр основного монитора (px). Кэшируется; фолбэк 960x540."""
    global _SCREEN_CENTER
    if _SCREEN_CENTER is None:
        try:
            u = ctypes.windll.user32
            _SCREEN_CENTER = (u.GetSystemMetrics(0) // 2, u.GetSystemMetrics(1) // 2)
        except Exception:
            _SCREEN_CENTER = (960, 540)
    return _SCREEN_CENTER


def camera_drag(dx, dy=0, center=None, duration=None):
    """
    Повернуть камеру: поставить курсор в точку реколибровки, зажать ПКМ и
    потянуть мышь на (dx, dy) пикселей по шагам, затем отпустить. В L2
    удержание правой кнопки + движение мыши вращает камеру; персонаж при этом
    стоит на месте.

    center=(x, y) — точка реколибровки курсора (экранные координаты). Обычно
    это центр зоны поиска мобов: она заведомо над игровым миром, а не над UI.
    None -> центр основного экрана (запасной вариант).

    Курсор реколибруем перед КАЖДЫМ поворотом: относительные сдвиги двигают
    системный курсор, и без реколибровки он упёрся бы в край экрана — после
    чего камера перестала бы вращаться. Реколибровка идёт с ОТПУЩЕННОЙ ПКМ,
    поэтому на камеру не влияет.

    ПКМ отпускаем в finally — чтобы кнопка не «залипла» при исключении
    (в т.ч. при срабатывании failsafe).
    """
    if duration is None:
        duration = config.CAMERA_STEP_DURATION
    cx, cy = center if center else _screen_center()

    # Arduino: ставим курсор в точку реколибровки системно, а поворот (зажатая
    # ПКМ + плавный сдвиг) отдаём аппаратному мосту — сглаживание уже в скетче.
    a = _arduino()
    if a is not None:
        _set_cursor(cx, cy)
        time.sleep(_jittered(config.CAMERA_SETTLE))
        a.drag(int(dx), int(dy), int(max(0.04, duration) * 1000))
        return
    if _backend_is_arduino_only():
        return

    pydirectinput.moveTo(int(cx), int(cy))

    # Человекоподобный свайп (по мотивам smoothMove из прошивки LA2Pixel):
    #  - smoothstep-ускорение/замедление: курсор трогается и тормозит плавно,
    #    а не идёт с постоянной скоростью (равномерность выдаёт бота);
    #  - лёгкая ДУГА поперёк движения со случайной кривизной — путь не идеально
    #    прямой, как у живой руки;
    #  - джиттер задержки на каждом шаге;
    #  - пауза после зажатия ПКМ и перед отпусканием — чтобы игра успела
    #    зарегистрировать состояние кнопки (без неё поворот может «съесться»).
    dist = math.hypot(dx, dy)
    # Шаги по РАССТОЯНИЮ (~6px на шаг), не по времени: слишком мелкие сдвиги
    # (1–2px) игра может «не замечать», поэтому держим шаг крупным, но плавным.
    steps = max(4, min(20, int(dist / 6)))
    per = duration / steps
    curve = random.uniform(-config.CAMERA_ARC, config.CAMERA_ARC)
    pydirectinput.mouseDown(button="right")
    try:
        time.sleep(_jittered(config.CAMERA_SETTLE))
        sent_x = sent_y = 0
        for i in range(1, steps + 1):
            t = i / steps
            eased = t * t * (3.0 - 2.0 * t)             # smoothstep 0->1
            arc = math.sin(math.pi * t) * curve         # 0 на концах, max в середине
            off_x = (-dy / dist) * arc if dist else 0.0  # смещение ПЕРПЕНДИКУЛЯРНО пути
            off_y = (dx / dist) * arc if dist else 0.0
            tx = round(dx * eased + off_x)
            ty = round(dy * eased + off_y)
            mx, my = tx - sent_x, ty - sent_y
            if mx or my:
                pydirectinput.moveRel(mx, my)
            sent_x, sent_y = tx, ty
            time.sleep(max(0.0, per + random.uniform(0.0, 0.004)))
        time.sleep(_jittered(config.CAMERA_SETTLE))
    finally:
        pydirectinput.mouseUp(button="right")
