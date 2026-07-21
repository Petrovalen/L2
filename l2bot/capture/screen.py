"""
Модуль захвата экрана. mss очень быстрый (~5-10 мс на кадр).
Возвращаем numpy-массив в формате BGR (как ждёт OpenCV).
"""
import numpy as np
import mss

import config


class ScreenCapture:
    """Обёртка над mss с переиспользуемым инстансом (так быстрее)."""

    def __init__(self, region=None, monitor_index=None):
        self.region = region if region is not None else config.CAPTURE_REGION
        self.monitor_index = monitor_index or config.MONITOR_INDEX
        self._sct = mss.mss()

    def grab(self):
        """Снять один кадр. Возвращает BGR numpy-массив HxWx3."""
        if self.region:
            monitor = self.region
        else:
            monitor = self._sct.monitors[self.monitor_index]
        raw = self._sct.grab(monitor)
        # mss отдаёт BGRA -> отбрасываем альфу, порядок каналов уже BGR.
        frame = np.asarray(raw)[:, :, :3]
        return frame

    def close(self):
        self._sct.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
