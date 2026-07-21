"""
Точка входа. Главный цикл: захват -> анализ -> действие.

Горячие клавиши:
  F11 — пауза/продолжить
  F12 — аварийный стоп
Дополнительно активен failsafe pydirectinput: резко увести мышь в левый
верхний угол экрана — процесс прервётся исключением.

Запуск (на Windows):
    python main.py
"""
import time

import keyboard

import config
from capture.screen import ScreenCapture
from logic.fsm import BotFSM


class BotRunner:
    def __init__(self):
        self.paused = False
        self.running = True
        self.fsm = BotFSM()

    def _bind_hotkeys(self):
        keyboard.add_hotkey(config.HOTKEY_PAUSE, self._toggle_pause)
        keyboard.add_hotkey(config.HOTKEY_STOP, self._stop)

    def _toggle_pause(self):
        self.paused = not self.paused
        print(f"[HOTKEY] {'ПАУЗА' if self.paused else 'ПРОДОЛЖАЕМ'}")

    def _stop(self):
        self.running = False
        print("[HOTKEY] СТОП")

    def run(self):
        self._bind_hotkeys()
        print("Бот запущен. F11 — пауза, F12 — стоп.")
        print("Даю 3 секунды переключиться в окно игры...")
        time.sleep(3)

        with ScreenCapture() as cap:
            while self.running:
                if self.paused:
                    time.sleep(0.2)
                    continue

                now = time.time()
                frame = cap.grab()
                status = self.fsm.tick(frame, now)

                if config.DEBUG_OVERLAY:
                    print(
                        f"[{status['state']:<6}] "
                        f"HP={status['hp']:>5}%  MP={status['mp']:>5}%  "
                        f"target={'Y' if status['target'] else '-'} "
                        f"({status['target_hp']}%)"
                    )

                time.sleep(config.LOOP_DELAY)

        print("Бот остановлен.")


if __name__ == "__main__":
    try:
        BotRunner().run()
    except KeyboardInterrupt:
        print("\nПрервано пользователем (Ctrl+C).")
