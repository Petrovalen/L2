"""
Пошаговый калибратор l2bot.

В отличие от tools_calibrate.py (который просто печатает координату+цвет),
этот инструмент ВЕДЁТ по шагам: спрашивает каждую нужную точку, собирает
x1/x2/y/color для каждой полоски и в конце пишет готовый блок в файл
calibration_result.txt — его потом впишем в config.py.

ЗАПУСК (в СВОЁМ терминале, не через ассистента!), игра — в оконном режиме:
    python tools_calibrate_guided.py

Управление:
    ПРОБЕЛ — записать точку под курсором
    S      — пропустить текущий шаг (для необязательных полосок CP/цель)
    ESC    — выйти досрочно (уже собранное сохранится)

Совет: перед калибровкой сделай HP/MP/CP ПОЛНЫМИ (полоски залиты целиком) —
так правый край и цвет заполнения определятся корректно. Для полоски цели
выбери какого-нибудь моба, чтобы она была видна.
"""
import os
import time

import mss
import numpy as np
import keyboard
import pydirectinput

RESULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "calibration_result.txt")


def sample_pixel(sct, x, y):
    """Средний цвет 3x3 вокруг (x,y) в BGR — устойчивее одиночного пикселя."""
    shot = sct.grab({"left": x - 1, "top": y - 1, "width": 3, "height": 3})
    arr = np.asarray(shot)[:, :, :3].reshape(-1, 3)
    b, g, r = arr.mean(axis=0).round().astype(int)
    return int(b), int(g), int(r)


def wait_release(*keys):
    """Антидребезг: дождаться отпускания клавиш."""
    while any(keyboard.is_pressed(k) for k in keys):
        time.sleep(0.01)


def capture_point(sct, prompt):
    """
    Ждать действия пользователя. Вернуть:
      ("point", x, y, (b,g,r))  — записал точку
      ("skip", ...)             — пропустил шаг
      ("quit", ...)             — досрочный выход
    """
    print(prompt)
    print("   [ПРОБЕЛ] записать   [S] пропустить   [ESC] выход")
    while True:
        if keyboard.is_pressed("esc"):
            wait_release("esc")
            return ("quit", None, None, None)
        if keyboard.is_pressed("s"):
            wait_release("s")
            print("   -> пропущено\n")
            return ("skip", None, None, None)
        if keyboard.is_pressed("space"):
            x, y = pydirectinput.position()
            color = sample_pixel(sct, x, y)
            wait_release("space")
            print(f"   -> ({x}, {y})  color(BGR)={color}\n")
            return ("point", x, y, color)
        time.sleep(0.01)


# Полоски, которые калибруем. required=False -> можно пропустить (S).
BARS = [
    ("hp", "полоска HP (красная)", True),
    ("mp", "полоска MP (синяя)", True),
    ("cp", "полоска CP (жёлтая, необязательно)", False),
]


def calibrate_bar(sct, name, human, required):
    print(f"\n--- {human} ---")
    # левый край (по центру полоски по высоте) — отсюда берём y и цвет заполнения
    res = capture_point(
        sct, f"1) Наведи на ЛЕВЫЙ край {human}, по центру по высоте.")
    if res[0] == "quit":
        return "quit", None
    if res[0] == "skip":
        return ("skip", None) if not required else calibrate_bar(sct, name, human, required)
    _, x1, y, color = res

    res2 = capture_point(
        sct, f"2) Наведи на ПРАВЫЙ край {human} (максимум заполнения).")
    if res2[0] == "quit":
        return "quit", None
    if res2[0] == "skip":
        return ("skip", None) if not required else calibrate_bar(sct, name, human, required)
    _, x2, _, _ = res2

    if x2 < x1:
        x1, x2 = x2, x1
    cfg = {"x1": x1, "x2": x2, "y": y, "color": color, "tol": 60}
    print(f"   => {name}: {cfg}")
    return "ok", cfg


def main():
    print("=" * 64)
    print("ПОШАГОВАЯ КАЛИБРОВКА l2bot")
    print("Игра должна быть в оконном режиме и видна. HP/MP/CP — полные.")
    print("=" * 64)

    results = {}
    with mss.mss() as sct:
        # полоски персонажа
        for name, human, required in BARS:
            status, cfg = calibrate_bar(sct, name, human, required)
            if status == "quit":
                break
            if status == "ok":
                results[name] = cfg

        # полоска HP цели (нужен выбранный моб)
        else:
            print("\n--- полоска HP ЦЕЛИ (сначала выбери моба, чтобы она была видна) ---")
            r1 = capture_point(sct, "1) Наведи на ЛЕВЫЙ край полоски HP цели.")
            if r1[0] == "point":
                _, tx1, ty, tcolor = r1
                r2 = capture_point(sct, "2) Наведи на ПРАВЫЙ край полоски HP цели.")
                if r2[0] == "point":
                    tx2 = r2[1]
                    if tx2 < tx1:
                        tx1, tx2 = tx2, tx1
                    results["target"] = {"x1": tx1, "x2": tx2, "y": ty,
                                         "color": tcolor, "tol": 60}

    # --- вывод результата ---
    lines = []
    lines.append("# === РЕЗУЛЬТАТ КАЛИБРОВКИ — впиши в config.py ===\n")
    if any(k in results for k in ("hp", "mp", "cp")):
        lines.append("BARS = {")
        for name in ("hp", "mp", "cp"):
            if name in results:
                c = results[name]
                lines.append(f'    "{name}": {{"x1": {c["x1"]}, "x2": {c["x2"]}, '
                             f'"y": {c["y"]}, "color": {tuple(c["color"])}, "tol": {c["tol"]}}},')
        lines.append("}\n")
    if "target" in results:
        c = results["target"]
        lines.append(f'TARGET_BAR = {{"x1": {c["x1"]}, "x2": {c["x2"]}, '
                     f'"y": {c["y"]}, "color": {tuple(c["color"])}, "tol": {c["tol"]}}}\n')

    block = "\n".join(lines) if lines else "# ничего не собрано"
    print("\n" + "=" * 64)
    print(block)
    print("=" * 64)

    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        f.write(block + "\n")
    print(f"\nСохранено в: {RESULT_FILE}")
    print("Скажи ассистенту 'готово' — он впишет значения в config.py.")


if __name__ == "__main__":
    main()
