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
from tkinter import ttk, simpledialog, messagebox

import keyboard

import config
from capture.screen import ScreenCapture
from logic.fsm import BotFSM
from logic.humanize import BreakScheduler
from logic import mob_list
from logic import settings, notify
from control import input_ctl as ctl
from vision import bars, ocr, targets

# Человекочитаемые подписи действий для ленты.
ACTION_LABELS = {
    "target_nearest": "выбор цели",
    "next_target": "следующая цель",
    "attack": "атака",
    "heal_potion": "хилка",
    "mana_potion": "мана",
    "pickup": "подобрать лут",
    "assist_select": "ассист: выбор игрока",
    "assist": "ассист: взять цель",
}

# Действия с настраиваемой клавишей и их подписи в редакторе (порядок сохранён).
# Лечение/банки вынесены в отдельный блок «Лечение».
KEY_ACTIONS = [
    ("target_nearest", "Выбор цели (ближайшая)"),
    ("next_target", "Следующая цель (перебор — обход застрявшей)"),
    ("attack", "Атака / основной скилл"),
    ("pickup", "Подобрать лут"),
    ("assist_select", "Ассист: выбрать игрока"),
    ("assist", "Ассист: взять его цель"),
]

# Дефолт новой способности при добавлении в редакторе.
DEFAULT_SKILL = {"key": "", "label": "Скилл", "cooldown": 4.0,
                 "target_hp_above": 0, "target_hp_below": 100, "mp_min": 0,
                 "once": False, "enabled": True}

WARMUP_SEC = 3          # пауза перед стартом — успеть переключиться в игру
MAX_LOG_LINES = 500     # ограничение размера ленты


class _Tooltip:
    """Всплывающая подсказка при наведении на виджет."""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _e=None):
        if self.tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 3
        except tk.TclError:
            return
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry("+%d+%d" % (x, y))
        self.tip.attributes("-topmost", True)
        tk.Label(self.tip, text=self.text, bg="#ffffe0", fg="#222",
                 relief="solid", borderwidth=1, font=("Segoe UI", 8),
                 justify="left", wraplength=340, padx=5, pady=3).pack()

    def _hide(self, _e=None):
        if self.tip is not None:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None


