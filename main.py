"""
main.py — приватный Telegram-бот для мониторинга NFT / Telegram Gifts в TON.

Два режима:
  1) «Свежий апгрейд»  — следит за появлением новых NFT в коллекциях и сообщает,
                         если NFT ещё не выставлен на продажу или выставлен дешевле флора.
  2) «Забытые NFT»     — ищет NFT, которые не меняли владельца дольше N дней.

Бот отвечает ТОЛЬКО пользователю с id = config.ADMIN_ID, остальные апдейты игнорируются.
"""
import asyncio
import base64
import html
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp
import aiosqlite
from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import LinkPreviewOptions, Message

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("nft-bot")


# =============================================================================
# Утилиты: адреса, ссылки, форматирование
# =============================================================================
def _crc16(data: bytes) -> int:
    """CRC16-XMODEM — нужен для сборки user-friendly адреса TON."""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def to_friendly(raw: Optional[str], bounceable: bool = True) -> str:
    """Переводит raw-адрес '0:abcd...' в вид 'EQ...' / 'UQ...'. Если уже friendly — возвращает как есть."""
    if not raw or ":" not in raw:
        return raw or ""
    wc, h = raw.split(":", 1)
    body = bytes([0x11 if bounceable else 0x51, int(wc) & 0xFF]) + bytes.fromhex(h)
    return base64.urlsafe_b64encode(body + _crc16(body).to_bytes(2, "big")).decode()


def esc(x: Any) -> str:
    """HTML-экранирование для parse_mode=HTML."""
    return html.escape(str(x))


def fmt_duration(seconds: float) -> str:
    """Человекочитаемая длительность: '1 г. 2 мес. 5 дн.' / '3 ч. 12 мин.'."""
    s = int(seconds)
    if s < 3600:
        return f"{s // 60} мин."
    if s < 86400:
        return f"{s // 3600} ч. {(s % 3600) // 60} мин."
    d = s // 86400
    years, d = divmod(d, 365)
    months, d = divmod(d, 30)
    parts = []
    if years:
        parts.append(f"{years} г.")
    if months:
        parts.append(f"{months} мес.")
    if d or not parts:
        parts.append(f"{d} дн.")
    return " ".join(parts)


def fmt_ts(ts: Optional[float]) -> str:
    """Unix-время -> 'YYYY-MM-DD HH:MM UTC'."""
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def nft_links(addr_raw: str, name: str = "", col_addr: Optional[str] = None) -> str:
    """Ссылки на NFT: Getgems, Tonscan, Tonviewer и Fragment."""
    a = to_friendly(addr_raw)
    links = [
        f'<a href="https://getgems.io/nft/{a}">Getgems</a>',
        f'<a href="https://tonscan.org/address/{a}">Tonscan</a>',
        f'<a href="https://tonviewer.com/{a}">Tonviewer</a>',
    ]
    # Подарки называются вроде "Plush Pepe #123" -> fragment.com/gift/plushpepe-123 (эвристика!)
    m = re.match(r"^(.+?)\s*#(\d+)$", (name or "").strip())
    if m:
        slug = re.sub(r"[^a-z0-9]", "", m.group(1).lower())
        if slug:
            links.append(f'<a href="https://fragment.com/gift/{slug}-{m.group(2)}">Fragment</a>')
    elif col_addr:
        links.append(f'<a href="https://fragment.com/gifts">Fragment</a>')
    return " · ".join(links)


