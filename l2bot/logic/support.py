"""
Поддержка ВТОРЫМ окном (dual-box).

Первое окно фармит обычным FSM. Этот контроллер — саппорт во ВТОРОМ окне:
следит за HP/MP ПЕРВОГО персонажа (полоски первого окна), за своими HP/MP
(полоски второго окна, bar_hp2/bar_mp2) и за баффами первого (баф-бар первого)
и по условиям кастует:
  • селф-хил окна 2  — когда HP второго <= порога;
  • хил первого      — когда HP первого <= порога;
  • мана первому     — когда MP первого <= порога;
  • баффы первого    — перебаф, если иконки нет на баф-баре первого.
Касты на первого не идут, если у окна 2 не хватает маны (порог dual_min_mp2).

Клавиши игра принимает только в АКТИВНОМ окне, поэтому на время каста фокус
переносится на окно 2 (клик по dual_focus_2), для каста на первого жмётся клавиша
выбора члена группы (dual_party_key). ПОСЛЕ каста жмётся отдельная клавиша
«следовать» (dual_follow_key) — чтобы окно 2 продолжило бежать за первым, — и
фокус возвращается на окно 1 (dual_focus_1).

Настройки (settings.json):
  dual_focus_1 / dual_focus_2 — {"x","y"} точки клика для активации окон;
  dual_party_key             — клавиша выбора первого персонажа в окне 2;
  dual_follow_key            — клавиша «следовать за целью» в окне 2;
  bar_hp2 / bar_mp2          — полоски HP/MP второго окна;
  buff_region                — баф-бар первого (общий с самобаффами первого);
  dual_min_mp2               — не кастовать на первого, если MP окна 2 <= % (0=выкл);
  dual_heal/dual_mana/dual_selfheal — {"enabled","key","percent","cooldown"};
  dual_buffs                 — [{"enabled","label","key"}] баффы, которые окно 2
                               держит на первом (иконка — шаблон "d2_<label>").
"""
import config
from vision import bars, targets
from control import input_ctl as ctl
from logic import settings

_BUFF_PREFIX = "d2_"      # префикс шаблона иконки баффа первого от окна 2 (без коллизий)
_SELFBUFF_PREFIX = "d2s_" # префикс шаблона иконки СЕЛФ-баффа окна 2


