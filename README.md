# TON NFT Monitor (приватный Telegram-бот)

Два режима: **«Свежий апгрейд»** (новый NFT не выставлен или выставлен ниже флора) и **«Забытые NFT»** (нет движения дольше N дней). Бот отвечает только пользователю с `ADMIN_ID`.

## 1. Что нужно подготовить
1. Токен бота — у [@BotFather](https://t.me/BotFather).
2. Ваш user id — у [@userinfobot](https://t.me/userinfobot) → `ADMIN_ID`.
3. Ключ Tonapi (бесплатно) — https://tonconsole.com → `TONAPI_KEY`. Без ключа работает, но лимит около 1 запроса/сек.
4. Адреса коллекций — со страницы коллекции на Getgems/Tonviewer. У Telegram Gifts каждый вид подарка — своя коллекция.

## 2. Локальный запуск
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```
Создайте файл `.env` рядом с `main.py`:
```
BOT_TOKEN=123456:ABC...
ADMIN_ID=123456789
TONAPI_KEY=...
UPGRADE_COLLECTIONS=EQ...,EQ...
# необязательно:
# FORGOTTEN_COLLECTIONS=EQ...
# HOLD_DAYS=180
# UNDERPRICE_PERCENT=15
```
Запуск: `python main.py`. В Telegram отправьте боту `/start`.

## 3. Команды
- `/status` — настройки
- `/forgotten <адрес коллекции или кошелька>` — ручной скан забытых NFT (идёт в фоне)
- `/hold 365` — порог удержания в днях

## 4. Деплой на Render (бесплатно)
1. Залейте папку в приватный GitHub-репозиторий (**без** `.env`).
2. Render → New → **Web Service** → выберите репозиторий.
3. Runtime: Python 3. Build: `pip install -r requirements.txt`. Start: `python main.py`. Plan: Free.
4. Environment: добавьте `BOT_TOKEN`, `ADMIN_ID`, `TONAPI_KEY`, `UPGRADE_COLLECTIONS`.
5. Бесплатный Web Service засыпает через ~15 минут без HTTP-трафика. Бот отдаёт `ok` на `/`, поэтому заведите пинг каждые 5 минут на `https://<ваш-сервис>.onrender.com/` (например, UptimeRobot).
6. Диск на free-плане временный: при перезапуске `bot.db` очищается. Бот просто заново запомнит текущие NFT как «старые» (первый проход тихий), а повторные уведомления о забытых возможны.

## 5. Деплой на Railway
New Project → Deploy from GitHub → в Variables те же переменные → Start Command `python main.py`. Постоянной бесплатной тарифной сетки у Railway нет (пробный кредит), для долгой работы подойдёт и любой дешёвый VPS: `nohup python main.py &` или systemd.

## 6. Важные оговорки
- Эндпоинты Tonapi (`/v2/nfts/collections/{id}`, `.../items`, `/v2/accounts/{id}/events`, `.../dns/backresolve`) и поле `next_item_index` я писал по памяти; в окружении сборки не было сети, поэтому **код не запускался**. Проверьте на своей коллекции, при расхождениях в ответе смотрите https://tonapi.io/api-v2.
- Детектор апгрейдов строится на предположении, что новые NFT добавляются в конец коллекции. Если у нужной коллекции нумерация другая, увеличьте `UPGRADE_LOOKBACK`.
- «Флор» считается грубо (минимальная цена среди первых `FLOOR_SCAN_LIMIT` NFT) и не учитывает редкость модели/фона. Редкие атрибуты стоят дороже флора, так что оценивайте цену по атрибутам сами.
- Ссылка на Fragment строится эвристически по имени вида `Name #123`, Telegram-username владельца виден только если у кошелька есть домен `*.t.me`.