def tip(widget, text):
    """Навесить подсказку на виджет и вернуть его (для чейнинга)."""
    _Tooltip(widget, text)
    return widget


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
        self.zones_overlay = None        # прозрачный оверлей выделенных зон
        self.zones_canvas = None
        self._overlay_stop = threading.Event()

        settings.apply_to_config()   # применить сохранённые настройки панели

        root.title("l2bot — панель управления")
        root.geometry("560x900")
        root.minsize(520, 640)

        # --- баннер статуса ---
        self.status_var = tk.StringVar(value="ОСТАНОВЛЕН")
        self.banner = tk.Label(root, textvariable=self.status_var,
                               font=("Segoe UI", 16, "bold"),
                               fg="white", bg="#555", pady=10)
        self.banner.pack(fill="x")

        # --- профили (ПК / персонаж) ---
        self._build_profiles_bar(root)

        # --- полоски HP/MP/CP (всегда видны) ---
        barf = ttk.LabelFrame(root, text="Состояние персонажа")
        barf.pack(fill="x", padx=10, pady=(6, 4))
        self.hp = self._make_bar(barf, "HP")
        self.mp = self._make_bar(barf, "MP")
        self.cp = self._make_bar(barf, "CP")

        # --- вкладки настроек (статус/бары/кнопки/лента — всегда видны вне вкладок) ---
        # Каждая вкладка прокручивается: контента много, окно может быть меньше.
        # Вкладки и «Действия бота» разделены перетаскиваемым сплиттером —
        # пользователь мышью растягивает окошко действий по высоте.
        self._main_paned = ttk.PanedWindow(root, orient="vertical")
        self._nb = ttk.Notebook(self._main_paned)
        _p_calib = ttk.Frame(self._nb); self._nb.add(_p_calib, text=" Калибровка ")
        _p_mobs = ttk.Frame(self._nb); self._nb.add(_p_mobs, text=" Мобы и поиск ")
        _p_combat = ttk.Frame(self._nb); self._nb.add(_p_combat, text=" Бой ")
        _p_notify = ttk.Frame(self._nb); self._nb.add(_p_notify, text=" Уведомления ")
        tab_calib = self._scrollable(_p_calib)
        tab_mobs = self._scrollable(_p_mobs)
        tab_combat = self._scrollable(_p_combat)
        tab_notify = self._scrollable(_p_notify)

        # --- калибровка полосок рамкой ---
        calib = ttk.LabelFrame(tab_calib, text="Калибровка полосок (обведи рамкой ПОЛНУЮ полоску)")
        calib.pack(fill="x", padx=10, pady=(6, 6))
        tip(calib, "Обведи каждую полоску целиком (заполненной). В ту же рамку "
                   "должны попасть цифры, если включишь режим «Чтение цифрами».")
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
        dg = ttk.LabelFrame(tab_calib, text="Чтение цифрами (OCR): процент = тек/макс × 100")
        dg.pack(fill="x", padx=10, pady=(0, 6))
        tip(dg, "Читать HP/MP/CP/цель как ЧИСЛА из бара (когда в рамку попали цифры), "
                "а не по заливке. Max пусто = брать максимум из формата «тек/макс».")
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
        tip(self.start_btn, "Запустить бота. Даётся 3 сек, чтобы переключиться в игру.")
        tip(self.pause_btn, "Пауза/продолжить (также F11). Бот замирает, не теряя настройки.")
        tip(self.stop_btn, "Полностью остановить бота (также клавиша стопа, по умолч. F12).")

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
        mobframe = ttk.LabelFrame(tab_mobs, text="Мои мобы (кого бить)")
        mobframe.pack(fill="x", padx=10, pady=6)
        row = tk.Frame(mobframe)
        row.pack(fill="x", padx=6, pady=4)
        self.add_btn = tk.Button(row, text="＋ Добавить моба (рамкой)",
                                 command=self.add_mob)
        self.add_btn.pack(side="left")
        tip(self.add_btn, "Обведи ник моба в игре — он попадёт в список «кого бить», "
                          "и запомнится шаблон его ника для поиска.")
        self.del_btn = tk.Button(row, text="Удалить", command=self.remove_mob)
        self.del_btn.pack(side="left", padx=4)
        tip(self.del_btn, "Удалить выбранного в списке моба (и его шаблон).")
        self.zone_btn = tk.Button(row, text="Зона поиска", command=self.set_search_region)
        self.zone_btn.pack(side="left", padx=4)
        tip(self.zone_btn, "Область экрана, где бот ищет ники мобов. Обведи центр "
                           "игрового мира без чата и панелей интерфейса.")
        self.char_btn = tk.Button(row, text="Точка персонажа",
                                  command=self.set_character_anchor)
        self.char_btn.pack(side="left", padx=4)
        tip(self.char_btn, "Точка отсчёта «ближайшего» моба. Обведи небольшую область "
                           "на персонаже (лучше по ногам).")
        self.mob_status = tk.StringVar(value="")
        tk.Label(mobframe, textvariable=self.mob_status, font=("Segoe UI", 8),
                 fg="#1565c0", anchor="w").pack(fill="x", padx=8)
        self.mob_listbox = tk.Listbox(mobframe, height=4, font=("Consolas", 9))
        self.mob_listbox.pack(fill="x", padx=6, pady=4)
        self._refresh_mobs()

        # --- настройки визуального поиска ---
        setframe = ttk.LabelFrame(tab_mobs, text="Настройки поиска мобов")
        setframe.pack(fill="x", padx=10, pady=6)
        tip(setframe, "Визуальный поиск по никам, проверка имени цели, отладочные "
                      "рамки/зоны и смещение точки клика по телу моба.")
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
        self.zones_var = tk.BooleanVar(value=False)
        tk.Checkbutton(setframe, text="Показывать выделенные зоны (отладка)",
                       variable=self.zones_var,
                       command=self._toggle_zones_overlay).pack(anchor="w", padx=6)
        rx = tk.Frame(setframe)
        rx.pack(fill="x", padx=6)
        tk.Label(rx, text="Смещение клика X, px", width=18, anchor="w").pack(side="left")
        self.dx_scale = tk.Scale(rx, from_=-60, to=60, orient="horizontal",
                                 command=self._on_click_dx)
        self.dx_scale.set(config.NAME_CLICK_DX)
        self.dx_scale.pack(side="left", fill="x", expand=True)
        self.dx_scale.bind("<ButtonRelease-1>", lambda e:
                           settings.set("name_click_dx", int(self.dx_scale.get())))
        r1 = tk.Frame(setframe)
        r1.pack(fill="x", padx=6)
        tk.Label(r1, text="Смещение клика Y, px", width=18, anchor="w").pack(side="left")
        self.dy_scale = tk.Scale(r1, from_=-40, to=80, orient="horizontal",
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
        camframe = ttk.LabelFrame(tab_mobs, text="Камера (активный поиск)")
        camframe.pack(fill="x", padx=10, pady=6)
        tip(camframe, "Когда цель не находится, бот вращает камеру (зажатая ПКМ), "
                      "чтобы в кадр попали новые мобы. Скорость = резкость поворота, "
                      "плавность = длительность свайпа.")
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

        # --- вкладка «Бой»: клавиши, лечение, ротация, баффы (со скроллом) ---
        self._build_combat_tab(tab_combat)

        # --- Telegram: уведомление на телефон при смерти ---
        tgf = ttk.LabelFrame(tab_notify, text="Telegram (уведомление о смерти)")
        tgf.pack(fill="x", padx=10, pady=6)
        tip(tgf, "Пришлёт сообщение в Telegram, когда HP держится ~0 (смерть). "
                 "Токен — от @BotFather, chat_id — свой (напр. через @userinfobot).")
        self.tg_enabled = tk.BooleanVar(value=config.DEATH_NOTIFY)
        tk.Checkbutton(tgf, text="Слать в Telegram, когда HP=0",
                       variable=self.tg_enabled).pack(anchor="w", padx=6)
        self.cp_notify = tk.BooleanVar(value=config.CP_NOTIFY)
        tk.Checkbutton(tgf, text="Слать, когда CP пробит (перестал быть полным)",
                       variable=self.cp_notify).pack(anchor="w", padx=6)
        tr1 = tk.Frame(tgf); tr1.pack(fill="x", padx=6, pady=1)
        tk.Label(tr1, text="Токен бота", width=10, anchor="w").pack(side="left")
        self.tg_token = tk.StringVar(value=config.TELEGRAM_TOKEN)
        tk.Entry(tr1, textvariable=self.tg_token, show="•").pack(side="left", fill="x", expand=True)
        tr2 = tk.Frame(tgf); tr2.pack(fill="x", padx=6, pady=1)
        tk.Label(tr2, text="chat_id", width=10, anchor="w").pack(side="left")
        self.tg_chat = tk.StringVar(value=config.TELEGRAM_CHAT_ID)
        tk.Entry(tr2, textvariable=self.tg_chat, width=18).pack(side="left")
        tk.Button(tr2, text="Сохранить+тест",
                  command=self._save_telegram).pack(side="left", padx=6)
        self.tg_status = tk.StringVar(value="")
        tk.Label(tgf, textvariable=self.tg_status, font=("Segoe UI", 8),
                 fg="#1565c0", anchor="w").pack(fill="x", padx=8)

        # --- лента действий (растягивается сплиттером; тянуть за разделитель) ---
        logframe = ttk.LabelFrame(self._main_paned, text="Действия бота (тяни разделитель ↕)")
        self.log = tk.Text(logframe, height=8, state="disabled",
                           font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
                           wrap="none")
        scroll = ttk.Scrollbar(logframe, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        # вкладки сверху (больше веса), лента снизу — с перетаскиваемым сплиттером
        self._main_paned.add(self._nb, weight=3)
        self._main_paned.add(logframe, weight=1)
        self._main_paned.pack(fill="both", expand=True, padx=6, pady=4)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._register_hotkeys()        # F11/F12/F10 — один раз на всё время работы
        self.root.after(50, self._drain)

    def _scrollable(self, parent):
        """Вернуть внутренний фрейм с вертикальной прокруткой внутри `parent`
        (для высоких вкладок, напр. «Бой»)."""
        canvas = tk.Canvas(parent, highlightthickness=0)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win_id, width=e.width))
        # прокрутка колесом только пока курсор над этой областью
        def _wheel(e):
            canvas.yview_scroll(int(-e.delta / 120), "units")
        inner.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _wheel))
        inner.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return inner

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

    def set_buff_region(self):
        self._select_region(
            "Обведи ЗОНУ панели баффов (где иконки баффов персонажа)   Esc — отмена",
            self._save_buff_region)

    def _save_buff_region(self, left, top, w, h):
        settings.set("buff_region",
                     {"left": left, "top": top, "width": w, "height": h})
        msg = f"Зона баффов задана: {w}x{h} @ ({left},{top})"
        if getattr(self, "_ctrl_status", None) is not None:
            self._ctrl_status.set(msg)
        self._append_log(msg)

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

    def _on_assist_toggle(self):
        v = bool(self.assist_mode.get())
        config.ASSIST_MODE = v
        settings.set("assist_mode", v)
        self._ctrl_status.set(
            "Режим ассиста ВКЛ — задай клавиши «выбрать игрока» и «ассист» и сохрани боевые настройки."
            if v else "Режим ассиста выключен (бот сам выбирает цель).")
        self._append_log("Режим ассиста: %s" % ("включён" if v else "выключен"))

    def _save_telegram(self):
        enabled = bool(self.tg_enabled.get())
        token = self.tg_token.get().strip()
        chat = self.tg_chat.get().strip()
        settings.set("death_notify", enabled)
        settings.set("cp_notify", bool(self.cp_notify.get()))
        settings.set("telegram_token", token)
        settings.set("telegram_chat_id", chat)
        settings.apply_to_config()
        ok = notify.send_telegram(token, chat, "L2: тест уведомления ✅")
        self.tg_status.set("Сохранено. Тест отправлен — проверь Telegram."
                           if ok else "Сохранено. Для теста заполни токен и chat_id.")
        self._append_log("Telegram-уведомления обновлены.")

    def _on_click_dx(self, val):
        config.NAME_CLICK_DX = int(float(val))   # применяем сразу; сохраняем при отпускании

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

    # --- вкладка «Бой»: клавиши/лут, лечение, ротация, баффы ----------------
    # ---- профили (ПК / персонаж) -------------------------------------------
    def _build_profiles_bar(self, parent):
        """
        Панель выбора активных профилей. Профиль КОМПЬЮТЕРА — калибровки экрана;
        профиль ПЕРСОНАЖА — клавиши/скиллы/баффы/лечение. Общие настройки (камера,
        Telegram, лут, тайминги) в профили не входят.
        """
        pf = ttk.LabelFrame(parent, text="Профили (выбираются вручную)")
        pf.pack(fill="x", padx=10, pady=(4, 4))
        tip(pf, "Профиль «Компьютер» — калибровки полосок и зон под этот экран.\n"
                "Профиль «Персонаж» — клавиши, ротация скиллов, баффы, лечение.\n"
                "«Новый» — пустой, «Копия» — копия текущего. Переключение применяется сразу.")
        self._profile_cb = {}
        for kind, label in (("machine", "Компьютер"), ("character", "Персонаж")):
            row = tk.Frame(pf)
            row.pack(fill="x", padx=6, pady=2)
            tk.Label(row, text=label, width=10, anchor="w").pack(side="left")
            cb = ttk.Combobox(row, width=16, state="readonly",
                              values=settings.list_profiles(kind))
            cb.set(settings.active_profile(kind) or "")
            cb.pack(side="left", padx=(0, 4))
            cb.bind("<<ComboboxSelected>>",
                    lambda e, k=kind: self._switch_profile(k, self._profile_cb[k].get()))
            self._profile_cb[kind] = cb
            tk.Button(row, text="Новый", width=6,
                      command=lambda k=kind: self._new_profile(k, copy=False)).pack(side="left", padx=1)
            tk.Button(row, text="Копия", width=6,
                      command=lambda k=kind: self._new_profile(k, copy=True)).pack(side="left", padx=1)
            tk.Button(row, text="✎", width=2,
                      command=lambda k=kind: self._rename_profile(k)).pack(side="left", padx=1)
            tk.Button(row, text="🗑", width=2,
                      command=lambda k=kind: self._delete_profile(k)).pack(side="left", padx=1)

    def _kind_label(self, kind):
        return "ПК" if kind == "machine" else "персонажа"

    def _refresh_profile_lists(self):
        """Обновить выпадающие списки профилей и выделить активные."""
        for kind, cb in self._profile_cb.items():
            cb["values"] = settings.list_profiles(kind)
            cb.set(settings.active_profile(kind) or "")

    def _switch_profile(self, kind, name):
        settings.set_active(kind, name)
        settings.apply_to_config()
        self._reload_profile_fields()
        self._append_log("Профиль %s: %s" % (self._kind_label(kind), name))

    def _new_profile(self, kind, copy=False):
        title = "Новый профиль " + self._kind_label(kind)
        name = simpledialog.askstring(title, "Имя профиля:", parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        if name in settings.list_profiles(kind):
            messagebox.showwarning(title, "Профиль с таким именем уже есть.")
            return
        src = settings.active_profile(kind) if copy else None
        settings.create_profile(kind, name, copy_from=src)
        settings.apply_to_config()
        self._refresh_profile_lists()
        self._reload_profile_fields()
        self._append_log("Создан профиль %s: %s%s"
                         % (self._kind_label(kind), name, " (копия)" if copy else ""))

    def _rename_profile(self, kind):
        cur = settings.active_profile(kind)
        name = simpledialog.askstring("Переименовать профиль " + self._kind_label(kind),
                                      "Новое имя:", initialvalue=cur, parent=self.root)
        if not name or not name.strip() or name.strip() == cur:
            return
        if name.strip() in settings.list_profiles(kind):
            messagebox.showwarning("Переименовать", "Профиль с таким именем уже есть.")
            return
        settings.rename_profile(kind, cur, name.strip())
        self._refresh_profile_lists()

    def _delete_profile(self, kind):
        cur = settings.active_profile(kind)
        if len(settings.list_profiles(kind)) <= 1:
            messagebox.showinfo("Удалить профиль", "Нельзя удалить последний профиль.")
            return
        if not messagebox.askyesno("Удалить профиль",
                                   "Удалить профиль %s «%s»?" % (self._kind_label(kind), cur)):
            return
        settings.delete_profile(kind, cur)
        settings.apply_to_config()
        self._refresh_profile_lists()
        self._reload_profile_fields()
        self._append_log("Удалён профиль %s: %s" % (self._kind_label(kind), cur))

    def _set_potion_vars(self, v, cfg, pct_key):
        v["enabled"].set(bool(cfg.get("enabled")))
        v["key"].set(str(cfg.get("key") or ""))
        v["pct"].set(int(cfg.get(pct_key, 0)))
        v["cooldown"].set(float(cfg.get("cooldown", 3.0)))

    def _reload_profile_fields(self):
        """Перелить значения активных профилей в существующие поля панели."""
        # клавиши (профиль персонажа)
        for action, var in getattr(self, "_key_vars", {}).items():
            var.set(config.KEYS.get(action) or "")
        # лечение / банки (профиль персонажа)
        if hasattr(self, "_heal_vars"):
            self._set_potion_vars(self._heal_vars, config.HEAL, "hp_percent")
        if hasattr(self, "_mppot_vars"):
            self._set_potion_vars(self._mppot_vars, config.MP_POTION, "mp_percent")
        # ротация скиллов (перерисовать строки из config.SKILLS)
        if hasattr(self, "_skill_vars"):
            self._skill_vars = [self._skill_to_vars({**DEFAULT_SKILL, **sk})
                                for sk in config.SKILLS]
            self._render_skill_rows()
        # баффы
        if hasattr(self, "_buff_vars"):
            self._buff_vars = [self._buff_to_vars(b) for b in config.BUFFS]
            self._render_buff_rows()
        # режим цифр (профиль ПК)
        for key, var in getattr(self, "digit_enabled", {}).items():
            d = settings.get("bar_%s_digits" % key) or {}
            var.set(bool(d.get("enabled")))
            if key in self.digit_max_var:
                self.digit_max_var[key].set(str(d.get("max") or ""))

    def _build_combat_tab(self, parent):
        """Собрать боевые настройки прямо в контейнере вкладки `parent`."""
        win = parent   # строим во вкладке, не в отдельном окне

        # --- клавиши действий ---
        kf = ttk.LabelFrame(win, text="Клавиши действий (имя клавиши: 1, 2, f1, e…)")
        kf.pack(fill="x", padx=10, pady=(10, 6))
        tip(kf, "Игровые клавиши действий бота. «Следующая цель» нужна, чтобы "
                "обходить недостижимого моба. «Подбор лута» — сколько раз жать после убийства.")
        self._key_vars = {}
        for action, label in KEY_ACTIONS:
            row = tk.Frame(kf)
            row.pack(fill="x", padx=8, pady=2)
            tk.Label(row, text=label, width=26, anchor="w").pack(side="left")
            var = tk.StringVar(value=config.KEYS.get(action) or "")
            tk.Entry(row, textvariable=var, width=8).pack(side="left")
            self._key_vars[action] = var
        lootrow = tk.Frame(kf)
        lootrow.pack(fill="x", padx=8, pady=2)
        tk.Label(lootrow, text="Подбор лута: нажатий от/до", width=26,
                 anchor="w").pack(side="left")
        self._loot_presses_min_var = tk.IntVar(value=int(config.LOOT_PRESSES_MIN))
        tk.Spinbox(lootrow, from_=1, to=10, width=4,
                   textvariable=self._loot_presses_min_var).pack(side="left")
        tk.Label(lootrow, text="—").pack(side="left", padx=2)
        self._loot_presses_max_var = tk.IntVar(value=int(config.LOOT_PRESSES_MAX))
        tk.Spinbox(lootrow, from_=1, to=10, width=4,
                   textvariable=self._loot_presses_max_var).pack(side="left")
        self.assist_mode = tk.BooleanVar(value=config.ASSIST_MODE)
        tk.Checkbutton(kf, text="Режим ассиста (бить по цели другого игрока, а не выбирать самому)",
                       variable=self.assist_mode,
                       command=self._on_assist_toggle).pack(anchor="w", padx=6, pady=(2, 0))
        tk.Label(kf, text="Ассист: задай клавиши «выбрать игрока» и «взять его цель». "
                          "Пусто = действие отключено.", font=("Segoe UI", 8),
                 fg="#666", anchor="w", justify="left", wraplength=460).pack(fill="x", padx=10, pady=(0, 4))

        # --- лечение / банки (выживание, работает в любом состоянии) ---
        hf = ttk.LabelFrame(win, text="Лечение (пить при падении своего HP/MP)")
        hf.pack(fill="x", padx=10, pady=6)
        tip(hf, "Отдельные банки: пить, когда своё HP/MP опустилось ниже %. "
                "Работает всегда — и в бою, и вне боя.")
        self._heal_vars = self._potion_to_vars(config.HEAL, "hp_percent")
        self._mppot_vars = self._potion_to_vars(config.MP_POTION, "mp_percent")
        self._render_potion_row(hf, "HP-банка", self._heal_vars, "HP <", "%")
        self._render_potion_row(hf, "MP-банка", self._mppot_vars, "MP <", "%")

        # --- способности (боевая ротация: порядок сверху вниз = приоритет) ---
        sf = ttk.LabelFrame(win, text="Ротация в бою (порядок сверху вниз = приоритет)")
        sf.pack(fill="x", padx=10, pady=6)
        hint = ("Каждый тик кастуется ПЕРВАЯ подходящая способность, между ними — автоатака.\n"
                "HP цели от…до — каст только если HP цели в этом диапазоне % (0…100 = всегда).\n"
                "MP ≥ — только если своя MP не ниже % (0 = без условия).  КД — не чаще раза в с.\n"
                "«раз» — применить один раз за цель (для стартовых скиллов последовательности).")
        tk.Label(sf, text=hint, font=("Segoe UI", 8), fg="#1565c0",
                 justify="left", anchor="w").pack(fill="x", padx=8, pady=(4, 2))

        head = tk.Frame(sf)
        head.pack(fill="x", padx=8)
        for text, w in (("вкл", 3), ("Клав", 6), ("HP от %", 7), ("HP до %", 7),
                        ("MP ≥ %", 7), ("КД", 5), ("раз", 4), ("гот", 5), ("", 8)):
            tk.Label(head, text=text, width=w, anchor="w",
                     font=("Segoe UI", 8, "bold")).pack(side="left", padx=1)

        self._skills_box = tk.Frame(sf)
        self._skills_box.pack(fill="x", padx=4)

        # текущие способности -> редактируемые строки (глубокая копия значений)
        self._skill_vars = []
        for sk in config.SKILLS:
            self._skill_vars.append(self._skill_to_vars({**DEFAULT_SKILL, **sk}))
        self._render_skill_rows()

        tk.Button(sf, text="＋ Добавить способность",
                  command=self._add_skill_row).pack(anchor="w", padx=8, pady=4)

        # --- самобаффы (поддерживаются ВНЕ боя по иконке в панели баффов) ---
        bf = ttk.LabelFrame(win, text="Баффы (держать на персонаже, вне боя)")
        bf.pack(fill="x", padx=10, pady=6)
        tk.Button(bf, text="Зона панели баффов",
                  command=self.set_buff_region).pack(anchor="w", padx=8, pady=(4, 0))
        bhint = ("Имя — подпись/файл иконки. «Иконка» — обведи иконку баффа в панели.\n"
                 "Если иконки нет в зоне — бафф считается спавшим и накладывается.")
        tk.Label(bf, text=bhint, font=("Segoe UI", 8), fg="#1565c0",
                 justify="left", anchor="w").pack(fill="x", padx=8, pady=(2, 2))
        bhead = tk.Frame(bf)
        bhead.pack(fill="x", padx=8)
        for text, w in (("вкл", 3), ("Имя", 14), ("Клав", 6), ("КД", 5), ("", 14)):
            tk.Label(bhead, text=text, width=w, anchor="w",
                     font=("Segoe UI", 8, "bold")).pack(side="left", padx=1)
        self._buffs_box = tk.Frame(bf)
        self._buffs_box.pack(fill="x", padx=4)
        self._buff_vars = []
        for b in config.BUFFS:
            self._buff_vars.append(self._buff_to_vars(b))
        self._render_buff_rows()
        tk.Button(bf, text="＋ Добавить бафф",
                  command=self._add_buff_row).pack(anchor="w", padx=8, pady=4)

        # --- статус + сохранить ---
        self._ctrl_status = tk.StringVar(value="")
        tk.Label(win, textvariable=self._ctrl_status, font=("Segoe UI", 8),
                 fg="#1565c0", anchor="w").pack(fill="x", padx=12)
        tk.Button(win, text="💾 Сохранить боевые настройки",
                  command=self._save_controls, bg="#2e7d32", fg="white",
                  font=("Segoe UI", 10, "bold")).pack(fill="x", padx=10, pady=8)

    # --- банки (HP/MP) ---
    def _potion_to_vars(self, cfg, pct_key):
        return {
            "enabled": tk.BooleanVar(value=bool(cfg.get("enabled"))),
            "key": tk.StringVar(value=str(cfg.get("key") or "")),
            "pct": tk.IntVar(value=int(cfg.get(pct_key, 0))),
            "cooldown": tk.DoubleVar(value=float(cfg.get("cooldown", 3.0))),
        }

    def _render_potion_row(self, parent, title, v, pct_label, pct_suffix):
        row = tk.Frame(parent)
        row.pack(fill="x", padx=8, pady=2)
        tk.Checkbutton(row, variable=v["enabled"], width=2).pack(side="left")
        tk.Label(row, text=title, width=9, anchor="w").pack(side="left")
        tk.Label(row, text="клавиша").pack(side="left")
        tk.Entry(row, textvariable=v["key"], width=5).pack(side="left", padx=(2, 8))
        tk.Label(row, text=pct_label).pack(side="left")
        tk.Spinbox(row, from_=1, to=99, textvariable=v["pct"], width=4).pack(side="left")
        tk.Label(row, text=pct_suffix + "   КД").pack(side="left")
        tk.Spinbox(row, from_=0.0, to=120.0, increment=0.5,
                   textvariable=v["cooldown"], width=5).pack(side="left", padx=2)

    def _skill_to_vars(self, sk):
        below = sk.get("target_hp_below", sk.get("target_hp_max", 100))
        return {
            "enabled": tk.BooleanVar(value=bool(sk.get("enabled", True))),
            "key": tk.StringVar(value=str(sk.get("key") or "")),
            "target_hp_above": tk.IntVar(value=int(sk.get("target_hp_above", 0))),
            "target_hp_below": tk.IntVar(value=int(below)),
            "mp_min": tk.IntVar(value=int(sk.get("mp_min", 0))),
            "cooldown": tk.DoubleVar(value=float(sk.get("cooldown", 4.0))),
            "once": tk.BooleanVar(value=bool(sk.get("once", False))),
            "ready_region": sk.get("ready_region"),   # dict|None (проверка готовности)
        }

    def _capture_skill_ready(self, idx):
        if not (0 <= idx < len(self._skill_vars)):
            return
        key = self._skill_vars[idx]["key"].get().strip()
        if not key:
            self._ctrl_status.set("Сначала впиши клавишу скилла (для файла иконки).")
            return
        self._select_region(
            f"Обведи ИКОНКУ скилла (клавиша {key}) в хотбаре — скилл должен быть "
            "ГОТОВ (не на откате)   Esc — отмена",
            lambda l, t, w, h: self._save_skill_ready(idx, key, l, t, w, h))

    def _save_skill_ready(self, idx, key, left, top, w, h):
        region = {"left": left, "top": top, "width": w, "height": h}
        try:
            with ScreenCapture() as cap:
                frame = cap.grab()
            ok = targets.save_skill_template(frame, region, key)
        except Exception as e:
            self._ctrl_status.set(f"Ошибка захвата иконки скилла: {e}")
            return
        if ok and 0 <= idx < len(self._skill_vars):
            self._skill_vars[idx]["ready_region"] = region
            self._render_skill_rows()
            self._ctrl_status.set(f"Готовность скилла (клавиша {key}) настроена.")
        else:
            self._ctrl_status.set("Иконку скилла сохранить не удалось.")

    # --- самобаффы ---
    def _buff_to_vars(self, b):
        return {
            "enabled": tk.BooleanVar(value=bool(b.get("enabled", True))),
            "label": tk.StringVar(value=str(b.get("label") or "")),
            "key": tk.StringVar(value=str(b.get("key") or "")),
            "cooldown": tk.DoubleVar(value=float(b.get("cooldown", 3.0))),
        }

    def _add_buff_row(self):
        self._buff_vars.append(self._buff_to_vars({"label": "", "key": ""}))
        self._render_buff_rows()

    def _del_buff_row(self, idx):
        if 0 <= idx < len(self._buff_vars):
            self._buff_vars.pop(idx)
            self._render_buff_rows()

    def _render_buff_rows(self):
        for w in self._buffs_box.winfo_children():
            w.destroy()
        for idx, v in enumerate(self._buff_vars):
            row = tk.Frame(self._buffs_box)
            row.pack(fill="x", pady=1)
            tk.Checkbutton(row, variable=v["enabled"], width=2).pack(side="left", padx=1)
            tk.Entry(row, textvariable=v["label"], width=14).pack(side="left", padx=1)
            tk.Entry(row, textvariable=v["key"], width=6).pack(side="left", padx=1)
            tk.Spinbox(row, from_=0.0, to=600.0, increment=0.5,
                       textvariable=v["cooldown"], width=5).pack(side="left", padx=1)
            tk.Button(row, text="Иконка", width=7,
                      command=lambda i=idx: self._capture_buff_icon(i)).pack(side="left", padx=1)
            tk.Button(row, text="✕", width=2,
                      command=lambda i=idx: self._del_buff_row(i)).pack(side="left")

    def _capture_buff_icon(self, idx):
        if not (0 <= idx < len(self._buff_vars)):
            return
        label = self._buff_vars[idx]["label"].get().strip()
        if not label:
            self._ctrl_status.set("Сначала впиши имя баффа (для файла иконки).")
            return
        self._select_region(
            f"Обведи ИКОНКУ баффа «{label}» в панели баффов   Esc — отмена",
            lambda l, t, w, h: self._save_buff_icon(label, l, t, w, h))

    def _save_buff_icon(self, label, left, top, w, h):
        try:
            with ScreenCapture() as cap:
                frame = cap.grab()
            ok = targets.save_buff_template(
                frame, {"left": left, "top": top, "width": w, "height": h}, label)
        except Exception as e:
            self._ctrl_status.set(f"Ошибка захвата иконки: {e}")
            return
        self._ctrl_status.set(f"Иконка баффа «{label}» сохранена ({w}x{h})."
                              if ok else "Иконку сохранить не удалось.")

    def _add_skill_row(self):
        self._skill_vars.append(self._skill_to_vars(DEFAULT_SKILL))
        self._render_skill_rows()

    def _del_skill_row(self, idx):
        if 0 <= idx < len(self._skill_vars):
            self._skill_vars.pop(idx)
            self._render_skill_rows()

    def _move_skill_row(self, idx, delta):
        j = idx + delta
        if 0 <= idx < len(self._skill_vars) and 0 <= j < len(self._skill_vars):
            self._skill_vars[idx], self._skill_vars[j] = \
                self._skill_vars[j], self._skill_vars[idx]
            self._render_skill_rows()

    def _render_skill_rows(self):
        for w in self._skills_box.winfo_children():
            w.destroy()
        n = len(self._skill_vars)
        for idx, v in enumerate(self._skill_vars):
            row = tk.Frame(self._skills_box)
            row.pack(fill="x", pady=1)
            tk.Checkbutton(row, variable=v["enabled"], width=2).pack(side="left", padx=1)
            tk.Entry(row, textvariable=v["key"], width=5).pack(side="left", padx=1)
            tk.Spinbox(row, from_=0, to=100, textvariable=v["target_hp_above"],
                       width=5).pack(side="left", padx=1)
            tk.Spinbox(row, from_=0, to=100, textvariable=v["target_hp_below"],
                       width=5).pack(side="left", padx=1)
            tk.Spinbox(row, from_=0, to=100, textvariable=v["mp_min"],
                       width=5).pack(side="left", padx=1)
            tk.Spinbox(row, from_=0.0, to=600.0, increment=0.5,
                       textvariable=v["cooldown"], width=5).pack(side="left", padx=1)
            tk.Checkbutton(row, variable=v["once"], width=2).pack(side="left", padx=1)
            ready_set = bool(v.get("ready_region"))
            tk.Button(row, text=("гот✓" if ready_set else "гот"), width=4,
                      fg=("#2e7d32" if ready_set else "black"),
                      command=lambda i=idx: self._capture_skill_ready(i)).pack(side="left", padx=1)
            tk.Button(row, text="↑", width=2,
                      command=lambda i=idx: self._move_skill_row(i, -1),
                      state=("disabled" if idx == 0 else "normal")).pack(side="left")
            tk.Button(row, text="↓", width=2,
                      command=lambda i=idx: self._move_skill_row(i, 1),
                      state=("disabled" if idx == n - 1 else "normal")).pack(side="left")
            tk.Button(row, text="✕", width=2,
                      command=lambda i=idx: self._del_skill_row(i)).pack(side="left")

    def _save_controls(self):
        # клавиши
        keys = {}
        for action, _ in KEY_ACTIONS:
            keys[action] = self._key_vars[action].get().strip() or None
        # способности (ротация)
        skills = []
        for i, v in enumerate(self._skill_vars, 1):
            try:
                above = max(0, min(100, int(v["target_hp_above"].get())))
                below = max(0, min(100, int(v["target_hp_below"].get())))
                mp = max(0, min(100, int(v["mp_min"].get())))
                cd = max(0.0, float(v["cooldown"].get()))
            except (tk.TclError, ValueError):
                self._ctrl_status.set("Проверь числовые поля способностей.")
                return
            if above > below:            # защита от перепутанных границ
                above, below = below, above
            skills.append({
                "key": v["key"].get().strip(),
                "label": f"Скилл {i}",
                "cooldown": cd,
                "target_hp_above": above,
                "target_hp_below": below,
                "mp_min": mp,
                "once": bool(v["once"].get()),
                "enabled": bool(v["enabled"].get()),
                "ready_region": v.get("ready_region"),
            })
        # банки (HP/MP)
        try:
            heal = {"enabled": bool(self._heal_vars["enabled"].get()),
                    "key": self._heal_vars["key"].get().strip() or None,
                    "hp_percent": max(1, min(99, int(self._heal_vars["pct"].get()))),
                    "cooldown": max(0.0, float(self._heal_vars["cooldown"].get()))}
            mppot = {"enabled": bool(self._mppot_vars["enabled"].get()),
                     "key": self._mppot_vars["key"].get().strip() or None,
                     "mp_percent": max(1, min(99, int(self._mppot_vars["pct"].get()))),
                     "cooldown": max(0.0, float(self._mppot_vars["cooldown"].get()))}
        except (tk.TclError, ValueError):
            self._ctrl_status.set("Проверь числовые поля лечения.")
            return
        # самобаффы
        buffs = []
        for v in self._buff_vars:
            try:
                cd = max(0.0, float(v["cooldown"].get()))
            except (tk.TclError, ValueError):
                self._ctrl_status.set("Проверь КД баффов.")
                return
            buffs.append({
                "label": v["label"].get().strip(),
                "key": v["key"].get().strip() or None,
                "cooldown": cd,
                "enabled": bool(v["enabled"].get()),
            })
        try:
            loot_min = max(1, int(self._loot_presses_min_var.get()))
        except (tk.TclError, ValueError):
            loot_min = config.LOOT_PRESSES_MIN
        try:
            loot_max = max(loot_min, int(self._loot_presses_max_var.get()))
        except (tk.TclError, ValueError):
            loot_max = max(loot_min, config.LOOT_PRESSES_MAX)
        settings.set("keys", keys)
        settings.set("skills", skills)
        settings.set("heal", heal)
        settings.set("mp_potion", mppot)
        settings.set("buffs", buffs)
        settings.set("loot_presses_min", loot_min)
        settings.set("loot_presses_max", loot_max)
        settings.apply_to_config()   # применить сразу (в т.ч. на работающем боте)
        self._ctrl_status.set(
            f"Сохранено: {len([s for s in skills if s['key']])} способностей, "
            f"{len([b for b in buffs if b['key']])} баффов, лечение и клавиши.")
        self._append_log("Клавиши, лечение, способности и баффы обновлены.")

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
        # Исключить оверлей из ЗАХВАТА ЭКРАНА (mss): иначе наши же рамки попадают
        # в следующий скриншот поверх иконок/мобов и ломают детекцию (мигание).
        # WDA_EXCLUDEFROMCAPTURE = 0x11 (Windows 10 2004+); окно остаётся видимым
        # глазом, но невидимо для скриншотов.
        try:
            user32.SetWindowDisplayAffinity(hwnd, 0x00000011)
        except Exception:
            pass

    def _overlay_panic(self):
        # хоткей F10 срабатывает из потока keyboard -> выполняем на главном потоке
        def _kill():
            self.debug_var.set(False)
            self._destroy_debug_overlay()
            self.zones_var.set(False)
            self._destroy_zones_overlay()
        self.root.after(0, _kill)

    def _destroy_debug_overlay(self):
        self._overlay_stop.set()
        if self.debug_overlay is not None:
            try:
                self.debug_overlay.destroy()
            except Exception:
                pass
        self.debug_overlay = None
        self.debug_canvas = None

    # --- оверлей выделенных зон (статичный, для проверки калибровки) ---
    def _toggle_zones_overlay(self):
        if self.zones_var.get():
            self._create_zones_overlay()
        else:
            self._destroy_zones_overlay()

    def _create_zones_overlay(self):
        if self.zones_overlay is not None:
            return
        ov = tk.Toplevel(self.root)
        ov.attributes("-fullscreen", True)
        ov.attributes("-topmost", True)
        ov.configure(bg="black")
        try:
            ov.attributes("-transparentcolor", "black")
        except tk.TclError:
            pass
        cv = tk.Canvas(ov, bg="black", highlightthickness=0)
        cv.pack(fill="both", expand=True)
        try:
            self._make_clickthrough(ov)
        except Exception as e:
            ov.destroy()
            self.zones_var.set(False)
            self._append_log(f"[!] Клик-сквозь зон не удался ({e}). Оверлей отключён.")
            return
        self.zones_overlay = ov
        self.zones_canvas = cv
        self._draw_zones()
        self.root.after(1000, self._zones_refresh)   # периодически перерисовывать
        self._append_log("Оверлей зон включён (F10 — аварийно убрать).")

    def _zones_refresh(self):
        if self.zones_overlay is None:
            return
        self._draw_zones()
        self.root.after(1000, self._zones_refresh)

    def _draw_zones(self):
        cv = self.zones_canvas
        if cv is None:
            return
        cv.delete("all")
        zones = [
            ("HP", settings.get("bar_hp"), "#ff5252"),
            ("MP", settings.get("bar_mp"), "#448aff"),
            ("CP", settings.get("bar_cp"), "#ffd740"),
            ("Цель HP", settings.get("bar_target"), "#ff9100"),
            ("Имя цели", settings.get("target_name_region"), "#18ffff"),
            ("Поиск мобов", settings.get("search_region"), "#69f0ae"),
            ("Персонаж", settings.get("character_anchor"), "#ea80fc"),
            ("Баффы", settings.get("buff_region"), "#ffffff"),
        ]
        for label, r, color in zones:
            if not r:
                continue
            l, t = int(r["left"]), int(r["top"])
            w, h = int(r["width"]), int(r["height"])
            cv.create_rectangle(l, t, l + w, t + h, outline=color, width=2)
            cv.create_text(l + 2, max(6, t - 8), text=label, fill=color,
                           anchor="w", font=("Segoe UI", 8, "bold"))
        self._draw_buff_icons(cv)

    def _draw_buff_icons(self, cv):
        """Рамки иконок самобаффов: зелёная — бафф найден (висит), список красным
        снизу — какие баффы сейчас НЕ найдены (спали)."""
        buffs = settings.get("buffs") or []
        breg = settings.get("buff_region")
        if not buffs or not breg:
            return
        try:
            with ScreenCapture() as cap:
                frame = cap.grab()
        except Exception:
            return
        missing = []
        for b in buffs:
            if not b.get("enabled") or not b.get("label"):
                continue
            label = b["label"]
            score, box = targets.locate_buff(frame, breg, label)
            if box and score >= config.BUFF_MATCH_THRESHOLD:
                l, t, w, h = box
                cv.create_rectangle(l, t, l + w, t + h, outline="#00e676", width=2)
                cv.create_text(l + 2, max(6, t - 8), text=label, fill="#00e676",
                               anchor="w", font=("Segoe UI", 8, "bold"))
            else:
                missing.append(label)
        if missing:
            cv.create_text(breg["left"] + 2, breg["top"] + breg["height"] + 10,
                           text="нет баффа: " + ", ".join(missing), fill="#ff5252",
                           anchor="w", font=("Segoe UI", 8, "bold"))

    def _destroy_zones_overlay(self):
        if self.zones_overlay is not None:
            try:
                self.zones_overlay.destroy()
            except Exception:
                pass
        self.zones_overlay = None
        self.zones_canvas = None

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

    def _save_general_settings(self):
        """
        Сохранить ОБЩИЕ (непрофильные) настройки при выходе — чтобы галки/поля,
        у которых нет мгновенного сохранения, оставались при следующем запуске
        (напр. галка уведомления о пробитом CP). Остальные тумблеры сохраняются
        сразу при изменении.
        """
        try:
            if hasattr(self, "tg_enabled"):
                settings.set("death_notify", bool(self.tg_enabled.get()))
            if hasattr(self, "cp_notify"):
                settings.set("cp_notify", bool(self.cp_notify.get()))
            if hasattr(self, "tg_token"):
                settings.set("telegram_token", self.tg_token.get().strip())
            if hasattr(self, "tg_chat"):
                settings.set("telegram_chat_id", self.tg_chat.get().strip())
        except Exception:
            pass

    def on_close(self):
        self._save_general_settings()
        if self.worker:
            self.worker.stop()
        self._unregister_hotkeys()
        self._destroy_debug_overlay()
        self._destroy_zones_overlay()
        self.root.after(200, self.root.destroy)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
