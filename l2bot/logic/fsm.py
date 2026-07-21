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
from vision import bars, targets
from control import input_ctl as ctl

SEARCH = "SEARCH"
COMBAT = "COMBAT"
LOOT = "LOOT"


class BotFSM:
    def __init__(self):
        self.state = SEARCH
        self._combat_started = 0.0
        self._loot_started = 0.0

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

    def _on_search(self, frame, target_present, now):
        if target_present:
            self._enter_combat(now)
            return
        # пробуем выбрать ближайшую цель клавишей
        ctl.press_action("target_nearest")
        # если есть шаблоны мобов — можно ещё кликнуть по ближайшему
        mobs = targets.find_mobs(frame)
        if mobs:
            m = mobs[0]
            ctl.click(m["x"], m["y"])

    def _enter_combat(self, now):
        self.state = COMBAT
        self._combat_started = now

    def _on_combat(self, target_present, now):
        # цель умерла/пропала -> лут
        if not target_present:
            self.state = LOOT
            self._loot_started = now
            return
        # таймаут боя (цель недостижима/убегает) -> сброс
        if now - self._combat_started > config.ATTACK_TIMEOUT:
            self.state = SEARCH
            return
        ctl.press_action("attack")

    def _on_loot(self, now):
        ctl.press_action("pickup")
        if now - self._loot_started > config.LOOT_TIME:
            self.state = SEARCH
