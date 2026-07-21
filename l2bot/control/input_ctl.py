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


def press_action(action_name):
    """Нажать клавишу по логическому имени из config.KEYS."""
    key = config.KEYS.get(action_name)
    if not key:
        return False
    press_key(key)
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