class SupportController:
    def __init__(self):
        self._ready = {}          # ключ действия -> monotonic-время следующего каста
        self._last_check = 0.0
        self._last_buff_check = 0.0
        self._warned_focus = False

    def maybe_act(self, frame, now, state=None):
        """Проверить состояние окон и, если надо, поддержать. state — состояние FSM
        окна 1 ('SEARCH'/'COMBAT'/...). Вызывать каждый тик; сам троттлит и уходит в
        фокус-дэнс только при реальной необходимости."""
        if not getattr(config, "DUAL_BOX_ENABLED", False):
            return
        if now - self._last_check < config.DUAL_CHECK_INTERVAL:
            return
        self._last_check = now
        # окно 1 в бою: после фокус-дэнса его таргет слетает -> при возврате нужно
        # заново взять ближайшего моба (next_target). Для хила/маны/селф-хила.
        in_combat = (state == "COMBAT")

        # 1) СЕЛФ-ХИЛ окна 2 (по его собственному HP) — приоритет, саппорт должен выжить.
        sh = settings.get("dual_selfheal") or {}
        if (sh.get("enabled") and sh.get("key") and now >= self._ready.get("selfheal", 0.0)
                and settings.get("bar_hp2")):
            hp2 = bars.read_self_bars(frame, "2").get("hp")
            if hp2 is not None and hp2 <= sh.get("percent", 50):
                if self._cast(sh["key"], select_main=False, retarget=in_combat):
                    self._ready["selfheal"] = now + sh.get("cooldown", 3.0)
                    ctl.emit("2-е окно: селф-хил (HP2 %d%%)" % int(hp2))
                return

        # 2/3) ХИЛ/МАНА ПЕРВОГО (по его HP/MP), с учётом маны окна 2. Можно и в бою.
        sb1 = None
        for metric, skey in (("hp", "dual_heal"), ("mp", "dual_mana")):
            spec = settings.get(skey) or {}
            if not spec.get("enabled") or not spec.get("key"):
                continue
            if now < self._ready.get(skey, 0.0):
                continue
            if sb1 is None:
                sb1 = bars.read_self_bars(frame)      # HP/MP первого окна
            val = sb1.get(metric)
            if val is None or val > spec.get("percent", 50):
                continue
            if not self._mp2_ok(frame):               # у окна 2 мало маны — не кастуем
                continue
            if self._cast(spec["key"], select_main=True, retarget=in_combat):
                self._ready[skey] = now + spec.get("cooldown", 3.0)
                ctl.emit("2-е окно: %s первого (%s %d%%)"
                         % (spec.get("label") or skey, metric.upper(), int(val)))
            return   # один каст за проход — фокус-дэнс дорогой

        # 4) БАФФЫ: ТОЛЬКО когда окно 1 в ПОИСКЕ (не в бою и не в луте) — перебаф не
        #    должен прерывать бой/сбор. Сначала СВОИ селф-баффы окна 2, затем баффы
        #    первого. Один каст за проход.
        if state == "SEARCH" and now - self._last_buff_check >= config.DUAL_BUFF_INTERVAL:
            self._last_buff_check = now
            if not self._maybe_selfbuff(frame):
                self._maybe_buff(frame)

    def _maybe_selfbuff(self, frame):
        """Один недостающий СЕЛФ-бафф окна 2 — наложить на себя. Иконки ищем на
        баф-баре окна 2 (buff_region2). True — что-то кастовали (или пытались)."""
        region = settings.get("buff_region2")
        buffs = settings.get("dual_selfbuffs") or []
        if not region or not buffs or not settings.get("bar_hp2") or not self._mp2_ok(frame):
            return False
        for b in buffs:
            if not b.get("enabled") or not b.get("key") or not b.get("label"):
                continue
            tlabel = _SELFBUFF_PREFIX + b["label"]
            if targets.buff_present(frame, region, tlabel):
                continue                              # бафф на месте (или нет шаблона)
            if self._cast_selfbuff(b["key"]):
                ctl.emit("2-е окно: селф-бафф «%s»" % b["label"])
            return True                               # один бафф за проход
        return False

    def _maybe_buff(self, frame):
        """Один недостающий бафф первого — наложить (фокус-дэнс). Иконки ищем на
        баф-баре первого (buff_region). Нет шаблона иконки -> бафф пропускаем."""
        region = settings.get("buff_region")
        buffs = settings.get("dual_buffs") or []
        if not region or not buffs or not self._mp2_ok(frame):
            return
        for b in buffs:
            if not b.get("enabled") or not b.get("key") or not b.get("label"):
                continue
            tlabel = _BUFF_PREFIX + b["label"]
            if targets.buff_present(frame, region, tlabel):
                continue                              # бафф на месте (или нет шаблона)
            if self._cast(b["key"], select_main=True):
                ctl.emit("2-е окно: бафф «%s» первому" % b["label"])
            return                                    # один бафф за проход

    def _mp2_ok(self, frame):
        """Хватает ли маны у окна 2 для каста на первого. True, если проверка
        выключена (dual_min_mp2=0) или полоска MP окна 2 не откалибрована."""
        min_mp2 = settings.get("dual_min_mp2") or 0
        if min_mp2 <= 0 or not settings.get("bar_mp2"):
            return True
        mp2 = bars.read_self_bars(frame, "2").get("mp")
        return mp2 is None or mp2 > min_mp2

    def _cast(self, skill_key, select_main, retarget=False):
        """Фокус-дэнс: активировать окно 2 -> выбрать цель (первого пати-клавишей
        ИЛИ себя кликом по своей полоске HP) -> каст -> follow -> вернуть фокус на
        окно 1. select_main=False — каст на СЕБЯ (клик по bar_hp2). retarget=True —
        окно 1 было в бою: при возврате сразу жмём next_target (таргет слетел).
        True — последовательность выполнена."""
        f1 = settings.get("dual_focus_1")
        f2 = settings.get("dual_focus_2")
        if not f1 or not f2:
            if not self._warned_focus:
                self._warned_focus = True
                ctl.emit("2-е окно: не заданы точки фокуса окон — задай в панели")
            return False
        self._warned_focus = False
        party = settings.get("dual_party_key")
        follow = settings.get("dual_follow_key")
        ctl.click(f2["x"], f2["y"])                   # активировать окно 2
        ctl.sleep(config.DUAL_FOCUS_SETTLE)
        if select_main:
            if party:                                 # выделить первого (пати-мембер)
                ctl.press_key(party)
                ctl.sleep(config.DUAL_TARGET_SETTLE)
        else:
            self._select_self2()                      # выделить СЕБЯ (клик по своей HP)
        ctl.press_key(skill_key)                      # каст
        ctl.sleep(config.DUAL_CAST_SETTLE)            # дать способности примениться
        # вернуть слежение за первым: (после селф-хила снова выделить первого) +
        # команда «следовать» (отдельная клавиша follow).
        if not select_main and party:
            ctl.press_key(party)
            ctl.sleep(config.DUAL_TARGET_SETTLE)
        if follow:
            ctl.press_key(follow)
            ctl.sleep(config.DUAL_TARGET_SETTLE)
        ctl.click(f1["x"], f1["y"])                   # вернуть фокус на окно 1
        if retarget:
            # окно 1 было в бою — его таргет мог слететь за время фокус-дэнса.
            # Сразу берём ближайшего моба, иначе оно бьёт «в никуда». Клавиша —
            # ОТДЕЛЬНАЯ (dual_retarget_key); если не задана, падаем на next_target
            # окна 1 (иначе на «ближайшую цель»).
            ctl.sleep(config.DUAL_FOCUS_SETTLE)
            rt = settings.get("dual_retarget_key")
            if rt:
                ctl.press_key(rt)
            elif not ctl.press_action("next_target", respect_cooldown=False):
                ctl.press_action("target_nearest", respect_cooldown=False)
        return True

    def _cast_selfbuff(self, skill_key):
        """Селф-бафф окна 2: активировать окно 2 -> выделить себя (клик по bar_hp2)
        -> каст -> ДВАЖДЫ нажать выбор первого (побежать за ним) -> вернуть фокус
        на окно 1. True — последовательность выполнена."""
        f1 = settings.get("dual_focus_1")
        f2 = settings.get("dual_focus_2")
        if not f1 or not f2:
            if not self._warned_focus:
                self._warned_focus = True
                ctl.emit("2-е окно: не заданы точки фокуса окон — задай в панели")
            return False
        self._warned_focus = False
        party = settings.get("dual_party_key")
        ctl.click(f2["x"], f2["y"])                   # активировать окно 2
        ctl.sleep(config.DUAL_FOCUS_SETTLE)
        self._select_self2()                          # выделить СЕБЯ
        ctl.press_key(skill_key)                      # каст селф-баффа
        ctl.sleep(config.DUAL_CAST_SETTLE)            # дать примениться
        if party:                                     # дважды таргет первого -> follow
            ctl.press_key(party)
            ctl.sleep(config.DUAL_TARGET_SETTLE)
            ctl.press_key(party)
            ctl.sleep(config.DUAL_TARGET_SETTLE)
        ctl.click(f1["x"], f1["y"])                   # вернуть фокус на окно 1
        return True

    def _select_self2(self):
        """Выделить СВОЙ персонаж в окне 2 — клик по своей полоске HP (bar_hp2).
        В L2 клик по своему бару HP выделяет собственного персонажа."""
        spec = settings.get("bar_hp2")
        if not spec:
            return False
        cx = spec["left"] + spec["width"] // 2
        cy = spec["top"] + spec["height"] // 2
        ctl.click(cx, cy)
        ctl.sleep(config.DUAL_TARGET_SETTLE)
        return True
