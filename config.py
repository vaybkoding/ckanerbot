"""
config.py — все настройки бота.

Значения берутся из переменных окружения (на Render/Railway задаются в панели),
а при локальном запуске — из файла .env рядом с main.py.
"""
import os

try:
    from dotenv import load_dotenv  # необязательная зависимость для локального запуска
    load_dotenv()
except ImportError:
    pass


def _list(name: str) -> list[str]:
    """Читает переменную окружения вида 'a,b,c' и возвращает список без пустых элементов."""
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


# --- Telegram ---------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")          # токен от @BotFather
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)  # ваш Telegram user id (узнать: @userinfobot)

# --- TON API (tonapi.io) ----------------------------------------------------
# Ключ бесплатно создаётся на https://tonconsole.com. Без ключа работает, но лимит ~1 запрос/сек.
TONAPI_KEY = os.getenv("TONAPI_KEY", "")
TONAPI_BASE = os.getenv("TONAPI_BASE", "https://tonapi.io")
# Пауза между запросами (сек). Без ключа держим >1с, с ключом можно уменьшить.
REQUEST_INTERVAL = float(os.getenv("REQUEST_INTERVAL", "0.35" if TONAPI_KEY else "1.2"))

# --- Что мониторить ---------------------------------------------------------
# Адреса коллекций через запятую. У Telegram Gifts каждый тип подарка (Plush Pepe и т.д.)
# — отдельная коллекция. Адрес берите со страницы коллекции на Getgems / Tonviewer.
UPGRADE_COLLECTIONS = _list("EQBlBJ4n01pmYez5VPd8Wo598s8agbQCyVOjucXKxLDAi9r7", "EQDc08YxzZWtlKAohSybNc3kXAkAPPtHch-jY_E6KMQ3b1mn")
# Коллекции для автоскана «забытых NFT» (по умолчанию те же).
FORGOTTEN_COLLECTIONS = _list("EQBlBJ4n01pmYez5VPd8Wo598s8agbQCyVOjucXKxLDAi9r7", "EQDc08YxzZWtlKAohSybNc3kXAkAPPtHch-jY_E6KMQ3b1mn") or UPGRADE_COLLECTIONS

# --- Параметры «Свежий апгрейд» --------------------------------------------
UPGRADE_POLL_SECONDS = int(os.getenv("UPGRADE_POLL_SECONDS", "45"))  # как часто опрашивать коллекции
UPGRADE_LOOKBACK = int(os.getenv("UPGRADE_LOOKBACK", "100"))         # сколько последних items смотреть за раз
UNDERPRICE_PERCENT = float(os.getenv("UNDERPRICE_PERCENT", "15"))    # «дёшево» = ниже флора на N%
FLOOR_SCAN_LIMIT = int(os.getenv("FLOOR_SCAN_LIMIT", "500"))         # сколько items сканировать для флора

# --- Параметры «Забытые NFT» -----------------------------------------------
HOLD_DAYS = int(os.getenv("HOLD_DAYS", "180"))                 # порог удержания по умолчанию (дней)
MAX_SCAN_ITEMS = int(os.getenv("MAX_SCAN_ITEMS", "150"))       # максимум NFT за один скан
FORGOTTEN_SCAN_EVERY_HOURS = int(os.getenv("FORGOTTEN_SCAN_EVERY_HOURS", "24"))
FORGOTTEN_TOP = int(os.getenv("FORGOTTEN_TOP", "15"))          # сколько результатов показывать

# --- Прочее -----------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", "bot.db")
PORT = int(os.getenv("PORT", "8080"))  # Render/Railway передают порт сами (для health-check)


def validate() -> None:
    """Падает с понятным сообщением, если обязательные настройки не заданы."""
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN")
    if not ADMIN_ID:
        raise SystemExit("Не задан ADMIN_ID (ваш Telegram user id)")
