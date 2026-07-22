"""
Ввод под DirectInput. L2 (как и многие игры на DirectX) часто игнорирует
обычный pyautogui/keybd_event для клавиш — нужен pydirectinput (scan-коды).

Все действия слегка "очеловечены": случайное удержание клавиши и джиттер пауз,
чтобы поведение не было идеально ритмичным.
"""
import random
import time

import pydirectinput

import config

# Отключаем встроенную паузу pydirectinput между вызовами — управляем сами.
pydirectinput.PAUSE = 0.0
# failsafe: увод мыши в угол экрана прерывает — оставим включённым для страховки.
pydirectinput.FAILSAFE = True

# Время последнего нажатия по каждому логическому действию (для кулдаунов).
# Ключ — имя действия из config.KEYS, значение — момент time.monotonic().
_last_action_ts = {}

# Необязательный колбэк: вызывается с именем действия при КАЖДОМ реальном
# нажатии (после того как клавиша отправлена). GUI подписывается на него,
# чтобы показывать ленту действий. По умолчанию None — CLI-режим не трогает.
on_action = None


def cooldown_remaining(action_name):
    """Сколько ещё секунд ждать до готовности действия (0.0 — готово)."""
    cd = config.ACTION_COOLDOWNS.get(action_name, config.DEFAULT_COOLDOWN)
    if cd <= 0:
        return 0.0
    elapsed = time.monotonic() - _last_action_ts.get(action_name, -1e9)
    return max(0.0, cd - elapsed)


def reset_cooldowns():
    """Сбросить все кулдауны (например, при снятии с паузы)."""
    _last_action_ts.clear()


def _jittered(value):
    """Добавить случайный разброс ±HUMANIZE_JITTER к паузе."""
    j = config.HUMANIZE_JITTER
    return value * (1.0 + random.uniform(-j, j))


def sleep(seconds):
    time.sleep(max(0.0, _jittered(seconds)))


def press_key(key):
    """Нажать и отпустить клавишу с человекоподобным удержанием."""
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
    if respect_cooldown and cooldown_remaining(action_name) > 0:
        return False
    press_key(key)
    _last_action_ts[action_name] = time.monotonic()
    if on_action is not None:
        try:
            on_action(action_name)
        except Exception:
            pass  # GUI-логгер не должен ронять бота
    return True


def move_mouse(x, y, duration=0.15):
    """Плавно подвести курсор к точке экрана."""
    pydirectinput.moveTo(int(x), int(y), duration=max(0.0, _jittered(duration)))


def click(x=None, y=None, button="left"):
    """Клик (опционально с предварительным перемещением)."""
    if x is not None and y is not None:
        move_mouse(x, y)
        sleep(0.05)
    pydirectinput.click(button=button)