# =============================================================================
# Клиент TON API (tonapi.io) с ограничением частоты и ретраями
# =============================================================================
class TonApi:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.headers = {"Authorization": f"Bearer {config.TONAPI_KEY}"} if config.TONAPI_KEY else {}

    async def get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        """GET-запрос к tonapi. Возвращает JSON или None (404 / исчерпаны попытки)."""
        for attempt in range(4):
            # Глобальный троттлинг, чтобы не словить 429.
            async with self._lock:
                wait = self._last + config.REQUEST_INTERVAL - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
            try:
                async with self.session.get(
                    config.TONAPI_BASE + path, params=params, headers=self.headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as r:
                    if r.status == 429:
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    if r.status in (400, 404):
                        return None
                    r.raise_for_status()
                    return await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("tonapi %s попытка %d: %s", path, attempt + 1, e)
                await asyncio.sleep(2 * (attempt + 1))
        return None

    # --- Обёртки над нужными эндпоинтами ---
    async def nft(self, addr: str) -> Optional[dict]:
        return await self.get(f"/v2/nfts/{addr}")

    async def collection(self, addr: str) -> Optional[dict]:
        return await self.get(f"/v2/nfts/collections/{addr}")

    async def collection_items(self, addr: str, limit: int, offset: int) -> list[dict]:
        d = await self.get(f"/v2/nfts/collections/{addr}/items", {"limit": limit, "offset": offset})
        return (d or {}).get("nft_items", [])

    async def account_nfts(self, addr: str, limit: int = 1000) -> list[dict]:
        d = await self.get(f"/v2/accounts/{addr}/nfts", {"limit": limit, "offset": 0, "indirect_ownership": "false"})
        return (d or {}).get("nft_items", [])

    async def tme_username(self, addr_raw: str) -> Optional[str]:
        """Пытается найти Telegram-username владельца через обратное разрешение DNS (*.t.me)."""
        d = await self.get(f"/v2/accounts/{addr_raw}/dns/backresolve")
        for dom in (d or {}).get("domains", []):
            if dom.endswith(".t.me"):
                return dom[: -len(".t.me")]
        return None

    async def last_transfer_ts(self, nft_addr: str) -> Optional[int]:
        """Время последнего перехода NFT (mint/transfer/purchase). Фолбэк — last_activity аккаунта."""
        d = await self.get(f"/v2/accounts/{nft_addr}/events", {"limit": 20})
        for ev in (d or {}).get("events", []):  # события идут от новых к старым
            for act in ev.get("actions", []):
                if act.get("type") in ("NftItemTransfer", "NftPurchase"):
                    return ev.get("timestamp")
        acc = await self.get(f"/v2/accounts/{nft_addr}")
        return (acc or {}).get("last_activity")


# =============================================================================
# Разбор NFT-объекта tonapi
# =============================================================================
def parse_nft(n: dict) -> dict:
    """Достаёт из ответа tonapi имя, атрибуты, владельца, цену продажи."""
    meta = n.get("metadata") or {}
    attrs = {}
    for a in meta.get("attributes", []) or []:
        key = str(a.get("trait_type", "")).strip().lower()
        if key:
            attrs[key] = a.get("value")
    sale = n.get("sale")
    price = None
    if sale and (sale.get("price") or {}).get("token_name", "TON") == "TON":
        try:
            price = int(sale["price"]["value"]) / 1e9
        except (KeyError, ValueError, TypeError):
            price = None
    return {
        "address": n.get("address", ""),
        "name": meta.get("name") or "NFT без названия",
        "model": attrs.get("model"),
        "pattern": attrs.get("pattern") or attrs.get("symbol"),
        "background": attrs.get("backdrop") or attrs.get("background"),
        "all_attrs": attrs,
        "owner": (n.get("owner") or {}).get("address"),
        "listed": bool(sale),
        "price": price,
        "collection": (n.get("collection") or {}).get("address"),
    }


# =============================================================================
# Основная логика мониторинга
# =============================================================================
class Monitor:
    def __init__(self, bot: Bot, api: TonApi):
        self.bot = bot
        self.api = api
        self.hold_days = config.HOLD_DAYS
        self._floor_cache: dict[str, tuple[float, Optional[float]]] = {}
        self._scan_lock = asyncio.Lock()  # чтобы не запускать два скана «забытых» одновременно

    # ---------- БД ----------
    async def db_init(self) -> None:
        """Создаёт таблицы: seen (уже виденные NFT коллекций) и forgotten_sent (уже отправленные «забытые»)."""
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute("CREATE TABLE IF NOT EXISTS seen (addr TEXT PRIMARY KEY, collection TEXT, ts REAL)")
            await db.execute("CREATE TABLE IF NOT EXISTS forgotten_sent (addr TEXT PRIMARY KEY, ts REAL)")
            await db.execute("CREATE TABLE IF NOT EXISTS baseline (collection TEXT PRIMARY KEY)")
            await db.commit()

    async def notify(self, text: str) -> None:
        """Отправка сообщения владельцу бота (без превью ссылок)."""
        try:
            await self.bot.send_message(config.ADMIN_ID, text, link_preview_options=LinkPreviewOptions(is_disabled=True))
        except Exception as e:  # noqa: BLE001
            log.error("Не удалось отправить сообщение: %s", e)

    # ---------- Флор коллекции ----------
    async def floor(self, collection: str) -> Optional[float]:
        """Грубый флор: минимальная цена (TON) среди выставленных NFT в первых N items. Кэш 10 минут."""
        cached = self._floor_cache.get(collection)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        prices: list[float] = []
        offset = 0
        while offset < config.FLOOR_SCAN_LIMIT:
            batch = await self.api.collection_items(collection, min(1000, config.FLOOR_SCAN_LIMIT - offset), offset)
            if not batch:
                break
            prices += [p["price"] for p in map(parse_nft, batch) if p["price"]]
            offset += len(batch)
        value = min(prices) if prices else None
        self._floor_cache[collection] = (time.time(), value)
        return value

    # ---------- Режим 1: свежие апгрейды ----------
    async def upgrade_loop(self) -> None:
        """Бесконечный цикл: опрашивает коллекции и ищет новые NFT."""
        if not config.UPGRADE_COLLECTIONS:
            log.warning("UPGRADE_COLLECTIONS пуст — мониторинг апгрейдов выключен")
            return
        while True:
            for col in config.UPGRADE_COLLECTIONS:
                try:
                    await self.check_collection_for_new(col)
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка проверки коллекции %s", col)
            await asyncio.sleep(config.UPGRADE_POLL_SECONDS)

    async def check_collection_for_new(self, col: str) -> None:
        """Берёт последние N items коллекции, находит ещё не виденные и оценивает их."""
        info = await self.api.collection(col)
        if not info:
            return
        # next_item_index — число/индекс следующего NFT; свежие items лежат в конце списка.
        total = int(info.get("next_item_index") or 0)
        offset = max(0, total - config.UPGRADE_LOOKBACK)
        items = await self.api.collection_items(col, config.UPGRADE_LOOKBACK, offset)
        if not items:
            return

        async with aiosqlite.connect(config.DB_PATH) as db:
            cur = await db.execute("SELECT 1 FROM baseline WHERE collection=?", (col,))
            first_run = await cur.fetchone() is None
            new_items = []
            for it in items:
                addr = it.get("address")
                cur = await db.execute("SELECT 1 FROM seen WHERE addr=?", (addr,))
                if await cur.fetchone() is None:
                    await db.execute("INSERT INTO seen VALUES (?,?,?)", (addr, col, time.time()))
                    new_items.append(it)
            if first_run:
                # Первый запуск: всё, что уже есть, считаем «старым» и не шлём уведомления.
                await db.execute("INSERT INTO baseline VALUES (?)", (col,))
                await db.commit()
                log.info("Коллекция %s: baseline из %d items", col, len(new_items))
                return
            await db.commit()

        for it in new_items:
            await self.evaluate_new_nft(it)

    async def evaluate_new_nft(self, raw_item: dict) -> None:
        """Решает, достоин ли свежий NFT уведомления: не выставлен или выставлен ниже флора."""
        full = await self.api.nft(raw_item["address"]) or raw_item
        n = parse_nft(full)
        fl = await self.floor(n["collection"]) if n["collection"] else None

        if not n["listed"]:
            verdict = "🟢 Не выставлен на продажу"
        elif n["price"] and fl and n["price"] < fl * (1 - config.UNDERPRICE_PERCENT / 100):
            verdict = f"🔥 Выставлен за {n['price']:.2f} TON — ниже флора (~{fl:.2f} TON) более чем на {config.UNDERPRICE_PERCENT:g}%"
        else:
            return  # выставлен по нормальной цене — неинтересно

        await self.notify(await self.format_card("🆕 <b>Свежий апгрейд / новый NFT</b>", n, verdict,
                                                 extra=f"⏱ Замечен: только что ({fmt_ts(time.time())})"))

    # ---------- Режим 2: забытые NFT ----------
    async def forgotten_loop(self) -> None:
        """Периодический автоскан забытых NFT по FORGOTTEN_COLLECTIONS."""
        if not config.FORGOTTEN_COLLECTIONS:
            log.warning("FORGOTTEN_COLLECTIONS пуст — автоскан забытых выключен (команда /forgotten работает)")
            return
        await asyncio.sleep(30)
        while True:
            for col in config.FORGOTTEN_COLLECTIONS:
                try:
                    await self.scan_forgotten(col, only_new=True)
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка скана забытых %s", col)
            await asyncio.sleep(config.FORGOTTEN_SCAN_EVERY_HOURS * 3600)

    async def load_items(self, target: str) -> list[dict]:
        """Если target — коллекция, берёт её items, иначе считает кошельком и берёт его NFT."""
        if await self.api.collection(target):
            items, offset = [], 0
            while len(items) < config.MAX_SCAN_ITEMS:
                batch = await self.api.collection_items(target, min(100, config.MAX_SCAN_ITEMS - len(items)), offset)
                if not batch:
                    break
                items += batch
                offset += len(batch)
            return items
        return (await self.api.account_nfts(target))[: config.MAX_SCAN_ITEMS]

    async def scan_forgotten(self, target: str, only_new: bool = False) -> int:
        """Ищет NFT без движения дольше self.hold_days. Возвращает число найденных."""
        async with self._scan_lock:
            items = await self.load_items(target)
            now = time.time()
            threshold = self.hold_days * 86400
            found: list[tuple[float, dict, int]] = []
            for it in items:
                ts = await self.api.last_transfer_ts(it["address"])
                if ts and now - ts >= threshold:
                    found.append((now - ts, parse_nft(it), ts))
            found.sort(key=lambda x: x[0], reverse=True)

            if only_new:
                async with aiosqlite.connect(config.DB_PATH) as db:
                    fresh = []
                    for f in found:
                        cur = await db.execute("SELECT 1 FROM forgotten_sent WHERE addr=?", (f[1]["address"],))
                        if await cur.fetchone() is None:
                            fresh.append(f)
                            await db.execute("INSERT INTO forgotten_sent VALUES (?,?)", (f[1]["address"], now))
                    await db.commit()
                found = fresh

            if not found:
                if not only_new:
                    await self.notify(f"Скан завершён: NFT без движения дольше {self.hold_days} дн. не найдено "
                                      f"(проверено {len(items)}).")
                return 0

            await self.notify(f"📦 <b>Забытые NFT</b>: найдено {len(found)} (проверено {len(items)}), "
                              f"порог {self.hold_days} дн. Показываю топ {min(len(found), config.FORGOTTEN_TOP)}.")
            for held, n, ts in found[: config.FORGOTTEN_TOP]:
                verdict = f"⏳ Держит без движения: <b>{fmt_duration(held)}</b>"
                extra = f"🔁 Последняя транзакция/перевод: {fmt_ts(ts)}"
                await self.notify(await self.format_card("💤 <b>Забытый NFT</b>", n, verdict, extra))
            return len(found)

    # ---------- Формат карточки ----------
    async def format_card(self, title: str, n: dict, verdict: str, extra: str = "") -> str:
        """Собирает единое сообщение: название, атрибуты, владелец, время, ссылки."""
        owner_line = "Владелец: неизвестен"
        if n["owner"]:
            of = to_friendly(n["owner"], bounceable=False)
            uname = await self.api.tme_username(n["owner"])
            tg = f' · <a href="https://t.me/{esc(uname)}">@{esc(uname)}</a>' if uname else ""
            owner_line = f'Владелец: <a href="https://tonviewer.com/{of}">{esc(of[:6])}…{esc(of[-6:])}</a>{tg}'
        price = f"\n💰 Цена: {n['price']:.2f} TON" if n.get("price") else ""
        return (
            f"{title}\n\n"
            f"<b>{esc(n['name'])}</b>\n"
            f"• Model: {esc(n['model'] or '—')}\n"
            f"• Pattern: {esc(n['pattern'] or '—')}\n"
            f"• Background: {esc(n['background'] or '—')}\n\n"
            f"{owner_line}\n{verdict}{price}\n{extra}\n\n"
            f"{nft_links(n['address'], n['name'], n.get('collection'))}"
        )


# =============================================================================
# Приватность: пропускаем только ADMIN_ID
# =============================================================================
class AdminOnly(BaseMiddleware):
    """Outer-middleware на уровне Update: чужие апдейты отбрасываются без ответа."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.id != config.ADMIN_ID:
            return None
        return await handler(event, data)


monitor: Monitor  # инициализируется в main()


# =============================================================================
# Команды бота
# =============================================================================
async def cmd_start(m: Message) -> None:
    """/start и /help — список команд."""
    await m.answer(
        "👋 <b>TON NFT Monitor</b>\n\n"
        "/status — текущие настройки\n"
        "/forgotten &lt;адрес коллекции или кошелька&gt; — скан «забытых» NFT\n"
        "/hold &lt;дней&gt; — изменить порог удержания\n\n"
        "Уведомления о свежих апгрейдах приходят автоматически."
    )


async def cmd_status(m: Message) -> None:
    """/status — показывает, что сейчас мониторится."""
    await m.answer(
        f"Коллекции (апгрейды): {len(config.UPGRADE_COLLECTIONS)}\n"
        f"Коллекции (забытые): {len(config.FORGOTTEN_COLLECTIONS)}\n"
        f"Опрос каждые {config.UPGRADE_POLL_SECONDS} с\n"
        f"Порог удержания: {monitor.hold_days} дн.\n"
        f"Порог «дешёвого» листинга: −{config.UNDERPRICE_PERCENT:g}% от флора\n"
        f"API-ключ tonapi: {'есть' if config.TONAPI_KEY else 'нет (низкий лимит)'}"
    )


async def cmd_hold(m: Message, command: CommandObject) -> None:
    """/hold 365 — меняет порог (в днях) для поиска забытых NFT."""
    if not command.args or not command.args.strip().isdigit():
        await m.answer("Использование: /hold 365")
        return
    monitor.hold_days = max(1, int(command.args.strip()))
    await m.answer(f"Порог удержания: {monitor.hold_days} дн.")


async def cmd_forgotten(m: Message, command: CommandObject) -> None:
    """/forgotten <адрес> — запускает скан в фоне (он долгий из-за лимитов API)."""
    target = (command.args or "").strip()
    if not target:
        await m.answer("Использование: /forgotten &lt;адрес коллекции или кошелька&gt;")
        return
    await m.answer(f"Запускаю скан (до {config.MAX_SCAN_ITEMS} NFT, это займёт несколько минут)…")

    async def job() -> None:
        try:
            await monitor.scan_forgotten(target)
        except Exception as e:  # noqa: BLE001
            log.exception("scan_forgotten")
            await monitor.notify(f"Ошибка скана: {esc(e)}")

    asyncio.create_task(job())


# =============================================================================
# Health-check сервер (нужен бесплатному Web Service на Render/Railway)
# =============================================================================
async def start_health_server() -> web.AppRunner:
    """Мини-HTTP сервер: отвечает 'ok' на GET /, чтобы платформа считала сервис живым."""
    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.PORT).start()
    return runner


# =============================================================================
# Точка входа
# =============================================================================
async def main() -> None:
    global monitor
    config.validate()

    session = aiohttp.ClientSession()
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    monitor = Monitor(bot, TonApi(session))
    await monitor.db_init()

    dp = Dispatcher()
    dp.update.outer_middleware(AdminOnly())  # приватность
    dp.message.register(cmd_start, Command("start", "help"))
    dp.message.register(cmd_status, Command("status"))
    dp.message.register(cmd_hold, Command("hold"))
    dp.message.register(cmd_forgotten, Command("forgotten"))

    runner = await start_health_server()
    tasks = [asyncio.create_task(monitor.upgrade_loop()), asyncio.create_task(monitor.forgotten_loop())]
    await monitor.notify("✅ Бот запущен.")
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
        await runner.cleanup()
        await session.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
