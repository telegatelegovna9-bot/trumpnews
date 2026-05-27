# Truth Social → Telegram Bot

Мониторинг постов Donald Trump на Truth Social с автоматической отправкой в Telegram.

## Что делает бот

- 🖼️ **Скриншот** каждого нового поста (как на сайте)
- 🇷🇺 **Перевод** на русский язык
- 🎭 **Sentiment-анализ** тональности (позитив/негатив/нейтрально)
- 🔗 Ссылка на оригинальный пост
- ♻️ Работает 24/7, проверяет каждые 5 минут

## Пример сообщения в Telegram

```
[СКРИНШОТ ПОСТА]

🔴 Пост из Truth Social
📅 2025-01-15 | 🟢 Тон: позитивный

🇷🇺 Перевод:
Отличные новости для Америки! Наша экономика процветает как никогда раньше!

🔗 Открыть оригинал
```

## Как работает

1. **Playwright** (headless Chrome) загружает страницу профиля Truth Social
2. Извлекает текст и делает **скриншот** каждого поста
3. Переводит текст на русский через Google Translate
4. Отправляет **скриншот + перевод** в Telegram
5. SQLite хранит ID отправленных постов — дубликатов не будет

> Playwright — это настоящий браузер, поэтому обходит Cloudflare и все блокировки.

## Быстрый старт

### 1. Создай Telegram бота

1. [@BotFather](https://t.me/BotFather) → `/newbot` → получи токен
2. Добавь бота в группу/канал
3. Узнай Chat ID (см. ниже)

### 2. Узнай Chat ID и Thread ID

**Chat ID** — ID группы/канала:
- Для личных сообщений: напиши боту, открой `https://api.telegram.org/bot<TOKEN>/getUpdates`
- Для группы: используй [@RawDataBot](https://t.me/RawDataBot)

**Thread ID** (для группы с темами):
- Добавь [@RawDataBot](https://t.me/RawDataBot) в группу
- Напиши сообщение в нужной теме
- RawDataBot покажет `message_thread_id`

### 3. Настрой переменные

```bash
cp .env.example .env
```

```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_CHAT_ID=-1003156829333
TELEGRAM_THREAD_ID=44469
TRUTHSOCIAL_USERNAME=realDonaldTrump
POLL_INTERVAL_MINUTES=5
```

### 4. Запуск локально

```bash
pip install -r requirements.txt
playwright install chromium
python main.py
```

### 5. Деплой на Railway

1. Залей проект на GitHub
2. [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
3. Добавь переменные окружения в Settings → Variables
4. Railway соберёт Docker (с Playwright + Chromium) и запустит

> **Volume**: Для сохранения SQLite базы добавь Volume в Railway (Settings → Volumes → Mount Path: `/app`)

## Структура проекта

```
trump mode/
├── main.py              # Основной код бота
├── requirements.txt     # Python зависимости (включая Playwright)
├── Dockerfile           # Docker с Chromium для Railway
├── railway.toml         # Конфиг деплоя
├── .env.example         # Шаблон переменных
├── .gitignore
└── README.md
```

## Технологии

| Компонент | Технология |
|-----------|-----------|
| Браузер | Playwright (headless Chromium) |
| Перевод | Google Translate (deep-translator) |
| Sentiment | TextBlob |
| Планировщик | APScheduler |
| Хранение | SQLite |
| Деплой | Railway (Docker) |

## Дополнительные идеи

- [ ] Мониторинг нескольких аккаунтов
- [ ] Дайджест за день/неделю
- [ ] Фильтрация по ключевым словам
- [ ] Web dashboard для истории постов
- [ ] Видео-посты → скриншот превью + ссылка
