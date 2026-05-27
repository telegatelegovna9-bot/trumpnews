# Truth Social → Telegram Monitor

Мониторинг постов Donald Trump на Truth Social с автоматической отправкой в Telegram.

## Что делает бот

- 🔄 Каждые 5 минут проверяет новые посты на Truth Social
- 🇺🇸 → 🇷🇺 Автоматически переводит посты на русский
- 🎭 Sentiment-анализ тональности каждого поста (позитив/негатив/нейтрально)
- 📎 Показывает наличие медиа-вложений
- 🔗 Ссылка на оригинальный пост

## Пример сообщения

```
🔴 Новый пост Truth Social
📅 2025-01-15 | 🟢 Тон: позитивный

🇺🇸 Оригинал:
Great news for America! Our economy is booming like never before!

🇷🇺 Перевод:
Отличные новости для Америки! Наша экономика процветает как никогда раньше!

🔗 Открыть в Truth Social
```

## Быстрый старт

### 1. Создай Telegram бота

1. Открой [@BotFather](https://t.me/BotFather) в Telegram
2. Отправь `/newbot`
3. Следуй инструкциям, получи **токен бота**
4. Добавь бота в канал/группу или напиши ему `/start`
5. Узнай **Chat ID**:
   - Для личных сообщений: напиши боту, потом открой `https://api.telegram.org/bot<TOKEN>/getUpdates` — там будет `"chat":{"id":123456}`
   - Для канала: добавь бота как админа канала, потом используй `@channel_name` или числовой ID
   - **Для группы с темами (форум)**: см. инструкцию ниже

#### Группа с темами (форум)

Если группа разделена на темы/ветки:
1. Добавь бота в группу как администратора
2. **В нужной теме** напиши боту `/start`
3. Открой `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Найди `"message_thread_id": 123` — это ID темы
5. Добавь в `.env`:
   ```env
   TELEGRAM_THREAD_ID=123
   ```

> Без `TELEGRAM_THREAD_ID` бот пишет в **основной чат** группы (не в тему).

### 2. Настрой переменные окружения

Скопируй `.env.example` в `.env`:

```bash
cp .env.example .env
```

Заполни:
```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_CHAT_ID=123456789
TELEGRAM_THREAD_ID=123          # опционально, для группы с темами
TRUTHSOCIAL_USERNAME=realDonaldTrump
POLL_INTERVAL_MINUTES=5
```

### 3. Запуск локально

```bash
pip install -r requirements.txt
python main.py
```

### 4. Деплой на Railway

1. Залей проект на GitHub
2. Зайди на [railway.app](https://railway.app)
3. New Project → Deploy from GitHub repo
4. Добавь переменные окружения (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) в Settings → Variables
5. Railway автоматически соберёт Docker-образ и запустит

> **Volume для SQLite**: Чтобы база данных не сбрасывалась при рестарте, добавь Volume в Railway:
> Settings → Volumes → Mount Path: `/app` (или `/app/state.db` если Railway поддерживает файлы)

## Структура проекта

```
trump mode/
├── main.py              # Основной код бота
├── requirements.txt     # Python зависимости
├── Dockerfile           # Для деплоя на Railway
├── .env.example         # Шаблон переменных окружения
├── .env                 # Твои реальные переменные (не коммитить!)
└── state.db             # SQLite — хранит состояние (создаётся автоматически)
```

## Как работает

1. При первом запуске бот помечает существующие посты как «отправленные» (чтобы не спамить старыми)
2. Каждые 5 минут опрашивает Truth Social (с автоматическим fallback между методами):
   - **Прямой API** — Mastodon-совместимый API Truth Social (работает с residential IP)
   - **RSSHub** — публичный RSS-сервис (работает с серверов)
   - **Nitter** — зеркала Truth Social (резервный метод)
3. Новые посты → перевод → sentiment-анализ → отправка в Telegram
4. ID отправлен��ых постов хранятся в SQLite — дубликатов не будет

## Дополнительные идеи

- [ ] Мониторинг нескольких аккаунтов
- [ ] Дайджест за день/неделю
- [ ] Фильтрация по ключевым словам
- [ ] Web dashboard для истории постов
