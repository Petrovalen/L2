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
    "assist_skill": "доп. скилл",
    "heal_potion": "хилка",
    "mana_potion": "мана",
    "pickup": "подобрать лут",
}

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
        self._overlay_matched = []      # [(cx,cy,name)] из фонового OCR

        settings.apply_to_config()   # применить сохранённые настройки панели

        root.title("l2bot — панель управления")
        root.geometry("480x930")
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
        for label, key in (("HP", "hp"), ("MP", "mp"), ("CP", "cp"), ("Цель", "target")):
            tk.Button(crow, text=label, width=5,
                      command=lambda k=key: self._calibrate_bar(k)).pack(side="left", padx=2)
        tk.Button(crow, text="Имя цели", width=8,
                  command=self.calibrate_target_name).pack(side="left", padx=2)
        self.calib_status = tk.StringVar(value="")
        tk.Label(calib, textvariable=self.calib_status, font=("Segoe UI", 8),
                 fg="#1565c0", anchor="w").pack(fill="x", padx=8)

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
        self.zone_btn = tk.Button(row, text="Зона поиска", command=self.set_search_region)
        self.zone_btn.pack(side="left", padx=4)
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
        self._overlay_matched = []
        threading.Thread(target=self._overlay_fast_loop, daemon=True).start()
        threading.Thread(target=self._overlay_ocr_loop, daemon=True).start()
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
        """Быстро (~20/сек): захват ТОЛЬКО зоны -> боксы. Зелёный — по буферу OCR."""
        import mss
        import numpy as np
        sct = mss.mss()
        try:
            while not self._overlay_stop.is_set():
                region = settings.get("search_region")
                if region:
                    try:
                        mon = {"left": region["left"], "top": region["top"],
                               "width": region["width"], "height": region["height"]}
                        crop = np.asarray(sct.grab(mon))[:, :, :3]
                        boxes = targets.boxes_in_crop(crop, region["left"], region["top"])
                        matched = self._overlay_matched
                        draw = []
                        for (l, t, w, h) in boxes:
                            cx, cy = l + w / 2, t + h / 2
                            name = None
                            for (mx, my, mn) in matched:
                                if (cx - mx) ** 2 + (cy - my) ** 2 < 45 ** 2:
                                    name = mn
                                    break
                            draw.append({"box": (l, t, w, h),
                                         "green": name is not None, "text": name})
                        self.q.put(("overlay_boxes", draw))
                    except Exception:
                        pass
                self._overlay_stop.wait(0.05)
        finally:
            try:
                sct.close()
            except Exception:
                pass

    def _overlay_ocr_loop(self):
        """Медленно (~1/сек): полный кадр + OCR -> буфер совпадений (зелёные)."""
        try:
            cap = ScreenCapture()
        except Exception:
            return
        try:
            while not self._overlay_stop.is_set():
                try:
                    region = settings.get("search_region")
                    names = mob_list.load()
                    if region and names:
                        frame = cap.grab()
                        plates = targets.scan_nameplates(frame, region, names)
                        self._overlay_matched = [
                            (l + w / 2, t + h / 2, p["name"])
                            for p in plates if p["name"]
                            for (l, t, w, h) in [p["box"]]]
                    else:
                        self._overlay_matched = []
                except Exception:
                    pass
                self._overlay_stop.wait(1.0)
        finally:
            try:
                cap.close()
            except Exception:
                pass

    def _draw_overlay(self, draw):
        cv = self.debug_canvas
        if cv is None:
            return
        cv.delete("all")
        for d in draw:
            l, t, w, h = d["box"]
            color = "#00ff00" if d["green"] else "#888888"
            cv.create_rectangle(l, t, l + w, t + h, outline=color, width=2)
            if d.get("text"):
                cv.create_text(l, t - 8, text=d["text"], fill=color,
                               anchor="w", font=("Segoe UI", 9, "bold"))

    # --- калибровка полосок рамкой ---
    def _calibrate_bar(self, name):
        labels = {"hp": "HP", "mp": "MP", "cp": "CP", "target": "HP ЦЕЛИ"}
        self._select_region(
            f"Обведи полоску {labels[name]} — она должна быть ПОЛНОЙ   (Esc — отмена)",
            lambda l, t, w, h: self._save_bar(name, l, t, w, h))

    def calibrate_target_name(self):
        self._select_region(
            "Обведи ИМЯ ЦЕЛИ в окне цели (моб должен быть выделен)   Esc — отмена",
            self._save_target_name)

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
