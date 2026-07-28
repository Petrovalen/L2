"""
Графический интерфейс l2bot (Tkinter — встроен в Python, доп. зависимостей нет).

Окно с кнопками Старт / Пауза / Стоп, живыми полосками HP/MP/CP, состоянием
FSM, инфо по цели и лентой всех действий бота.

Запуск (от администратора, если клиент L2 запущен от администратора):
    python gui.py

Горячие клавиши работают, даже когда активна игра (а не окно бота):
    F11 — пауза/продолжить
    F12 — стоп
Также активен failsafe pydirectinput — резкий увод мыши в левый верхний угол
экрана прерывает бота.
"""
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

import keyboard

import config
from capture.screen import ScreenCapture
from logic.fsm import BotFSM
from logic.humanize import BreakScheduler
from logic import mob_list
from control import input_ctl as ctl
from vision import bars, ocr

# Человекочитаемые подписи действий для ленты.
ACTION_LABELS = {
    "target_nearest": "выбор цели",
    "target_macro": "цель по имени (F6)",
    "attack": "атака",
    "assist_skill": "доп. скилл",
    "heal_potion": "хилка",
    "mana_potion": "мана",
    "pickup": "подобрать лут",
}

WARMUP_SEC = 3          # пауза перед стартом — успеть переключиться в игру
MAX_LOG_LINES = 500     # ограничение размера ленты


