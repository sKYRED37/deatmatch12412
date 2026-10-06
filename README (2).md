# Topoff DM

Telegram-бот лобби Standoff 2 (aiogram 3). Закрытый доступ, управление кнопками, публикация лобби в канал.

## Запуск на Bothost

1. Залейте репозиторий на GitHub (файлы: `bot.py`, `requirements.txt`, `.gitignore`, `.env.example`, `README.md`).
2. В Bothost создайте бота из репозитория, главный файл — `bot.py`.
3. В панели добавьте переменные окружения:
   - `BOT_TOKEN` — токен от @BotFather
   - `CHANNEL_ID` — `@username` канала или `-100...` (бот должен быть админом канала)
   - `ADMIN_IDS` — ваш Telegram ID (несколько через запятую)
4. Запустите/перезапустите бота.

## Локально

```
pip install -r requirements.txt
cp .env.example .env   # заполните значения
python bot.py
```
