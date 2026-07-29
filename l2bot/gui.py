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
import ctypes
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
from logic import settings
from control import input_ctl as ctl
from vision import bars, ocr, targets

# Человекочитаемые подписи действий для ленты.
ACTION_LABELS = {
    "target_nearest": "выбор цели",
    "attack": "атака",
    "heal_potion": "хилка",
    "mana_potion": "мана",
    "pickup": "подобрать лут",
}

# Действия с настраиваемой клавишей и их подписи в редакторе (порядок сохранён).
KEY_ACTIONS = [
    ("target_nearest", "Выбор цели (некст-таргет)"),
    ("attack", "Атака / основной скилл"),
    ("heal_potion", "Хилка (зелье HP)"),
    ("mana_potion", "Мана (зелье MP)"),
    ("pickup", "Подобрать лут"),
]

# Дефолт новой способности при добавлении в редакторе.
DEFAULT_SKILL = {"key": "", "label": "Скилл", "cooldown": 4.0,
                 "target_hp_max": 100, "mp_min": 0, "enabled": True}

WARMUP_SEC = 3          # пауза перед стартом — успеть переключиться в игру
MAX_LOG_LINES = 500     # ограничение размера ленты


class BotWorker(threading.Thread):
    """Крутит главный цикл бота в фоне и шлёт события в очередь для GUI."""

    def __init__(self, event_q, gen=0):
        super().__init__(daemon=True)
        self.q = event_q
        self.gen = gen                 # номер поколения (для отсева старых событий)
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
        ctl.on_event = lambda m: self.q.put(("log", m))
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
        ctl.on_event = None
        self.q.put(("stopped", self.gen))


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.worker = None
        self._worker_gen = 0            # счётчик поколений воркера
        self._running = False
        self._hotkeys = []
        self.debug_overlay = None       # прозрачный оверлей с рамками мобов
        self.debug_canvas = None
        self._overlay_stop = threading.Event()

        settings.apply_to_config()   # применить сохранённые настройки панели

        root.title("l2bot — панель управления")
        # выше, чем раньше: добавились панели «Камера» и «Цифры», иначе лента
        # действий уезжала за нижний край. Окно можно свободно растягивать.
        root.geometry("480x1150")
        root.minsize(440, 640)

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

        # --- калибровка полосок рамкой ---
        calib = ttk.LabelFrame(root, text="Калибровка полосок (обведи рамкой ПОЛНУЮ полоску)")
        calib.pack(fill="x", padx=10, pady=(0, 6))
        crow = tk.Frame(calib)
        crow.pack(fill="x", padx=6, pady=4)
        for label, key, wide in (("HP", "hp", 5), ("MP", "mp", 5),
                                 ("CP", "cp", 5), ("HP цели", "target", 8)):
            tk.Button(crow, text=label, width=wide,
                      command=lambda k=key: self._calibrate_bar(k)).pack(side="left", padx=2)
        tk.Button(crow, text="Имя цели", width=8,
                  command=self.calibrate_target_name).pack(side="left", padx=2)
        self.calib_status = tk.StringVar(value="")
        tk.Label(calib, textvariable=self.calib_status, font=("Segoe UI", 8),
                 fg="#1565c0", anchor="w").pack(fill="x", padx=8)

        # --- режим «цифры» (OCR чисел, вшитых в бар) ---
        dg = ttk.LabelFrame(root, text="Чтение цифрами (OCR): процент = тек/макс × 100")
        dg.pack(fill="x", padx=10, pady=(0, 6))
        self.digit_enabled = {}
        self.digit_max_var = {}
        for label, key in (("HP", "hp"), ("MP", "mp"), ("CP", "cp"), ("HP цели", "target")):
            d = settings.get("bar_%s_digits" % key) or {}
            drow = tk.Frame(dg)
            drow.pack(fill="x", padx=6, pady=1)
            ev = tk.BooleanVar(value=bool(d.get("enabled")))
            self.digit_enabled[key] = ev
            tk.Checkbutton(drow, text=label, width=8, anchor="w", variable=ev,
                           command=lambda k=key: self._toggle_digit(k)).pack(side="left")
            tk.Label(drow, text="Max (если не «тек/макс»):").pack(side="left", padx=(6, 1))
            mv = tk.StringVar(value=str(d.get("max") or ""))
            self.digit_max_var[key] = mv
            e = tk.Entry(drow, textvariable=mv, width=7)
            e.pack(side="left")
            e.bind("<FocusOut>", lambda ev_, k=key: self._save_digit_max(k))
            e.bind("<Return>", lambda ev_, k=key: self._save_digit_max(k))
        tk.Label(dg, text="Числа читаются из области бара — просто откалибруй бар так, "
                          "чтобы в рамку попали цифры. Max пусто = авто из «тек/макс».",
                 font=("Segoe UI", 8), fg="#666", anchor="w").pack(fill="x", padx=8)

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

        hk = tk.Frame(root)
        hk.pack(fill="x", padx=12)
        self.hotkey_lbl = tk.StringVar()
        tk.Label(hk, textvariable=self.hotkey_lbl, font=("Segoe UI", 8),
                 fg="#666", anchor="w").pack(side="left")
        tk.Label(hk, text="Клавиша стоп:", font=("Segoe UI", 8),
                 fg="#666").pack(side="left", padx=(10, 2))
        self.stopkey_cb = ttk.Combobox(
            hk, width=4, state="readonly",
            values=["f12", "f9", "f8", "f7", "f6", "f5", "f4", "f3", "f2", "f1"])
        self.stopkey_cb.set(config.HOTKEY_STOP)
        self.stopkey_cb.pack(side="left")
        self.stopkey_cb.bind("<<ComboboxSelected>>", self._on_stopkey)
        self._update_hotkey_label()

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
        self.zone_btn = tk.Button(row, text="Зона поиска", command=self.set_search_region)
        self.zone_btn.pack(side="left", padx=4)
        self.char_btn = tk.Button(row, text="Точка персонажа",
                                  command=self.set_character_anchor)
        self.char_btn.pack(side="left", padx=4)
        self.mob_status = tk.StringVar(value="")
        tk.Label(mobframe, textvariable=self.mob_status, font=("Segoe UI", 8),
                 fg="#1565c0", anchor="w").pack(fill="x", padx=8)
        self.mob_listbox = tk.Listbox(mobframe, height=4, font=("Consolas", 9))
        self.mob_listbox.pack(fill="x", padx=6, pady=4)
        self._refresh_mobs()

        # --- настройки визуального поиска ---
        setframe = ttk.LabelFrame(root, text="Настройки поиска мобов")
        setframe.pack(fill="x", padx=10, pady=6)
        self.vision_var = tk.BooleanVar(value=config.VISION_TARGETING)
        tk.Checkbutton(setframe, text="Визуальный поиск ников (OCR)",
                       variable=self.vision_var,
                       command=self._on_vision_toggle).pack(anchor="w", padx=6)
        self.namefilter_var = tk.BooleanVar(value=config.TARGET_NAME_FILTER)
        tk.Checkbutton(setframe, text="Проверять имя цели (некст-таргет)",
                       variable=self.namefilter_var,
                       command=self._on_namefilter_toggle).pack(anchor="w", padx=6)
        self.debug_var = tk.BooleanVar(value=False)
        tk.Checkbutton(setframe, text="Показывать рамки мобов (отладка)",
                       variable=self.debug_var,
                       command=self._toggle_debug_overlay).pack(anchor="w", padx=6)
        r1 = tk.Frame(setframe)
        r1.pack(fill="x", padx=6)
        tk.Label(r1, text="Смещение клика, px", width=18, anchor="w").pack(side="left")
        self.dy_scale = tk.Scale(r1, from_=0, to=80, orient="horizontal",
                                 command=self._on_click_dy)
        self.dy_scale.set(config.NAME_CLICK_DY)
        self.dy_scale.pack(side="left", fill="x", expand=True)
        self.dy_scale.bind("<ButtonRelease-1>", lambda e:
                           settings.set("name_click_dy", int(self.dy_scale.get())))
        r2 = tk.Frame(setframe)
        r2.pack(fill="x", padx=6, pady=(0, 4))
        tk.Label(r2, text="Интервал поиска, с", width=18, anchor="w").pack(side="left")
        self.iv_scale = tk.Scale(r2, from_=0.5, to=5.0, resolution=0.5,
                                 orient="horizontal", command=self._on_interval)
        self.iv_scale.set(config.VISION_INTERVAL)
        self.iv_scale.pack(side="left", fill="x", expand=True)
        self.iv_scale.bind("<ButtonRelease-1>", lambda e:
                           settings.set("vision_interval", float(self.iv_scale.get())))

        # --- камера (активный поиск) ---
        camframe = ttk.LabelFrame(root, text="Камера (активный поиск)")
        camframe.pack(fill="x", padx=10, pady=6)
        self.camera_var = tk.BooleanVar(value=config.CAMERA_SEARCH)
        tk.Checkbutton(camframe, text="Крутить камеру, когда цель не найдена",
                       variable=self.camera_var,
                       command=self._on_camera_toggle).pack(anchor="w", padx=6)
        cr = tk.Frame(camframe)
        cr.pack(fill="x", padx=6, pady=(0, 4))
        tk.Label(cr, text="Скорость поворота", width=18, anchor="w").pack(side="left")
        self.cam_scale = tk.Scale(cr, from_=15, to=120, resolution=5,
                                  orient="horizontal", command=self._on_camera_speed)
        self.cam_scale.set(config.CAMERA_DRAG_DISTANCE)
        self.cam_scale.pack(side="left", fill="x", expand=True)
        self.cam_scale.bind("<ButtonRelease-1>", lambda e:
                            settings.set("camera_drag_distance", int(self.cam_scale.get())))
        cr2 = tk.Frame(camframe)
        cr2.pack(fill="x", padx=6, pady=(0, 4))
        tk.Label(cr2, text="Плавность, с", width=18, anchor="w").pack(side="left")
        # Длительность одного свайпа: больше = дольше и плавнее, меньше = резче.
        self.camdur_scale = tk.Scale(cr2, from_=0.10, to=0.80, resolution=0.05,
                                     orient="horizontal", command=self._on_camera_duration)
        self.camdur_scale.set(config.CAMERA_STEP_DURATION)
        self.camdur_scale.pack(side="left", fill="x", expand=True)
        self.camdur_scale.bind("<ButtonRelease-1>", lambda e:
                               settings.set("camera_step_duration", float(self.camdur_scale.get())))

        # --- клавиши и способности ---
        ctrlframe = ttk.LabelFrame(root, text="Клавиши и способности")
        ctrlframe.pack(fill="x", padx=10, pady=6)
        tk.Button(ctrlframe, text="⚙ Настроить клавиши и способности",
                  command=self.open_controls_dialog).pack(fill="x", padx=6, pady=6)

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
        self._register_hotkeys()        # F11/F12/F10 — один раз на всё время работы
        self.root.after(50, self._drain)

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
        if self._running:
            self._append_log("[Старт] бот ещё работает/останавливается — подождите.")
            return
        self._clear_log()
        self._running = True
        self._worker_gen += 1
        self.worker = BotWorker(self.q, self._worker_gen)
        self.worker.start()
        self.status_var.set("ЗАПУСК…")
        self.banner.config(bg="#f9a825")
        self.start_btn.config(state="disabled")
        self.pause_btn.config(state="normal", text="⏸ Пауза")
        self.stop_btn.config(state="normal")

    def pause(self):
        if self.worker and self.worker.is_alive():
            self.worker.toggle_pause()

    def stop(self):
        # Может прийти из потока keyboard (F12). Хоткеи НЕ трогаем — они живут
        # всё время (регистрируются один раз при старте панели).
        self._append_log("[Стоп] нажат.")
        if self.worker:
            self.worker.stop()
        # сторож: если воркер не завершится за 5с — принудительно разблокируем Старт
        self.root.after(5000, self._stop_watchdog)

    def _stop_watchdog(self):
        if self._running and self.worker is not None and self.worker.is_alive():
            self._append_log("[!] Воркер не остановился за 5с — сбрасываю панель "
                             "(поток брошен, завершится сам при разблокировке).")
            self.worker = None
            self._set_stopped_ui()

    def _set_stopped_ui(self):
        self._running = False
        self.status_var.set("ОСТАНОВЛЕН")
        self.banner.config(bg="#555")
        self.start_btn.config(state="normal")
        self.pause_btn.config(state="disabled", text="⏸ Пауза")
        self.stop_btn.config(state="disabled")

    # --- белый список мобов ---
    def _refresh_mobs(self):
        self.mob_listbox.delete(0, "end")
        for n in mob_list.load():
            self.mob_listbox.insert("end", n)

    def _select_region(self, hint, on_done):
        """Полупрозрачный оверлей: пользователь обводит рамку. on_done(left,top,w,h)."""
        ov = tk.Toplevel(self.root)
        ov.attributes("-fullscreen", True)
        ov.attributes("-alpha", 0.3)
        ov.attributes("-topmost", True)
        ov.configure(cursor="crosshair")
        canvas = tk.Canvas(ov, highlightthickness=0, bg="gray15")
        canvas.pack(fill="both", expand=True)
        canvas.create_text(ov.winfo_screenwidth() // 2, 28, fill="white",
                           font=("Segoe UI", 14, "bold"), text=hint)
        st = {"lx": 0, "ly": 0, "rx": 0, "ry": 0, "rect": None}

        def on_press(e):
            st["lx"], st["ly"] = e.x, e.y            # локальные — для рисунка
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
            self.root.after(150, lambda: on_done(left, top, w, h))

        def on_cancel(_):
            ov.destroy()
            self.mob_status.set("Отменено.")

        canvas.bind("<Button-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        ov.bind("<Escape>", on_cancel)
        ov.focus_force()

    def add_mob(self):
        self._select_region(
            "Обведи рамкой ник моба   (ЛКМ — растянуть, Esc — отмена)",
            self._read_boxed_name)

    def set_search_region(self):
        self._select_region(
            "Обведи ЗОНУ ПОИСКА мобов: центр экрана, без интерфейса   (Esc — отмена)",
            self._save_search_region)

    def _save_search_region(self, left, top, w, h):
        settings.set("search_region",
                     {"left": left, "top": top, "width": w, "height": h})
        self.mob_status.set(f"Зона поиска задана: {w}x{h} в ({left},{top})")
        self._append_log(f"Зона поиска мобов: {w}x{h} @ ({left},{top})")

    def set_character_anchor(self):
        self._select_region(
            "Обведи небольшую область НА ПЕРСОНАЖЕ (точка отсчёта расстояния "
            "до мобов)   Esc — отмена",
            self._save_character_anchor)

    def _save_character_anchor(self, left, top, w, h):
        settings.set("character_anchor",
                     {"left": left, "top": top, "width": w, "height": h})
        cx, cy = left + w // 2, top + h // 2
        self.mob_status.set(f"Точка персонажа задана: центр ({cx},{cy})")
        self._append_log(f"Точка персонажа (отсчёт ближайшего моба): ({cx},{cy})")

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
            # снять шаблон ника (для грасс-иммунного поиска по шаблону)
            tpl_ok = False
            try:
                tpl_ok = targets.save_name_template(
                    frame, {"left": left, "top": top, "width": w, "height": h}, name)
            except Exception as e:
                self._append_log(f"[!] шаблон не сохранён: {e}")
            self._refresh_mobs()
            suffix = " (+шаблон)" if tpl_ok else " (шаблон не снят — обведи плотнее)"
            self.mob_status.set(f"Добавлен: {name}{suffix}")
            self._append_log(f"Добавлен моб: {name}{suffix}")
        else:
            self.mob_status.set("В рамке не распознан текст — попробуй точнее.")

    def remove_mob(self):
        sel = self.mob_listbox.curselection()
        if not sel:
            self.mob_status.set("Выберите имя в списке для удаления.")
            return
        name = self.mob_listbox.get(sel[0])
        mob_list.remove(name)
        targets.delete_template(name)     # убрать и снимок ника
        self._refresh_mobs()
        self.mob_status.set(f"Удалён: {name}")

    # --- настройки визуального поиска (применяются к боту сразу) ---
    def _on_vision_toggle(self):
        v = bool(self.vision_var.get())
        config.VISION_TARGETING = v
        settings.set("vision_targeting", v)
        self.mob_status.set(f"Визуальный поиск: {'включён' if v else 'выключен'}")

    def _on_click_dy(self, val):
        config.NAME_CLICK_DY = int(float(val))   # применяем сразу; сохраняем при отпускании

    def _on_interval(self, val):
        config.VISION_INTERVAL = float(val)

    def _on_namefilter_toggle(self):
        v = bool(self.namefilter_var.get())
        config.TARGET_NAME_FILTER = v
        settings.set("target_name_filter", v)
        self.mob_status.set(f"Проверка имени цели: {'включена' if v else 'выключена'}")

    def _on_camera_toggle(self):
        v = bool(self.camera_var.get())
        config.CAMERA_SEARCH = v
        settings.set("camera_search", v)
        self.mob_status.set(f"Активный поиск камерой: {'включён' if v else 'выключен'}")

    def _on_camera_speed(self, val):
        config.CAMERA_DRAG_DISTANCE = int(float(val))   # применяем сразу; сохраняем при отпускании

    def _on_camera_duration(self, val):
        config.CAMERA_STEP_DURATION = float(val)        # применяем сразу; сохраняем при отпускании

    # --- редактор клавиш и способностей -----------------------------------
    def open_controls_dialog(self):
        """Окно настройки клавиш действий и списка способностей (с условиями)."""
        if getattr(self, "_ctrl_win", None) is not None:
            try:
                self._ctrl_win.deiconify()
                self._ctrl_win.lift()
                self._ctrl_win.focus_force()
                return
            except tk.TclError:
                self._ctrl_win = None

        win = tk.Toplevel(self.root)
        self._ctrl_win = win
        win.title("Клавиши и способности")
        win.geometry("600x640")
        win.transient(self.root)

        def on_close():
            self._ctrl_win = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", on_close)

        # --- клавиши действий ---
        kf = ttk.LabelFrame(win, text="Клавиши действий (имя клавиши: 1, 2, f1, e…)")
        kf.pack(fill="x", padx=10, pady=(10, 6))
        self._key_vars = {}
        for action, label in KEY_ACTIONS:
            row = tk.Frame(kf)
            row.pack(fill="x", padx=8, pady=2)
            tk.Label(row, text=label, width=26, anchor="w").pack(side="left")
            var = tk.StringVar(value=config.KEYS.get(action) or "")
            tk.Entry(row, textvariable=var, width=8).pack(side="left")
            self._key_vars[action] = var
        tk.Label(kf, text="Пусто = действие отключено.", font=("Segoe UI", 8),
                 fg="#666", anchor="w").pack(fill="x", padx=10, pady=(0, 4))

        # --- способности ---
        sf = ttk.LabelFrame(win, text="Способности в бою (кастуются при условиях)")
        sf.pack(fill="both", expand=True, padx=10, pady=6)
        hint = ("HP цели ≤ — кастовать, пока HP цели не выше этого % (100 = всегда).\n"
                "MP ≥ — кастовать, только если своя MP не ниже этого % (0 = без условия).\n"
                "КД — не чаще раза в столько секунд.")
        tk.Label(sf, text=hint, font=("Segoe UI", 8), fg="#1565c0",
                 justify="left", anchor="w").pack(fill="x", padx=8, pady=(4, 2))

        head = tk.Frame(sf)
        head.pack(fill="x", padx=8)
        for text, w in (("вкл", 4), ("Клавиша", 9), ("HP цели ≤", 10),
                        ("MP ≥", 7), ("КД, с", 7), ("", 8)):
            tk.Label(head, text=text, width=w, anchor="w",
                     font=("Segoe UI", 8, "bold")).pack(side="left", padx=2)

        self._skills_box = tk.Frame(sf)
        self._skills_box.pack(fill="both", expand=True, padx=4)

        # текущие способности -> редактируемые строки (глубокая копия значений)
        self._skill_vars = []
        for sk in config.SKILLS:
            self._skill_vars.append(self._skill_to_vars({**DEFAULT_SKILL, **sk}))
        self._render_skill_rows()

        tk.Button(sf, text="＋ Добавить способность",
                  command=self._add_skill_row).pack(anchor="w", padx=8, pady=4)

        # --- статус + кнопки ---
        self._ctrl_status = tk.StringVar(value="")
        tk.Label(win, textvariable=self._ctrl_status, font=("Segoe UI", 8),
                 fg="#1565c0", anchor="w").pack(fill="x", padx=12)
        btns = tk.Frame(win)
        btns.pack(fill="x", padx=10, pady=8)
        tk.Button(btns, text="💾 Сохранить", command=self._save_controls,
                  bg="#2e7d32", fg="white", font=("Segoe UI", 10, "bold")).pack(
            side="left", expand=True, fill="x", padx=2)
        tk.Button(btns, text="Закрыть", command=on_close).pack(
            side="left", expand=True, fill="x", padx=2)

    def _skill_to_vars(self, sk):
        return {
            "enabled": tk.BooleanVar(value=bool(sk.get("enabled", True))),
            "key": tk.StringVar(value=str(sk.get("key") or "")),
            "target_hp_max": tk.IntVar(value=int(sk.get("target_hp_max", 100))),
            "mp_min": tk.IntVar(value=int(sk.get("mp_min", 0))),
            "cooldown": tk.DoubleVar(value=float(sk.get("cooldown", 4.0))),
        }

    def _add_skill_row(self):
        self._skill_vars.append(self._skill_to_vars(DEFAULT_SKILL))
        self._render_skill_rows()

    def _del_skill_row(self, idx):
        if 0 <= idx < len(self._skill_vars):
            self._skill_vars.pop(idx)
            self._render_skill_rows()

    def _render_skill_rows(self):
        for w in self._skills_box.winfo_children():
            w.destroy()
        for idx, v in enumerate(self._skill_vars):
            row = tk.Frame(self._skills_box)
            row.pack(fill="x", pady=1)
            tk.Checkbutton(row, variable=v["enabled"], width=2).pack(side="left", padx=2)
            tk.Entry(row, textvariable=v["key"], width=9).pack(side="left", padx=2)
            tk.Spinbox(row, from_=0, to=100, textvariable=v["target_hp_max"],
                       width=8).pack(side="left", padx=2)
            tk.Spinbox(row, from_=0, to=100, textvariable=v["mp_min"],
                       width=6).pack(side="left", padx=2)
            tk.Spinbox(row, from_=0.0, to=600.0, increment=0.5,
                       textvariable=v["cooldown"], width=6).pack(side="left", padx=2)
            tk.Button(row, text="Удалить",
                      command=lambda i=idx: self._del_skill_row(i)).pack(side="left", padx=2)

    def _save_controls(self):
        # клавиши
        keys = {}
        for action, _ in KEY_ACTIONS:
            keys[action] = self._key_vars[action].get().strip() or None
        # способности
        skills = []
        for i, v in enumerate(self._skill_vars, 1):
            try:
                thp = max(0, min(100, int(v["target_hp_max"].get())))
                mp = max(0, min(100, int(v["mp_min"].get())))
                cd = max(0.0, float(v["cooldown"].get()))
            except (tk.TclError, ValueError):
                self._ctrl_status.set("Проверь числовые поля способностей.")
                return
            skills.append({
                "key": v["key"].get().strip(),
                "label": f"Скилл {i}",
                "cooldown": cd,
                "target_hp_max": thp,
                "mp_min": mp,
                "enabled": bool(v["enabled"].get()),
            })
        settings.set("keys", keys)
        settings.set("skills", skills)
        settings.apply_to_config()   # применить сразу (в т.ч. на работающем боте)
        self._ctrl_status.set(f"Сохранено: {len([s for s in skills if s['key']])} "
                              f"способностей, клавиши обновлены.")
        self._append_log("Клавиши и способности обновлены.")

    # --- отладочный оверлей с рамками мобов ---
    def _toggle_debug_overlay(self):
        if self.debug_var.get():
            self._create_debug_overlay()
        else:
            self._destroy_debug_overlay()

    def _create_debug_overlay(self):
        if self.debug_overlay is not None:
            return
        ov = tk.Toplevel(self.root)
        ov.attributes("-fullscreen", True)
        ov.attributes("-topmost", True)
        ov.configure(bg="black")
        try:
            ov.attributes("-transparentcolor", "black")   # чёрный фон -> прозрачный
        except tk.TclError:
            pass
        cv = tk.Canvas(ov, bg="black", highlightthickness=0)
        cv.pack(fill="both", expand=True)
        # КРИТИЧНО: сделать окно клик-сквозным, иначе оно перехватит все клики
        # (и панель станет некликабельной). При неудаче — не показываем оверлей.
        try:
            self._make_clickthrough(ov)
        except Exception as e:
            ov.destroy()
            self.debug_var.set(False)
            self._append_log(f"[!] Клик-сквозь не удался ({e}). Оверлей отключён, "
                             f"чтобы не блокировать клики.")
            return
        self.debug_overlay = ov
        self.debug_canvas = cv
        self._overlay_stop.clear()
        threading.Thread(target=self._overlay_fast_loop, daemon=True).start()
        self._append_log("Оверлей рамок мобов включён (F10 — аварийно убрать).")

    def _make_clickthrough(self, win):
        win.update_idletasks()
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(win.winfo_id())
        if not hwnd:
            raise OSError("не получить hwnd оверлея")
        style = user32.GetWindowLongW(hwnd, -20)   # GWL_EXSTYLE
        # WS_EX_LAYERED (0x80000) | WS_EX_TRANSPARENT (0x20)
        user32.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20)

    def _overlay_panic(self):
        # хоткей F10 срабатывает из потока keyboard -> выполняем на главном потоке
        self.root.after(0, lambda: (self.debug_var.set(False),
                                    self._destroy_debug_overlay()))

    def _destroy_debug_overlay(self):
        self._overlay_stop.set()
        if self.debug_overlay is not None:
            try:
                self.debug_overlay.destroy()
            except Exception:
                pass
        self.debug_overlay = None
        self.debug_canvas = None

    def _overlay_fast_loop(self):
        """
        Быстро (~15/сек): захват зоны -> поиск мобов ПО ШАБЛОНАМ (тот же код, что
        у бота). Сиреневый — ближайший (его и выберет бот), зелёный — прочие
        найденные мобы из списка. Показывает ровно то, что видит бот.
        """
        import mss
        import numpy as np
        sct = mss.mss()
        try:
            while not self._overlay_stop.is_set():
                region = settings.get("search_region")
                names = mob_list.load()
                if region and names:
                    try:
                        mon = {"left": region["left"], "top": region["top"],
                               "width": region["width"], "height": region["height"]}
                        crop = np.asarray(sct.grab(mon))[:, :, :3]
                        a = settings.get("character_anchor")
                        if a:
                            anchor = (a["left"] + a["width"] / 2.0,
                                      a["top"] + a["height"] / 2.0)
                        else:   # нет точки персонажа — центр зоны как ориентир
                            anchor = (region["left"] + region["width"] / 2.0,
                                      region["top"] + region["height"] / 2.0)
                        hits = targets.match_templates_in_crop(
                            crop, region, names, anchor)
                        draw = [{"box": hh["box"], "green": True,
                                 "nearest": hh["nearest"], "text": hh["name"]}
                                for hh in hits]
                        self.q.put(("overlay_boxes", draw))
                    except Exception:
                        pass
                self._overlay_stop.wait(0.07)
        finally:
            try:
                sct.close()
            except Exception:
                pass

    def _draw_overlay(self, draw):
        cv = self.debug_canvas
        if cv is None:
            return
        cv.delete("all")
        for d in draw:
            l, t, w, h = d["box"]
            # сиреневый — ближайший моб (его бот и выберет), зелёный — прочие
            # из белого списка, серый — не из списка.
            if d.get("nearest"):
                color, width = "#c080ff", 3
            elif d["green"]:
                color, width = "#00ff00", 2
            else:
                color, width = "#888888", 2
            cv.create_rectangle(l, t, l + w, t + h, outline=color, width=width)
            if d.get("text"):
                cv.create_text(l, t - 8, text=d["text"], fill=color,
                               anchor="w", font=("Segoe UI", 9, "bold"))

    # --- калибровка полосок рамкой ---
    def _calibrate_bar(self, name):
        hints = {
            "hp": "Обведи КРАСНУЮ полоску HP — она должна быть ПОЛНОЙ   (Esc — отмена)",
            "mp": "Обведи СИНЮЮ полоску MP — она должна быть ПОЛНОЙ   (Esc — отмена)",
            "cp": "Обведи ЖЁЛТУЮ полоску CP — она должна быть ПОЛНОЙ   (Esc — отмена)",
            "target": ("СНАЧАЛА ВЫДЕЛИ МОБА (нужна красная полоска HP цели), "
                       "затем обведи её ПОЛНОЙ   (Esc — отмена)"),
        }
        self._select_region(
            hints[name],
            lambda l, t, w, h: self._save_bar(name, l, t, w, h))

    def calibrate_target_name(self):
        self._select_region(
            "Обведи ИМЯ ЦЕЛИ в окне цели (моб должен быть выделен)   Esc — отмена",
            self._save_target_name)

    # --- режим «цифры» (OCR чисел баров) ---
    def _digit_update(self, name, **kw):
        cfg = settings.get("bar_%s_digits" % name) or {}
        cfg.update(kw)
        settings.set("bar_%s_digits" % name, cfg)
        return cfg

    def _toggle_digit(self, name):
        v = bool(self.digit_enabled[name].get())
        self._digit_update(name, enabled=v)
        if not v:
            self.calib_status.set(f"{name.upper()}: режим цифр выключен")
            return
        spec = settings.get("bar_" + name)
        if not spec:
            self.calib_status.set(
                f"{name.upper()}: сначала откалибруй бар (кнопка «{name.upper()}» выше) "
                "так, чтобы в рамку попали цифры")
            return
        # тест-чтение чисел прямо из области бара
        try:
            with ScreenCapture() as cap:
                frame = cap.grab()
            parsed = ocr.read_number(frame, spec)
        except Exception as e:
            self.calib_status.set(f"Ошибка захвата: {e}")
            return
        if parsed is None:
            self.calib_status.set(
                f"{name.upper()}: цифры в области бара не распознались — "
                "перекалибруй бар плотнее по числам")
        else:
            cur, mx = parsed
            tail = f", макс={mx}" if mx else " (макс не виден — задай Max)"
            self.calib_status.set(f"{name.upper()}: цифры вкл, текущее={cur}{tail}")

    def _save_digit_max(self, name):
        raw = self.digit_max_var[name].get().strip()
        try:
            mx = int(raw) if raw else 0
        except ValueError:
            self.calib_status.set(f"{name.upper()}: Max должно быть целым числом")
            return
        self._digit_update(name, max=mx)

    def _save_target_name(self, left, top, w, h):
        settings.set("target_name_region",
                     {"left": left, "top": top, "width": w, "height": h})
        name = ""
        try:
            with ScreenCapture() as cap:
                frame = cap.grab()
            name = ocr.read_target_name(frame)
        except Exception as e:
            self._append_log(f"[!] Ошибка чтения имени цели: {e}")
        self.calib_status.set(f"Имя цели читается как: '{name}'" if name
                              else "Имя цели: не распозналось (обведи точнее)")
        self._append_log(f"Область имени цели задана: {w}x{h}")

    def _save_bar(self, name, left, top, w, h):
        try:
            with ScreenCapture() as cap:
                frame = cap.grab()
        except Exception as e:
            self.calib_status.set(f"Ошибка захвата: {e}")
            return
        rect = {"left": left, "top": top, "width": w, "height": h}
        color = bars.detect_fill_color(frame, rect)
        spec = dict(rect, color=color, tol=60)
        # HP и HP цели — КРАСНЫЕ (в BGR: R заметно больше B и G). Если пойман не
        # красный цвет, калибровка почти наверняка промахнулась (нет выделенного
        # моба / рамка на фоне) — предупреждаем и НЕ сохраняем кривой цвет.
        if name in ("hp", "target"):
            b, g, r = color
            if not (r > b + 30 and r > g + 30):
                who = "HP цели" if name == "target" else "HP"
                msg = (f"⚠ {who}: цвет BGR {color} НЕ красный — калибровка мимо. "
                       + ("Выдели моба и обведи КРАСНУЮ полоску цели."
                          if name == "target" else
                          "Обведи КРАСНУЮ полоску HP."))
                self.calib_status.set(msg)
                self._append_log("[!] " + msg + " (не сохранено)")
                return
        settings.set("bar_" + name, spec)
        # проверочное чтение сразу после калибровки
        if name == "target":
            present, hp = bars.has_target(frame)
            self.calib_status.set(
                f"Цель: {'есть' if present else 'нет'} ({hp}%), цвет BGR {color}")
        else:
            val = bars.read_self_bars(frame).get(name)
            self.calib_status.set(f"{name.upper()}: читается {val}%, цвет BGR {color}")
        self._append_log(f"Калибрована полоска {name}: {w}x{h}, цвет {color}")

    def _update_hotkey_label(self):
        self.hotkey_lbl.set(
            f"F11 — пауза · F10 — убрать рамки · "
            f"{config.HOTKEY_STOP.upper()} — стоп (работают поверх игры)")

    def _on_stopkey(self, event=None):
        """Сменить клавишу аварийного стопа и перерегистрировать хоткеи."""
        key = self.stopkey_cb.get().strip().lower()
        if not key or key == config.HOTKEY_STOP:
            return
        config.HOTKEY_STOP = key
        settings.set("hotkey_stop", key)
        self._register_hotkeys()          # снять старые, повесить с новой клавишей
        self._update_hotkey_label()
        self._append_log(f"[Клавиши] стоп теперь на {key.upper()}")

    # --- горячие клавиши (регистрируются ОДИН раз при старте панели) ---
    def _register_hotkeys(self):
        self._unregister_hotkeys()
        try:
            self._hotkeys.append(keyboard.add_hotkey(config.HOTKEY_PAUSE, self.pause))
            self._hotkeys.append(keyboard.add_hotkey(config.HOTKEY_STOP, self.stop))
            self._hotkeys.append(keyboard.add_hotkey("f10", self._overlay_panic))
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
        last_overlay = None
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "overlay_boxes":
                    last_overlay = data       # рисуем только последний кадр за цикл
                    continue
                try:
                    self._handle(kind, data)
                except Exception as e:
                    # одна ошибка обработчика не должна останавливать весь цикл
                    # (иначе панель «зависает»: Старт после Стоп перестаёт работать)
                    try:
                        self._append_log(f"[!] ошибка обработки '{kind}': {e}")
                    except Exception:
                        pass
        except queue.Empty:
            pass
        if last_overlay is not None:
            try:
                self._draw_overlay(last_overlay)
            except Exception:
                pass
        self.root.after(50, self._drain)

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
        elif kind == "overlay_boxes":
            self._draw_overlay(data)
        elif kind == "break":
            self.status_var.set(f"ПЕРЕРЫВ · осталось {data} c")
            self.banner.config(bg="#1565c0")
        elif kind == "resumed":
            pass  # следующий status вернёт баннер в рабочий вид
        elif kind == "stopped":
            if data is not None and data != self._worker_gen:
                return              # «остановлен» от старого (бро́шенного) воркера
            self._set_stopped_ui()
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
        self._destroy_debug_overlay()
        self.root.after(200, self.root.destroy)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
