"""
Truth Social → Telegram Bot
Мониторит новые посты с Truth Social и отправляет в Telegram с переводом и sentiment-анализом.

Поддерживает несколько методов получения постов (автоматический fallback):
1. Прямой Truth Social API (Mastodon-compatible)
2. RSSHub (публичный RSS-сервис)
3. Nitter.poast.org (зеркало)
"""

import os
import re
import logging
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from deep_translator import GoogleTranslator
from textblob import TextBlob
from apscheduler.schedulers.background import BackgroundScheduler

# ─── Config ────────────────────────────────────────────────────────────────────

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_THREAD_ID = os.getenv("TELEGRAM_THREAD_ID")  # ID темы/ветки (опционально)
TRUTHSOCIAL_USERNAME = os.getenv("TRUTHSOCIAL_USERNAME", "realDonaldTrump")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_MINUTES", "5"))

DB_PATH = Path(__file__).parent / "state.db"

# HTTP-сессия с заголовками как у браузера
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("truth2tg")

# ─── Database ──────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_posts (
            post_id TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()
    log.info("Database initialized: %s", DB_PATH)


def is_sent(post_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT 1 FROM sent_posts WHERE post_id = ?", (post_id,)).fetchone()
    conn.close()
    return row is not None


def mark_sent(post_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO sent_posts (post_id, sent_at) VALUES (?, ?)",
        (post_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def get_last_id() -> str | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM meta WHERE key = 'last_post_id'").fetchone()
    conn.close()
    return row[0] if row else None


def set_last_id(post_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_post_id', ?)",
        (post_id,),
    )
    conn.commit()
    conn.close()

# ─── Truth Social Fetching (Multiple Methods) ──────────────────────────────────

class TruthSocialFetcher:
    """Пробует разные методы получения постов из Truth Social."""

    def __init__(self, username: str):
        self.username = username
        self._working_method = None  # кэш рабочего метода

    def fetch(self, limit: int = 20) -> list[dict]:
        """
        Возвращает список постов в едином формате:
        [{"id": str, "text": str, "url": str, "date": str, "media": list}, ...]
        """
        # Если уже знаем рабочий метод — пробуем его первым
        methods = [
            ("direct_api", self._fetch_direct_api),
            ("rsshub", self._fetch_rsshub),
            ("nitter", self._fetch_nitter),
        ]

        # Переставляем рабочий метод первым
        if self._working_method:
            methods.sort(key=lambda x: 0 if x[0] == self._working_method else 1)

        for name, method in methods:
            try:
                posts = method(limit)
                if posts:
                    if self._working_method != name:
                        log.info("✅ Method '%s' works! Using it.", name)
                        self._working_method = name
                    return posts
            except Exception as e:
                log.warning("Method '%s' failed: %s", name, e)

        log.error("❌ All methods failed!")
        return []

    def _fetch_direct_api(self, limit: int) -> list[dict]:
        """Метод 1: Прямой Mastodon-совместимый API Truth Social."""
        # Получаем ID аккаунта
        resp = SESSION.get(
            "https://truthsocial.com/api/v1/accounts/lookup",
            params={"acct": self.username},
            timeout=15,
        )
        resp.raise_for_status()
        account_id = resp.json()["id"]

        # Получаем посты
        resp = SESSION.get(
            f"https://truthsocial.com/api/v1/accounts/{account_id}/statuses",
            params={"limit": limit},
            timeout=15,
        )
        resp.raise_for_status()

        posts = []
        for item in resp.json():
            text = self._strip_html(item.get("content", ""))
            if not text:
                continue
            posts.append({
                "id": item["id"],
                "text": text,
                "url": item.get("url", f"https://truthsocial.com/@{self.username}/{item['id']}"),
                "date": item.get("created_at", "")[:10],
                "media": [m.get("type", "unknown") for m in item.get("media_attachments", [])],
            })
        return posts

    def _fetch_rsshub(self, limit: int) -> list[dict]:
        """Метод 2: RSSHub — публичный RSS-сервис."""
        urls = [
            f"https://rsshub.app/truthsocial/user/{self.username}",
            f"https://rsshub.rssforever.com/truthsocial/user/{self.username}",
        ]

        for url in urls:
            try:
                resp = SESSION.get(url, timeout=20)
                if resp.status_code == 200 and "<rss" in resp.text[:200]:
                    return self._parse_rss(resp.text, limit)
            except Exception:
                continue

        raise Exception("RSSHub: no working instance")

    def _fetch_nitter(self, limit: int) -> list[dict]:
        """Метод 3: Nitter.poast.org — зеркало Truth Social."""
        urls = [
            f"https://nitter.poast.org/{self.username}/rss",
            f"https://nitter.privacydev.net/{self.username}/rss",
        ]

        for url in urls:
            try:
                resp = SESSION.get(url, timeout=20)
                if resp.status_code == 200 and "<rss" in resp.text[:200]:
                    return self._parse_rss(resp.text, limit)
            except Exception:
                continue

        raise Exception("Nitter: no working instance")

    def _parse_rss(self, xml_text: str, limit: int) -> list[dict]:
        """Парсит RSS XML в список постов."""
        root = ET.fromstring(xml_text)
        items = root.findall(".//item")[:limit]

        posts = []
        for item in items:
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            description = item.findtext("description", "").strip()
            pub_date = item.findtext("pubDate", "").strip()

            # Берём описание или title, что длиннее
            text = self._strip_html(description) if description else title
            if not text:
                continue

            # Генерируем ID из ссылки
            post_id = link.rstrip("/").split("/")[-1] if link else str(hash(text))

            posts.append({
                "id": post_id,
                "text": text,
                "url": link,
                "date": pub_date[:16] if pub_date else "",
                "media": [],
            })

        return posts

    @staticmethod
    def _strip_html(html: str) -> str:
        """Убирает HTML-теги и декодирует entities."""
        text = re.sub(r"<[^>]+>", "", html)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"&lt;", "<", text)
        text = re.sub(r"&gt;", ">", text)
        text = re.sub(r"&#39;", "'", text)
        text = re.sub(r"&quot;", '"', text)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

# ─── Translation ───────────────────────────────────────────────────────────────

def translate_to_russian(text: str) -> str:
    if not text or len(text.strip()) < 5:
        return text
    try:
        translated = GoogleTranslator(source="en", target="ru").translate(text)
        return translated or text
    except Exception as e:
        log.warning("Translation failed: %s", e)
        return f"[перевод недоступен]"

# ─── Sentiment Analysis ────────────────────────────────────────────────────────

def analyze_sentiment(text: str) -> tuple[str, str]:
    """
    Возвращает (emoji, label).
    """
    clean = re.sub(r"https?://\S+", "", text).strip()

    if len(clean) < 10:
        return "⚪", "нейтрально"

    blob = TextBlob(clean)
    polarity = blob.sentiment.polarity

    if polarity > 0.15:
        return "🟢", "позитивный"
    elif polarity < -0.15:
        return "🔴", "негативный"
    else:
        return "⚪", "нейтральный"

# ─── Message Formatting ────────────────────────────────────────────────────────

def format_post(post: dict) -> str | None:
    """Форматируем пост для Telegram (HTML)."""
    text = post["text"]
    if not text or len(text) < 5:
        return None

    post_id = post["id"]
    url = post.get("url", "")
    date = post.get("date", "")
    media = post.get("media", [])

    # Перевод
    translated = translate_to_russian(text)

    # Sentiment
    emoji, label = analyze_sentiment(text)

    # Медиа
    media_note = ""
    if media:
        media_note = f"\n📎 <i>Вложений: {len(media)} ({', '.join(media)})</i>"

    msg = f"""<b>🔴 Новый пост Truth Social</b>
📅 {date} | {emoji} Тон: <b>{label}</b>

<b>🇺🇸 Оригинал:</b>
{text}

<b>🇷🇺 Перевод:</b>
{translated}
{media_note}
🔗 <a href="{url}">Открыть в Truth Social</a>""".strip()

    return msg

# ─── Telegram Sender ───────────────────────────────────────────────────────────

def send_to_telegram(text: str):
    """Отправляем сообщение в Telegram через HTTP API."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if TELEGRAM_THREAD_ID:
        payload["message_thread_id"] = int(TELEGRAM_THREAD_ID)
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()

# ─── Main Poll Logic ───────────────────────────────────────────────────────────

# Глобальный fetcher
fetcher = TruthSocialFetcher(TRUTHSOCIAL_USERNAME)


def poll_once():
    """Один цикл опроса Truth Social."""
    try:
        log.info("Polling @%s ...", TRUTHSOCIAL_USERNAME)

        posts = fetcher.fetch(limit=10)

        if not posts:
            log.info("No posts returned (all methods failed or empty).")
            return

        # Сортируем от старых к новым
        posts_sorted = list(reversed(posts))

        new_count = 0
        for post in posts_sorted:
            post_id = post["id"]

            if is_sent(post_id):
                continue

            message = format_post(post)
            if message is None:
                log.info("Skipping post %s (too short)", post_id)
                mark_sent(post_id)
                continue

            try:
                send_to_telegram(message)
                mark_sent(post_id)
                new_count += 1
                log.info("✅ Sent post %s to Telegram", post_id)
            except Exception as e:
                log.error("Failed to send post %s: %s", post_id, e)

        if new_count > 0:
            log.info("Sent %d new post(s).", new_count)
        else:
            log.info("No new posts.")

    except Exception as e:
        log.error("Poll error: %s", e)

# ─── Startup ───────────────────────────────────────────────────────────────────

def startup_check():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("❌ TELEGRAM_BOT_TOKEN не задан! См. .env.example")
    if not TELEGRAM_CHAT_ID:
        raise SystemExit("❌ TELEGRAM_CHAT_ID не задан! См. .env.example")

    init_db()

    # Первый запуск — помечаем текущие посты как отправлённые
    if get_last_id() is None:
        log.info("First run — fetching current posts to avoid spam...")
        try:
            posts = fetcher.fetch(limit=5)
            for post in posts:
                mark_sent(post["id"])
            if posts:
                set_last_id(posts[0]["id"])
                log.info("Marked %d existing posts as sent.", len(posts))
        except Exception as e:
            log.warning("Startup fetch failed: %s", e)

    log.info(
        "✅ Config OK — monitoring @%s every %d min → chat %s (thread: %s)",
        TRUTHSOCIAL_USERNAME, POLL_INTERVAL, TELEGRAM_CHAT_ID,
        TELEGRAM_THREAD_ID or "main",
    )

# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    startup_check()

    scheduler = BackgroundScheduler()
    scheduler.add_job(poll_once, "interval", minutes=POLL_INTERVAL, next_run_time=datetime.now())
    scheduler.start()

    log.info("🚀 Bot started. Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down...")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
