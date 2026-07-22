"""
Очеловечивание поведения (анти-бан): планировщик "человеческих" перерывов.

BreakScheduler решает, когда пора сделать паузу (AFK) и сколько она длится.
Сам он времени не отслеживает через time.* — все методы принимают `now`
(монотонные секунды), чтобы логику было легко тестировать и чтобы решение о
старте перерыва принимал вызывающий (только в безопасный момент игры).

Типичное использование в цикле:
    if sched.due(now) and safe_moment:
        dur = sched.start(now)          # начали перерыв
    if sched.is_active():
        if sched.remaining(now) <= 0:   # или опасность
            sched.end(now)              # закончили, назначится следующий
"""
import random

import config


class BreakScheduler:
    def __init__(self, now):
        self._until = None          # монотонное время конца активного перерыва
        self._reschedule(now)

    def _reschedule(self, now):
        """Назначить момент следующего перерыва и погасить активный."""
        self._due_at = now + random.uniform(config.BREAK_EVERY_MIN,
                                             config.BREAK_EVERY_MAX)
        self._until = None

    def is_active(self):
        return self._until is not None

    def due(self, now):
        """Пора ли сделать перерыв (и он сейчас не активен)."""
        return self._until is None and now >= self._due_at

    def until_due(self, now):
        """Сколько секунд до следующего перерыва (0, если уже пора/активен)."""
        if self._until is not None:
            return 0.0
        return max(0.0, self._due_at - now)

    def start(self, now):
        """Начать перерыв. Возвращает его длительность (сек)."""
        dur = random.uniform(config.BREAK_DURATION_MIN, config.BREAK_DURATION_MAX)
        self._until = now + dur
        return dur

    def remaining(self, now):
        """Сколько секунд перерыва осталось (0, если не активен)."""
        return max(0.0, self._until - now) if self._until is not None else 0.0

    def end(self, now):
        """Завершить перерыв и назначить следующий."""
        self._reschedule(now)
