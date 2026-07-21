"""
Утилита калибровки. Помогает подобрать координаты полосок/регионов и цвета.

Как пользоваться (на Windows, игра в оконном режиме):
    python tools_calibrate.py

Наведи курсор на нужную точку (например, край полоски HP) и нажми ПРОБЕЛ —
в консоль выведется координата экрана и цвет пикселя в BGR (как в config.py).
Собери так x1/x2/y и color для каждой полоски и впиши в config.BARS.
ESC — выход.
"""
import mss
import numpy as np
import keyboard
import pydirectinput  # только чтобы получить позицию курсора


def pixel_at(sct, x, y):
    shot = sct.grab({"left": x, "top": y, "width": 1, "height": 1})
    bgr = np.asarray(shot)[0, 0, :3]
    return int(bgr[0]), int(bgr[1]), int(bgr[2])  # B, G, R


def main():
    print("Наведи курсор и жми ПРОБЕЛ. ESC — выход.")
    with mss.mss() as sct:
        while True:
            if keyboard.is_pressed("esc"):
                print("Выход.")
                break
            if keyboard.is_pressed("space"):
                x, y = pydirectinput.position()
                b, g, r = pixel_at(sct, x, y)
                print(f"pos=({x}, {y})   color(BGR)=({b}, {g}, {r})   "
                      f"# для config: x/y выше, color=({b}, {g}, {r})")
                # антидребезг: ждём отпускания
                while keyboard.is_pressed("space"):
                    pass


if __name__ == "__main__":
    main()
