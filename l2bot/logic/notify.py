"""
Уведомления на телефон через Telegram-бота (без внешних зависимостей — stdlib).

Настройка (один раз):
  1. В Telegram напиши @BotFather -> /newbot -> получи ТОКЕН бота.
  2. Напиши своему боту любое сообщение (чтобы он мог тебе отвечать).
  3. Узнай свой chat_id: открой
     https://api.telegram.org/bot<ТОКЕН>/getUpdates  -> поле "chat":{"id":...}.
  4. Впиши токен и chat_id в панель (блок «Telegram»).
Отправка идёт в фоне и не роняет бота при ошибке сети.
"""
import threading
import urllib.parse
import urllib.request


def send_telegram(token, chat_id, text):
    """Отправить сообщение в Telegram (в фоне). Пустые токен/chat_id — no-op."""
    token = (token or "").strip()
    chat_id = str(chat_id or "").strip()
    if not token or not chat_id:
        return False

    def _worker():
        try:
            url = "https://api.telegram.org/bot%s/sendMessage" % token
            data = urllib.parse.urlencode(
                {"chat_id": chat_id, "text": text}).encode("utf-8")
            urllib.request.urlopen(url, data=data, timeout=10)
        except Exception:
            pass   # сеть/токен — не роняем бота

    threading.Thread(target=_worker, daemon=True).start()
    return True
