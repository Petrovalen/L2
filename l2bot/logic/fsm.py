"""
Конечный автомат логики бота.

Приоритеты каждый тик:
  1. ВЫЖИВАНИЕ  — HP ниже порога -> хилка; MP ниже порога -> мана.
                  Проверяется всегда, в любом состоянии.
  2. СОСТОЯНИЕ  — SEARCH -> COMBAT -> LOOT -> SEARCH.

Состояния:
  SEARCH  — цели нет. Ищем мобов, выбираем ближайшего (клавиша target_nearest
            или клик по найденному шаблону). Если цель не находится долго —
            периодически поворачиваем камеру, чтобы в поле зрения попали новые
            мобы (активный поиск, персонаж стоит на месте).
  COMBAT  — цель есть. Спамим атаку, пока цель жива и не вышел таймаут.
  LOOT    — цель умерла. Жмём pickup некоторое время, потом обратно в SEARCH.
"""
import time

import config
from vision import bars, targets, ocr
from control import input_ctl as ctl
from logic import mob_list, settings

SEARCH = "SEARCH"
COMBAT = "COMBAT"
LOOT = "LOOT"


class BotFSM:
    def __init__(self):
        self.state = SEARCH
        self._combat_started = 0.0
        self._loot_started = 0.0
        self._search_started = None   # когда вошли в поиск цели
        self._last_vision = 0.0       # последняя попытка визуального поиска
        self._target_lost_since = None  # с какого момента цель пропала (в бою)
        self._acquire_lock_until = 0.0  # до этого времени не перевыбираем цель
        self._last_name_check = 0.0     # последняя проверка имени цели (фильтр)
        self._vision_pending = False    # после вижн-клика ждём подтверждения цели
        self._wrong_target_count = 0    # подряд выбран не тот моб (некст-таргетом)
        self._last_camera = 0.0         # последний поворот камеры (активный поиск)
        self._target_hp_best = None     # минимальное HP цели за бой (прогресс урона)
        self._last_damage_at = 0.0      # когда последний раз HP цели упало
        self._last_click = None         # последняя точка вижн-клика (для «избегания»)
        self._avoid_point = None        # точка брошенной цели, которую не кликаем
        self._avoid_until = 0.0         # до какого момента избегаем avoid_point
        self._used_once = set()         # скиллы «раз за цель», уже применённые в этом бою
        self._last_buff_check = 0.0     # последняя проверка баффов
        self._buff_settle_until = 0.0   # пауза после каста баффа (прокаст)
        self._last_visible_check = 0.0  # последняя проверка «цель в кадре»
        self._view_lost_since = None    # с какого момента цель не видно в кадре
        self._last_view_rotate = 0.0    # последний доворот камеры к цели

    # ---- вспомогательные проверки ---------------------------------------
    def _survival(self, self_bars):
        """
        Лечение/банки — проверяется в любом состоянии. Пьём HP-банку, когда своё
        HP <= порога, и MP-банку, когда MP <= порога (каждая со своим кулдауном).
        Вернуть True, если что-то применено.
        """
        acted = False
        heal = config.HEAL
        if heal.get("enabled") and heal.get("key"):
            hp = self_bars.get("hp")
            if hp is not None and hp <= heal.get("hp_percent", 0):
                if ctl.press_skill("heal", heal["key"], heal.get("cooldown", 3.0)):
                    ctl.emit("лечение (HP %d%%)" % int(hp))
                    acted = True
        mpot = config.MP_POTION
        if mpot.get("enabled") and mpot.get("key"):
            mp = self_bars.get("mp")
            if mp is not None and mp <= mpot.get("mp_percent", 0):
                if ctl.press_skill("mp_potion", mpot["key"], mpot.get("cooldown", 3.0)):
                    ctl.emit("мана-банка (MP %d%%)" % int(mp))
                    acted = True
        return acted

    # ---- один тик автомата ----------------------------------------------
    def tick(self, frame, now):
        self_bars = bars.read_self_bars(frame)
        target_present, target_hp = bars.has_target(frame)

        # 1) выживание — вне зависимости от состояния
        self._survival(self_bars)

        # 2) машина состояний
        if self.state == SEARCH:
            self._on_search(frame, target_present, now)
        elif self.state == COMBAT:
            self._on_combat(frame, self_bars, target_present, target_hp, now)
        elif self.state == LOOT:
            self._on_loot(now)

        return {
            "state": self.state,
            "hp": self_bars.get("hp"),
            "mp": self_bars.get("mp"),
            "target": target_present,
            "target_hp": target_hp,
        }

    def _to_search(self, now):
        self.state = SEARCH
        self._search_started = now
        self._wrong_target_count = 0

    def _on_search(self, frame, target_present, now):
        if target_present:
            # Проверяем имя цели, если включён фильтр ИЛИ цель только что выбрана
            # визуальным кликом (вижн-клик всегда подтверждаем перед атакой:
            # убеждаемся, что выделился именно ожидаемый моб из списка).
            if config.TARGET_NAME_FILTER or self._vision_pending:
                if now - self._last_name_check < config.NAME_CHECK_INTERVAL:
                    return  # троттлим OCR/переключение
                self._last_name_check = now
                if not self._target_name_ok(frame):
                    self._vision_pending = False
                    self._wrong_target_count += 1
                    # Клавиша упорно даёт того же НЕ ТОГО моба -> после нескольких
                    # попыток переходим на ВИЗУАЛЬНЫЙ выбор (клик по нику из списка).
                    if self._wrong_target_count >= config.WRONG_TARGET_LIMIT:
                        if self._vision_click(frame):
                            ctl.emit("не тот моб — визуальный выбор")
                            self._vision_pending = True
                            self._acquire_lock_until = now + config.ACQUIRE_LOCK
                            self._wrong_target_count = 0
                        else:
                            # нужного ника не видно — доворачиваем камеру
                            ctl.emit("не тот моб, вижн пуст — доворот камеры")
                            ctl.camera_drag(config.CAMERA_DRAG_DISTANCE,
                                            center=self._camera_anchor())
                        return
                    ctl.emit("выделен не тот моб — переключаю")
                    self._switch_target()
                    return
            self._wrong_target_count = 0
            self._vision_pending = False
            self._enter_combat(now)
            return
        if self._search_started is None:
            self._search_started = now
        # только что кликнули визуально — ждём, пока цель зарегистрируется
        if now < self._acquire_lock_until:
            return
        # 0) САМОБАФФЫ (только вне боя): если бафф спал — накладываем и ждём прокаст,
        #    не начиная поиск цели (иначе клик по мобу прервёт каст).
        if now < self._buff_settle_until:
            return
        if self._maintain_buffs(frame, now):
            self._buff_settle_until = now + config.BUFF_CAST_SEC
            return
        self._vision_pending = False    # окно ожидания вижн-цели прошло (клик мимо)
        # 1) СНАЧАЛА обычный выбор ближайшей цели клавишей
        ctl.press_action("target_nearest")
        # 2) если ближняя цель не выбралась за SEARCH_VISION_AFTER — ПОДКЛЮЧАЕМ
        #    визуальный поиск ников из белого списка (OCR, троттлинг).
        if (config.VISION_TARGETING
                and now - self._search_started > config.SEARCH_VISION_AFTER
                and now - self._last_vision >= config.VISION_INTERVAL):
            self._last_vision = now
            if self._vision_click(frame):
                self._vision_pending = True
                self._acquire_lock_until = now + config.ACQUIRE_LOCK
                return
        # 3) активный поиск: цель не находится долго -> поворот камеры, чтобы в
        #    поле зрения попали новые мобы. Не крутим сразу после вижн-клика
        #    (_vision_pending) — даём цели зарегистрироваться.
        if (config.CAMERA_SEARCH and not self._vision_pending
                and now - self._search_started > config.SEARCH_CAMERA_AFTER
                and now - self._last_camera >= config.CAMERA_INTERVAL):
            self._last_camera = now
            ctl.emit("поворот камеры (поиск целей)")
            ctl.camera_drag(config.CAMERA_DRAG_DISTANCE, center=self._camera_anchor())

    def _camera_anchor(self):
        """
        Точка реколибровки курсора для поворота камеры — центр зоны поиска
        мобов (search_region): она заведомо над игровым миром, а не над UI.
        None -> camera_drag возьмёт центр экрана как запасной вариант.
        """
        region = settings.get("search_region")
        if not region:
            return None
        return (region["left"] + region["width"] // 2,
                region["top"] + region["height"] // 2)

    def _maintain_buffs(self, frame, now):
        """
        Поддержка самобаффов (вне боя). Раз в BUFF_CHECK_INTERVAL проверяем баффы;
        первый недостающий (иконки нет в зоне баффов) — накладываем и возвращаем
        True (вызывающий поставит паузу на прокаст). Один бафф за проход.
        """
        region = settings.get("buff_region")
        buffs = settings.get("buffs") or config.BUFFS
        if not region or not buffs:
            return False
        if now - self._last_buff_check < config.BUFF_CHECK_INTERVAL:
            return False
        self._last_buff_check = now
        for b in buffs:
            if not b.get("enabled") or not b.get("key"):
                continue
            label = b.get("label", "")
            if targets.buff_present(frame, region, label):
                continue
            if ctl.press_skill("buff_%s" % label, b["key"], b.get("cooldown", 3.0)):
                ctl.emit("бафф '%s' спал — накладываю" % (label or "бафф"))
                return True
        return False

    def _keep_target_visible(self, frame, now):
        """
        Держать выбранную цель в кадре. Если её ника не видно в зоне поиска
        (моб выбран клавишей «след. цель» и оказался вне экрана) — доворачиваем
        камеру, пока моб не появится. Пока моб виден — камеру не трогаем.
        """
        if not config.KEEP_TARGET_ON_SCREEN:
            return
        if now - self._last_visible_check < config.VISIBLE_CHECK_INTERVAL:
            return
        self._last_visible_check = now
        region = settings.get("search_region")
        names = mob_list.load()
        if not region or not names:
            return
        visible = (targets.find_mobs_by_template(frame, names, region)
                   or targets.find_named_mobs(frame, names, region))
        if visible:
            self._view_lost_since = None
            return
        # ника нужного моба в кадре нет
        if self._view_lost_since is None:
            self._view_lost_since = now
            return
        if now - self._view_lost_since < config.TARGET_VISIBLE_GRACE:
            return
        if now - self._last_view_rotate < config.CAMERA_INTERVAL:
            return
        self._last_view_rotate = now
        ctl.emit("цель вне экрана — довожу камеру")
        ctl.camera_drag(config.CAMERA_DRAG_DISTANCE, center=self._camera_anchor())

    def _target_name_ok(self, frame):
        """Имя выбранной цели есть в белом списке? Пустой список/нечитаемо -> ок."""
        names = mob_list.load()
        if not names:
            return True                      # фильтровать нечем
        text = ocr.read_target_name(frame)
        if not text:
            return True                      # не прочитали — не блокируем работу
        return targets.name_in_list(text, names)

    def _vision_click(self, frame):
        """Найти ник из белого списка на экране и кликнуть. True — кликнули."""
        region = settings.get("search_region")
        names = mob_list.load()
        if not region or not names:
            return False
        # Сначала — поиск по шаблонам ников (не срабатывает на траве). Если
        # шаблонов нет/не совпали — откат на OCR-скан по яркому тексту.
        mobs = targets.find_mobs_by_template(frame, names, region)
        how = "шаблон"
        if not mobs:
            mobs = targets.find_named_mobs(frame, names, region)
            how = "OCR"
        # не хватать сразу же брошенную (недостижимую) цель — пропускаем кандидатов
        # рядом с avoid_point, пока действует окно избегания.
        if self._avoid_point is not None and time.time() < self._avoid_until:
            ax, ay = self._avoid_point
            r2 = config.STUCK_AVOID_RADIUS ** 2
            mobs = [m for m in mobs
                    if (m["x"] - ax) ** 2 + (m["y"] - ay) ** 2 > r2]
        if not mobs:
            return False
        m = mobs[0]
        self._last_click = (m["x"], m["y"])
        ctl.emit(f"визуальный клик по '{m['name']}' ({how})")
        ctl.click(m["x"], m["y"])
        return True

    def _enter_combat(self, now):
        self.state = COMBAT
        self._combat_started = now
        self._target_lost_since = None
        self._target_hp_best = None      # сброс сторожа прогресса урона
        self._last_damage_at = now
        self._used_once = set()          # «раз за цель» — заново для новой цели
        self._view_lost_since = None     # сброс сторожа видимости цели
        self._last_visible_check = 0.0
        # человеческая реакция + запуск автоатаки (один раз, бьётся до смерти).
        ctl.reaction_delay()
        ctl.press_action("attack", respect_cooldown=False)

    def _on_combat(self, frame, self_bars, target_present, target_hp, now):
        # Фиксируемся на цели: одиночные сбойные кадры (цель «мигнула») не
        # выкидывают из боя. В лут уходим, только если цель пропала стабильно
        # дольше TARGET_LOST_GRACE — тогда считаем её мёртвой.
        if target_present:
            self._target_lost_since = None
            self._keep_target_visible(frame, now)   # доводим камеру, если моб вне кадра
        else:
            if self._target_lost_since is None:
                self._target_lost_since = now
            elif now - self._target_lost_since > config.TARGET_LOST_GRACE:
                # моб убит: его ник ещё виден (труп) — не даём вижн-поиску сразу
                # кликнуть по нему как по «ближайшему»
                if self._last_click is not None:
                    self._avoid_point = self._last_click
                    self._avoid_until = now + config.KILL_AVOID_SEC
                self.state = LOOT
                self._loot_started = now
                return
        # сторож прогресса урона: HP цели должно падать. Если за NO_DAMAGE_TIMEOUT
        # оно не упало на MIN_DAMAGE_PERCENT — бьём «в никуда» (препятствие / вне
        # линии видимости, «Cannot see target») -> бросаем цель и репозиционируемся.
        if target_present and target_hp is not None:
            if self._target_hp_best is None:
                self._target_hp_best = target_hp
                self._last_damage_at = now
            elif target_hp <= self._target_hp_best - config.MIN_DAMAGE_PERCENT:
                self._target_hp_best = target_hp          # урон пошёл — прогресс
                self._last_damage_at = now
            elif now - self._last_damage_at > config.NO_DAMAGE_TIMEOUT:
                self._give_up_target(now, "нет урона")
                return
        # жёсткий потолок времени боя (backstop, напр. если HP цели не читается)
        if now - self._combat_started > config.ATTACK_TIMEOUT:
            self._give_up_target(now, "таймаут боя")
            return
        # способности: кастуем те, чьи условия (HP цели / своя MP) выполнены.
        # Скилл прерывает автоатаку -> сразу после него возобновляем её.
        if self._use_skills(self_bars, target_hp):
            ctl.press_action("attack", respect_cooldown=False)
        else:
            # страховка: если автоатака оборвалась — переначинаем изредка
            # (интервал = кулдаун 'attack'). Спама атаки каждый тик больше нет.
            ctl.press_action("attack")

    def _switch_target(self):
        """
        Переключить цель В ИГРЕ на другую. Без этого после отказа моб остаётся
        выделенным, и SEARCH тут же снова входит в бой с ним же. Приоритет —
        отдельная клавиша «Следующая цель» (перебор мобов); если не задана,
        падаем на «ближайшую» (но она часто выбирает того же моба — поэтому для
        обхода застрявшей цели ЛУЧШЕ назначить клавишу next_target).
        """
        if config.KEYS.get("next_target"):
            ctl.press_action("next_target", respect_cooldown=False)
        else:
            ctl.press_action("target_nearest", respect_cooldown=False)

    def _give_up_target(self, now, reason):
        """
        Бросить цель (урон не идёт / таймаут) и не зациклиться на ней:
        переключить цель в игре, увеличенный поворот камеры (сменить сцену),
        кратковременный запрет вижн-клика по той же точке. Полностью проблему
        закроет шаг с перемещением (обход препятствия), пока — эти меры.
        """
        ctl.emit(f"меняю цель ({reason})")
        self._switch_target()
        if self._last_click is not None:
            self._avoid_point = self._last_click
            self._avoid_until = now + config.STUCK_AVOID_SEC
        if config.CAMERA_SEARCH:
            ctl.camera_drag(int(config.CAMERA_DRAG_DISTANCE * config.STUCK_TURN_MULT),
                            center=self._camera_anchor())
        self._to_search(now)

    def _use_skills(self, self_bars, target_hp):
        """
        Ротация: пройти список по порядку (порядок = приоритет) и применить
        ПЕРВУЮ подходящую способность. Условия:
          target_hp_above <= HP цели <= target_hp_below  И  моя MP >= mp_min
          (и вышел кулдаун; для «once» — ещё не применялась за этот бой).
        Один каст за тик — между кастами идёт автоатака. Вернуть True, если
        способность применена.
        """
        mp = self_bars.get("mp")
        mp = 100.0 if mp is None else mp
        thp = 100.0 if target_hp is None else target_hp
        for i, sk in enumerate(config.SKILLS):
            if not sk.get("enabled", True) or not sk.get("key"):
                continue
            # диапазон HP цели (target_hp_max — старое имя верхней границы)
            below = sk.get("target_hp_below", sk.get("target_hp_max", 100))
            above = sk.get("target_hp_above", 0)
            if not (above <= thp <= below):
                continue
            if mp < sk.get("mp_min", 0):
                continue
            cd_key = "skill_%d" % i
            if sk.get("once") and cd_key in self._used_once:
                continue
            if ctl.press_skill(cd_key, sk["key"], sk.get("cooldown", 4.0)):
                if sk.get("once"):
                    self._used_once.add(cd_key)
                ctl.emit("скилл '%s' (клавиша '%s')"
                         % (sk.get("label", "скилл"), sk["key"]))
                return True     # один каст за тик; далее возобновляется автоатака
        return False

    def _on_loot(self, now):
        ctl.press_action("pickup")
        if now - self._loot_started > config.LOOT_TIME:
            self._to_search(now)
