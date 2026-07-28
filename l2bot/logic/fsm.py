"""
Конечный автомат логики бота.

Приоритеты каждый тик:
  1. ВЫЖИВАНИЕ  — HP ниже порога -> хилка; MP ниже порога -> мана.
                  Проверяется всегда, в любом состоянии.
  2. СОСТОЯНИЕ  — SEARCH -> COMBAT -> LOOT -> SEARCH.

Состояния:
  SEARCH  — цели нет. Ищем мобов, выбираем ближайшего (клавиша target_nearest
            или клик по найденному шаблону).
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

    # ---- вспомогательные проверки ---------------------------------------
    def _survival(self, self_bars):
        """Вернуть True, если совершено действие выживания (пьём/хилимся)."""
        acted = False
        if self_bars.get("hp", 100) < config.HP_HEAL_THRESHOLD:
            ctl.press_action("heal_potion")
            acted = True
        if self_bars.get("mp", 100) < config.MP_MIN_THRESHOLD:
            ctl.press_action("mana_potion")
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
            self._on_combat(target_present, now)
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
                    ctl.emit("выделен не тот моб — переключаю")
                    ctl.press_action("target_nearest", respect_cooldown=False)
                    self._vision_pending = False
                    return
            self._vision_pending = False
            self._enter_combat(now)
            return
        if self._search_started is None:
            self._search_started = now
        # только что кликнули визуально — ждём, пока цель зарегистрируется
        if now < self._acquire_lock_until:
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
        mobs = targets.find_named_mobs(frame, names, region)
        if not mobs:
            return False
        m = mobs[0]
        ctl.emit(f"визуальный клик по '{m['name']}'")
        ctl.click(m["x"], m["y"])
        return True

    def _enter_combat(self, now):
        self.state = COMBAT
        self._combat_started = now
        self._target_lost_since = None
        # человеческая реакция + запуск автоатаки (один раз, бьётся до смерти).
        ctl.reaction_delay()
        ctl.press_action("attack", respect_cooldown=False)

    def _on_combat(self, target_present, now):
        # Фиксируемся на цели: одиночные сбойные кадры (цель «мигнула») не
        # выкидывают из боя. В лут уходим, только если цель пропала стабильно
        # дольше TARGET_LOST_GRACE — тогда считаем её мёртвой.
        if target_present:
            self._target_lost_since = None
        else:
            if self._target_lost_since is None:
                self._target_lost_since = now
            elif now - self._target_lost_since > config.TARGET_LOST_GRACE:
                self.state = LOOT
                self._loot_started = now
                return
        # таймаут боя (цель недостижима/убегает) -> сброс
        if now - self._combat_started > config.ATTACK_TIMEOUT:
            self._to_search(now)
            return
        # доп. скилл прерывает автоатаку -> сразу после него возобновляем её.
        if ctl.press_action("assist_skill"):
            ctl.press_action("attack", respect_cooldown=False)
        else:
            # страховка: если автоатака оборвалась — переначинаем изредка
            # (интервал = кулдаун 'attack'). Спама атаки каждый тик больше нет.
            ctl.press_action("attack")

    def _on_loot(self, now):
        ctl.press_action("pickup")
        if now - self._loot_started > config.LOOT_TIME:
            self._to_search(now)
