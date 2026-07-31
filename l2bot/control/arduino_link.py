"""
Связь с Arduino-мостом ввода (скетч l2bot/arduino/l2bot_hid).

Arduino с нативным USB-HID (Leonardo/Micro/Pro Micro, ATmega32U4) шлёт нажатия
как НАСТОЯЩАЯ клавиатура/мышь — игра принимает их даже когда блокирует
синтетический ввод (SendInput). Здесь — питон-сторона: находим порт, открываем
serial, шлём построчные команды (см. протокол в .ino).

Модуль не роняет бота: если Arduino не подключён/не отвечает — методы просто
возвращают False, а вызывающий код может откатиться на обычный ввод.
"""
import threading
import time

import config

try:
    import serial
    from serial.tools import list_ports
except Exception:                       # pyserial не установлен
    serial = None
    list_ports = None

_ARDUINO_VIDS = {0x2341, 0x2A03, 0x1B4F}   # Arduino LLC / Arduino SA / SparkFun (32u4)


class ArduinoLink:
    def __init__(self):
        self._ser = None
        self._lock = threading.Lock()
        self.port = None
        self.ready = False              # прошёл хендшейк (наша прошивка)

    # ---- поиск порта ----------------------------------------------------
    def _find_port(self):
        # 1) явно заданный в config
        want = getattr(config, "ARDUINO_PORT", None)
        if want:
            return want
        if not list_ports:
            return None
        # 2) первый порт с VID Arduino/совместимого HID-чипа
        for p in list_ports.comports():
            if p.vid in _ARDUINO_VIDS:
                return p.device
        # 3) фолбэк: описание содержит намёк
        for p in list_ports.comports():
            d = (p.description or "").lower()
            if any(k in d for k in ("arduino", "leonardo", "micro", "usb serial")):
                return p.device
        return None

    # ---- подключение ----------------------------------------------------
    def connect(self):
        """Открыть порт и проверить прошивку (PING->PONG). True — готово к работе."""
        if serial is None:
            return False
        with self._lock:
            if self.ready and self._ser and self._ser.is_open:
                return True
            self._close_locked()
            port = self._find_port()
            if not port:
                return False
            baud = getattr(config, "ARDUINO_BAUD", 115200)
            try:
                self._ser = serial.Serial(port, baud, timeout=0.4, write_timeout=1.0)
            except Exception:
                self._ser = None
                return False
            self.port = port
            # ATmega32U4 перезагружается при открытии порта — ждём старт скетча
            time.sleep(2.0)
            try:
                self._ser.reset_input_buffer()
            except Exception:
                pass
            self.ready = self._handshake_locked()
            return self.ready

    def _handshake_locked(self):
        """Спросить PING, дождаться PONG (или увидеть баннер L2HID)."""
        try:
            self._ser.write(b"PING\n")
            self._ser.flush()
        except Exception:
            return False
        end = time.monotonic() + 2.0
        while time.monotonic() < end:
            try:
                line = self._ser.readline().decode("ascii", "ignore").strip()
            except Exception:
                return False
            if line in ("PONG", "L2HID"):
                return True
        return False

    def _close_locked(self):
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None
        self.ready = False

    def close(self):
        with self._lock:
            self._close_locked()

    def is_connected(self):
        return bool(self.ready and self._ser and self._ser.is_open)

    # ---- отправка команды ----------------------------------------------
    def _send(self, cmd, wait_ok=True):
        """Отправить строку-команду. Возвращает True при успехе."""
        if not self.is_connected():
            return False
        with self._lock:
            try:
                self._ser.write((cmd + "\n").encode("ascii", "ignore"))
                self._ser.flush()
            except Exception:
                self._close_locked()
                return False
            if not wait_ok:
                return True
            # ждём подтверждение "OK" (короткий таймаут — не блокируем бота надолго)
            end = time.monotonic() + 1.5
            while time.monotonic() < end:
                try:
                    line = self._ser.readline().decode("ascii", "ignore").strip()
                except Exception:
                    return False
                if line == "OK":
                    return True
                if line == "PONG":
                    continue
            return True         # нет OK за таймаут — не считаем фатальным

    # ---- высокоуровневые действия --------------------------------------
    def key(self, name):
        return self._send("KEY %s" % name)

    def click(self):
        return self._send("CLICK")

    def rclick(self):
        return self._send("RCLICK")

    def move(self, dx, dy, duration_ms):
        return self._send("MOVE %d %d %d" % (int(dx), int(dy), int(duration_ms)))

    def drag(self, dx, dy, duration_ms):
        return self._send("DRAG %d %d %d" % (int(dx), int(dy), int(duration_ms)))

    def right_down(self):
        return self._send("RDOWN")

    def right_up(self):
        return self._send("RUP")


# Singleton
_link = None


def get_link():
    global _link
    if _link is None:
        _link = ArduinoLink()
    return _link
