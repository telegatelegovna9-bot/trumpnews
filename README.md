# Trump Truth Social → Telegram Monitor v2

Мониторинг постов Trump в Truth Social и пересылка в Telegram в **реальном времени**.

## Архитектура

| Приоритет | Метод | Задержка | Cloudflare |
|-----------|-------|----------|------------|
| 🥇 Primary | Playwright WebSocket Interception | 1-5 сек | Обходится |
| 🥈 Secondary | Playwright API Polling | ~60 сек | Обходится |
| 🥉 Fallback | truthbrush polling | ~60 сек | Обходится |

Все методы работают **параллельно**. Первый обнаруживший пост побеждает — дедупликация гарантирует отсутствие дублей.

## Что отправляется в Telegram

- 📸 Скриншот поста
- 📝 Оригинальный текст (с сохранением тона)
- 🇷🇺 Перевод на русский
- 🎭 Анализ тональности (позитивный/негативный/нейтральн��й)
- 🔗 Ссылка на оригинал

## Установка

```bash
pip install -r requirements.txt
playwright install chromium
```

## Настройка

```bash
cp .env.example .env
# Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID
```

## Запуск

```bash
python main.py
```

## Docker

```bash
docker build -t trump-monitor .
docker run -d --env-file .env --name trump-monitor trump-monitor
```

## Структура проекта

```
main.py              — Оркестратор (запускает всё параллельно)
models.py            — Модель поста
ws_listener.py       — WebSocket Streaming API listener
truthbrush_poller.py — truthbrush-based poller
playwright_ws.py     — Playwright с WS-перехватом и скриншотами
notifier.py          — Telegram уведомления
```
