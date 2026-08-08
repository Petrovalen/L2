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
from logic.humanize import BreakScheduler
from logic import settings
from control import input_ctl as ctl
from vision import bars


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
        settings.apply_to_config()   # применить сохранённые настройки панели
        self._bind_hotkeys()
        print("Бот запущен. F11 — пауза, F12 — стоп.")
        print("Даю 3 секунды переключиться в окно игры...")
        time.sleep(3)

        breaks = BreakScheduler(time.monotonic())
        with ScreenCapture() as cap:
            while self.running:
                if self.paused:
                    time.sleep(0.2)
                    continue

                mono = time.monotonic()
                now = time.time()
                frame = cap.grab()

                # активный перерыв: ждём, но следим за HP (урон -> выходим)
                if breaks.is_active():
                    hp = bars.read_self_bars(frame).get("hp", 100.0) or 100.0
                    if hp < config.HP_HEAL_THRESHOLD or breaks.remaining(mono) <= 0:
                        breaks.end(mono)
                        print("[BREAK] перерыв окончен, продолжаем")
                    else:
                        time.sleep(0.4)
                        continue

                status = self.fsm.tick(frame, now)

                if config.DEBUG_OVERLAY:
                    print(
                        f"[{status['state']:<6}] "
                        f"HP={status['hp']:>5}%  MP={status['mp']:>5}%  "
                        f"target={'Y' if status['target'] else '-'} "
                        f"({status['target_hp']}%)"
                    )

                # начать перерыв, если пора и сейчас безопасно (в АССИСТЕ — не делаем:
                # бот идёт за таргетом игрока и не должен отставать)
                if (config.BREAKS_ENABLED and not config.ASSIST_MODE and breaks.due(mono)
                        and status["state"] == "SEARCH" and not status["target"]
                        and (status["hp"] or 0) >= config.BREAK_SAFE_HP):
                    dur = breaks.start(mono)
                    print(f"[BREAK] перерыв ~{int(dur)} c (человеческая пауза)")
                    continue

                ctl.sleep(config.LOOP_DELAY)   # джиттер интервала цикла

        print("Бот остановлен.")


if __name__ == "__main__":
    try:
        BotRunner().run()
    except KeyboardInterrupt:
        print("\nПрервано пользователем (Ctrl+C).")