class BotWorker(threading.Thread):
    """Крутит главный цикл бота в фоне и шлёт события в очередь для GUI."""

    def __init__(self, event_q):
        super().__init__(daemon=True)
        self.q = event_q
        self._pause = threading.Event()
        self._stop = threading.Event()
        self.fsm = BotFSM()
        self._last_state = None

    # --- управление извне (потокобезопасно через Event) ---
    def toggle_pause(self):
        if self._pause.is_set():
            self._pause.clear()
            self.q.put(("paused", False))
        else:
            self._pause.set()
            self.q.put(("paused", True))

    def stop(self):
        self._stop.set()

    # --- колбэк действий из input_ctl ---
    def _on_action(self, name):
        label = ACTION_LABELS.get(name, name)
        key = config.KEYS.get(name)
        self.q.put(("action", f"{label} (клавиша '{key}')"))

    # --- основной цикл ---
    def run(self):
        ctl.on_action = self._on_action
        self.q.put(("log", f"Старт через {WARMUP_SEC} c — переключитесь в окно игры..."))
        end = time.monotonic() + WARMUP_SEC
        while time.monotonic() < end and not self._stop.is_set():
            time.sleep(0.05)
        if self._stop.is_set():
            self._finish()
            return

        self.q.put(("log", "Бот работает."))
        breaks = BreakScheduler(time.monotonic())
        if config.BREAKS_ENABLED:
            self.q.put(("log", f"Следующий перерыв через ~{int(breaks.until_due(time.monotonic()))} c."))
        deferred_logged = False
        try:
            with ScreenCapture() as cap:
                while not self._stop.is_set():
                    if self._pause.is_set():
                        time.sleep(0.15)
                        continue
                    mono = time.monotonic()
                    now = time.time()
                    frame = cap.grab()

                    # активный перерыв: ждём, но следим за HP (урон -> выходим)
                    if breaks.is_active():
                        hp = bars.read_self_bars(frame).get("hp", 100.0) or 100.0
                        if hp < config.HP_HEAL_THRESHOLD or breaks.remaining(mono) <= 0:
                            breaks.end(mono)
                            deferred_logged = False
                            self.q.put(("resumed", None))
                            self.q.put(("log", f"— перерыв окончен. Следующий через ~{int(breaks.until_due(mono))} c —"))
                        else:
                            self.q.put(("break", int(breaks.remaining(mono))))
                            time.sleep(0.4)
                            continue

                    status = self.fsm.tick(frame, now)
                    self.q.put(("status", status))
                    if status["state"] != self._last_state:
                        self.q.put(("log", f"Состояние → {status['state']}"))
                        self._last_state = status["state"]

                    # перерыв: стартуем только в безопасный момент
                    if config.BREAKS_ENABLED and breaks.due(mono):
                        safe = (status["state"] == "SEARCH" and not status["target"]
                                and (status["hp"] or 0) >= config.BREAK_SAFE_HP)
                        if safe:
                            dur = breaks.start(mono)
                            deferred_logged = False
                            self.q.put(("log", f"— перерыв ~{int(dur)} c (человеческая пауза) —"))
                            continue
                        elif not deferred_logged:
                            self.q.put(("log", f"Перерыв назначен — жду безопасного момента "
                                               f"(нет цели, HP>={config.BREAK_SAFE_HP}%)."))
                            deferred_logged = True

                    ctl.sleep(config.LOOP_DELAY)   # джиттер интервала цикла
        except Exception as e:  # failsafe pydirectinput и прочее
            self.q.put(("log", f"ОСТАНОВ: {type(e).__name__}: {e}"))
        finally:
            self._finish()

    def _finish(self):
        ctl.on_action = None
        self.q.put(("stopped", None))


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.worker = None
        self._hotkeys = []

        root.title("l2bot — панель управления")
        root.geometry("460x680")
        root.minsize(420, 600)

        # --- баннер статуса ---
        self.status_var = tk.StringVar(value="ОСТАНОВЛЕН")
        self.banner = tk.Label(root, textvariable=self.status_var,
                               font=("Segoe UI", 16, "bold"),
                               fg="white", bg="#555", pady=10)
        self.banner.pack(fill="x")

        # --- полоски HP/MP/CP ---
        bars = ttk.LabelFrame(root, text="Состояние персонажа")
        bars.pack(fill="x", padx=10, pady=8)
        self.hp = self._make_bar(bars, "HP")
        self.mp = self._make_bar(bars, "MP")
        self.cp = self._make_bar(bars, "CP")

        # --- цель ---
        self.target_var = tk.StringVar(value="Цель: —")
        tk.Label(root, textvariable=self.target_var, font=("Segoe UI", 11),
                 anchor="w").pack(fill="x", padx=12)

        # --- кнопки ---
        btns = tk.Frame(root)
        btns.pack(fill="x", padx=10, pady=8)
        self.start_btn = tk.Button(btns, text="▶ Старт", width=10,
                                   command=self.start, bg="#2e7d32", fg="white",
                                   font=("Segoe UI", 10, "bold"))
        self.pause_btn = tk.Button(btns, text="⏸ Пауза", width=10,
                                   command=self.pause, state="disabled")
        self.stop_btn = tk.Button(btns, text="⏹ Стоп", width=10,
                                  command=self.stop, state="disabled",
                                  bg="#c62828", fg="white",
                                  font=("Segoe UI", 10, "bold"))
        self.start_btn.pack(side="left", expand=True, fill="x", padx=2)
        self.pause_btn.pack(side="left", expand=True, fill="x", padx=2)
        self.stop_btn.pack(side="left", expand=True, fill="x", padx=2)

        tk.Label(root, text="F11 — пауза · F12 — стоп (работают и поверх игры)",
                 font=("Segoe UI", 8), fg="#666").pack()

        # --- белый список мобов ---
        mobframe = ttk.LabelFrame(root, text="Мои мобы (кого бить)")
        mobframe.pack(fill="x", padx=10, pady=6)
        row = tk.Frame(mobframe)
        row.pack(fill="x", padx=6, pady=4)
        self.add_btn = tk.Button(row, text="＋ Добавить моба (рамкой)",
                                 command=self.add_mob)
        self.add_btn.pack(side="left")
        self.del_btn = tk.Button(row, text="Удалить", command=self.remove_mob)
        self.del_btn.pack(side="left", padx=4)
        self.mob_status = tk.StringVar(value="")
        tk.Label(mobframe, textvariable=self.mob_status, font=("Segoe UI", 8),
                 fg="#1565c0", anchor="w").pack(fill="x", padx=8)
        self.mob_listbox = tk.Listbox(mobframe, height=4, font=("Consolas", 9))
        self.mob_listbox.pack(fill="x", padx=6, pady=4)
        self._refresh_mobs()

        # --- лента действий ---
        logframe = ttk.LabelFrame(root, text="Действия бота")
        logframe.pack(fill="both", expand=True, padx=10, pady=8)
        self.log = tk.Text(logframe, height=10, state="disabled",
                           font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
                           wrap="none")
        scroll = ttk.Scrollbar(logframe, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(80, self._drain)

    def _make_bar(self, parent, name):
        row = tk.Frame(parent)
        row.pack(fill="x", padx=8, pady=3)
        tk.Label(row, text=name, width=3, font=("Segoe UI", 10, "bold")).pack(side="left")
        pb = ttk.Progressbar(row, maximum=100, length=280)
        pb.pack(side="left", fill="x", expand=True, padx=6)
        val = tk.StringVar(value="—")
        tk.Label(row, textvariable=val, width=6, anchor="e").pack(side="left")
        return {"bar": pb, "val": val}

    # --- кнопки ---
    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self._clear_log()
        self.worker = BotWorker(self.q)
        self.worker.start()
        self._register_hotkeys()
        self.status_var.set("ЗАПУСК…")
        self.banner.config(bg="#f9a825")
        self.start_btn.config(state="disabled")
        self.pause_btn.config(state="normal", text="⏸ Пауза")
        self.stop_btn.config(state="normal")

    def pause(self):
        if self.worker and self.worker.is_alive():
            self.worker.toggle_pause()

    def stop(self):
        if self.worker:
            self.worker.stop()
        self._unregister_hotkeys()

    # --- белый список мобов ---
    def _refresh_mobs(self):
        self.mob_listbox.delete(0, "end")
        for n in mob_list.load():
            self.mob_listbox.insert("end", n)

    def add_mob(self):
        """Открыть полупрозрачный оверлей: обведи рамкой ник моба в игре."""
        ov = tk.Toplevel(self.root)
        ov.attributes("-fullscreen", True)
        ov.attributes("-alpha", 0.3)
        ov.attributes("-topmost", True)
        ov.configure(cursor="crosshair")
        canvas = tk.Canvas(ov, highlightthickness=0, bg="gray15")
        canvas.pack(fill="both", expand=True)
        canvas.create_text(ov.winfo_screenwidth() // 2, 28, fill="white",
                           font=("Segoe UI", 14, "bold"),
                           text="Обведи рамкой ник моба   (ЛКМ — растянуть, Esc — отмена)")
        st = {"lx": 0, "ly": 0, "rx": 0, "ry": 0, "rect": None}

        def on_press(e):
            st["lx"], st["ly"] = e.x, e.y          # локальные — для рисунка
            st["rx"], st["ry"] = e.x_root, e.y_root  # экранные — для захвата
            st["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                                 outline="yellow", width=2)

        def on_drag(e):
            if st["rect"] is not None:
                canvas.coords(st["rect"], st["lx"], st["ly"], e.x, e.y)

        def on_release(e):
            left, top = min(st["rx"], e.x_root), min(st["ry"], e.y_root)
            w, h = abs(e.x_root - st["rx"]), abs(e.y_root - st["ry"])
            ov.destroy()
            if w < 5 or h < 5:
                self.mob_status.set("Слишком маленькая рамка — отмена.")
                return
            # даём оверлею исчезнуть с экрана перед захватом
            self.root.after(150, lambda: self._read_boxed_name(left, top, w, h))

        def on_cancel(_):
            ov.destroy()
            self.mob_status.set("Добавление отменено.")

        canvas.bind("<Button-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        ov.bind("<Escape>", on_cancel)
        ov.focus_force()

    def _read_boxed_name(self, left, top, w, h):
        name = ""
        try:
            with ScreenCapture() as cap:
                frame = cap.grab()
            name = ocr.read_name(frame, {"left": left, "top": top,
                                         "width": w, "height": h})
        except Exception as e:
            self._append_log(f"[!] Ошибка чтения имени: {e}")
        if name:
            mob_list.add(name)
            self._refresh_mobs()
            self.mob_status.set(f"Добавлен: {name}")
            self._append_log(f"Добавлен моб: {name}")
        else:
            self.mob_status.set("В рамке не распознан текст — попробуй точнее.")

    def remove_mob(self):
        sel = self.mob_listbox.curselection()
        if not sel:
            self.mob_status.set("Выберите имя в списке для удаления.")
            return
        name = self.mob_listbox.get(sel[0])
        mob_list.remove(name)
        self._refresh_mobs()
        self.mob_status.set(f"Удалён: {name}")

    # --- горячие клавиши ---
    def _register_hotkeys(self):
        self._unregister_hotkeys()
        try:
            self._hotkeys.append(keyboard.add_hotkey(config.HOTKEY_PAUSE, self.pause))
            self._hotkeys.append(keyboard.add_hotkey(config.HOTKEY_STOP, self.stop))
        except Exception as e:
            self._append_log(f"[!] Горячие клавиши недоступны: {e}")

    def _unregister_hotkeys(self):
        for h in self._hotkeys:
            try:
                keyboard.remove_hotkey(h)
            except Exception:
                pass
        self._hotkeys = []

    # --- обработка событий из очереди ---
    def _drain(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                self._handle(kind, data)
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def _handle(self, kind, data):
        if kind == "status":
            self._show_status(data)
        elif kind == "action":
            self._append_log(f"→ {data}")
        elif kind == "log":
            self._append_log(data)
        elif kind == "paused":
            if data:
                self.status_var.set("ПАУЗА")
                self.banner.config(bg="#f9a825")
                self.pause_btn.config(text="▶ Продолжить")
                self._append_log("— пауза —")
            else:
                self.pause_btn.config(text="⏸ Пауза")
                self._append_log("— продолжаем —")
        elif kind == "break":
            self.status_var.set(f"ПЕРЕРЫВ · осталось {data} c")
            self.banner.config(bg="#1565c0")
        elif kind == "resumed":
            pass  # следующий status вернёт баннер в рабочий вид
        elif kind == "stopped":
            self.status_var.set("ОСТАНОВЛЕН")
            self.banner.config(bg="#555")
            self.start_btn.config(state="normal")
            self.pause_btn.config(state="disabled", text="⏸ Пауза")
            self.stop_btn.config(state="disabled")
            self._unregister_hotkeys()
            self._append_log("Бот остановлен.")

    def _show_status(self, s):
        self.status_var.set(f"РАБОТАЕТ · {s['state']}")
        self.banner.config(bg="#2e7d32")
        for key, widget in (("hp", self.hp), ("mp", self.mp), ("cp", self.cp)):
            v = s.get(key)
            if v is None:
                continue
            widget["bar"]["value"] = v
            widget["val"].set(f"{v:.0f}%")
        if s.get("target"):
            self.target_var.set(f"Цель: ЕСТЬ · HP цели {s.get('target_hp', 0):.0f}%")
        else:
            self.target_var.set("Цель: нет")

    # --- лог ---
    def _append_log(self, text):
        ts = time.strftime("%H:%M:%S")
        self.log.config(state="normal")
        self.log.insert("end", f"[{ts}] {text}\n")
        # обрезаем слишком длинную ленту
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.log.see("end")
        self.log.config(state="disabled")

    def _clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def on_close(self):
        if self.worker:
            self.worker.stop()
        self._unregister_hotkeys()
        self.root.after(200, self.root.destroy)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
