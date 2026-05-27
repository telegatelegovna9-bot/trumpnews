"""
Truth Social → Telegram Bot
Мониторит новые посты с Truth Social и отправляет в Telegram с переводом и sentiment-анализом.

Поддерживает несколько методов получения постов (автоматический fallback):
1. Прямой Truth Social API с Cloudflare bypass (через cookies)
2. Скрейпинг HTML-страницы профиля (извлечение встроенного JSON)
3. RSSHub
4. Nitter
"""

import os
import re
import json
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
TELEGRAM_THREAD_ID = os.getenv("TELEGRAM_THREAD_ID")
TRUTHSOCIAL_USERNAME = os.getenv("TRUTHSOCIAL_USERNAME", "realDonaldTrump")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_MINUTES", "5"))

DB_PATH = Path(__file__).parent / "state.db"

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

# ─── Truth Social Fetching ─────────────────────────────────────────────────────

class TruthSocialFetcher:
    """Пробует разные методы получения постов из Truth Social."""

    def __init__(self, username: str):
        self.username = username
        self._working_method = None
        self._session = self._make_session()

    def _make_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })
        return s

    def fetch(self, limit: int = 20) -> list[dict]:
        """Возвращает список постов в едином формате."""
        methods = [
            ("direct_api", self._fetch_direct_api),
            ("html_scrape", self._fetch_html_scrape),
            ("rsshub", self._fetch_rsshub),
            ("nitter", self._fetch_nitter),
        ]

        if self._working_method:
            methods.sort(key=lambda x: 0 if x[0] == self._working_method else 1)

        for name, method in methods:
            try:
                posts = method(limit)
                if posts:
                    if self._working_method != name:
                        log.info("✅ Method '%s' works! Switching to it.", name)
                        self._working_method = name
                    return posts
                else:
                    log.debug("Method '%s' returned empty.", name)
            except Exception as e:
                log.warning("Method '%s' failed: %s", name, e)

        log.error("❌ All methods failed!")
        return []

    def _fetch_direct_api(self, limit: int) -> list[dict]:
        """Метод 1: Mastodon API с Cloudflare cookie bypass."""
        # Сначала посещаем главную для получения cookies
        try:
            self._session.get("https://truthsocial.com/", timeout=10)
        except Exception:
            pass

        resp = self._session.get(
            "https://truthsocial.com/api/v1/accounts/lookup",
            params={"acct": self.username},
            timeout=15,
        )
        resp.raise_for_status()
        account_id = resp.json()["id"]

        resp = self._session.get(
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

    def _fetch_html_scrape(self, limit: int) -> list[dict]:
        """Метод 2: Скрейпинг HTML-страницы профиля."""
        url = f"https://truthsocial.com/@{self.username}"
        resp = self._session.get(url, timeout=20)
        resp.raise_for_status()

        html = resp.text
        log.info("HTML scrape: got %d bytes, status %d", len(html), resp.status_code)

        # Ищем встроенный JSON в script тегах (Next.js / React pattern)
        # Вариант 1: __NEXT_DATA__
        match = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                return self._extract_from_next_data(data, limit)
            except Exception as e:
                log.warning("Failed to parse __NEXT_DATA__: %s", e)

        # Вариант 2: Ищем JSON с постами в любом script теге
        script_blocks = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
        for block in script_blocks:
            if '"content"' in block and '"account"' in block:
                # Похоже на данные постов
                try:
                    # Пытаемся найти JSON-массив
                    json_match = re.search(r'\[.*?"content".*?\]', block, re.DOTALL)
                    if json_match:
                        data = json.loads(json_match.group())
                        return self._extract_statuses(data, limit)
                except Exception:
                    pass

        # Вариант 3: Ищем ссылки на посты в HTML и парсим их
        post_links = re.findall(rf'href="/@{self.username}/(\d+)"', html)
        if post_links:
            log.info("HTML scrape: found %d post links", len(post_links))
            # Пытаемся получить посты через API с cookies
            posts = []
            for post_id in list(set(post_links))[:limit]:
                try:
                    resp = self._session.get(
                        f"https://truthsocial.com/api/v1/statuses/{post_id}",
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        item = resp.json()
                        text = self._strip_html(item.get("content", ""))
                        if text:
                            posts.append({
                                "id": item["id"],
                                "text": text,
                                "url": item.get("url", f"https://truthsocial.com/@{self.username}/{post_id}"),
                                "date": item.get("created_at", "")[:10],
                                "media": [m.get("type", "unknown") for m in item.get("media_attachments", [])],
                            })
                except Exception:
                    continue
            return posts

        # Вариант 4: Парсим текст постов из HTML напрямую
        # Truth Social рендерит посты с data-атрибутами или определёнными CSS-классами
        post_texts = re.findall(r'class="[^"]*status-content[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
        if not post_texts:
            post_texts = re.findall(r'class="[^"]*post[^"]*content[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)

        if post_texts:
            log.info("HTML scrape: found %d post text blocks", len(post_texts))
            posts = []
            for i, raw in enumerate(post_texts[:limit]):
                text = self._strip_html(raw)
                if text and len(text) > 10:
                    posts.append({
                        "id": f"html_{i}_{hash(text)}",
                        "text": text,
                        "url": f"https://truthsocial.com/@{self.username}",
                        "date": "",
                        "media": [],
                    })
            return posts

        log.warning("HTML scrape: couldn't find posts in HTML (got %d bytes)", len(html))
        return []

    def _extract_from_next_data(self, data: dict, limit: int) -> list[dict]:
        """Извлекает посты из __NEXT_DATA__ JSON."""
        # Ищем массив постов в разных местах структуры
        def find_statuses(obj, depth=0):
            if depth > 10:
                return None
            if isinstance(obj, list):
                if obj and isinstance(obj[0], dict) and "content" in obj[0]:
                    return obj
                for item in obj:
                    result = find_statuses(item, depth + 1)
                    if result:
                        return result
            elif isinstance(obj, dict):
                if "content" in obj and "account" in obj:
                    return [obj]
                for key in ["statuses", "posts", "items", "props", "pageProps", "data"]:
                    if key in obj:
                        result = find_statuses(obj[key], depth + 1)
                        if result:
                            return result
            return None

        statuses = find_statuses(data)
        if statuses:
            return self._extract_statuses(statuses, limit)
        return []

    def _extract_statuses(self, items: list, limit: int) -> list[dict]:
        """Конвертирует массив Mastodon-совместимых статусов в наш формат."""
        posts = []
        for item in items[:limit]:
            text = self._strip_html(item.get("content", ""))
            if not text:
                continue
            posts.append({
                "id": str(item.get("id", hash(text))),
                "text": text,
                "url": item.get("url", ""),
                "date": item.get("created_at", "")[:10],
                "media": [m.get("type", "unknown") for m in item.get("media_attachments", [])],
            })
        return posts

    def _fetch_rsshub(self, limit: int) -> list[dict]:
        """Метод 3: RSSHub."""
        urls = [
            f"https://rsshub.app/truthsocial/user/{self.username}",
            f"https://rsshub.rssforever.com/truthsocial/user/{self.username}",
            f"https://rss.fatpandac.com/truthsocial/user/{self.username}",
        ]

        for url in urls:
            try:
                resp = self._session.get(url, timeout=20)
                log.info("RSSHub %s → %d, content-type: %s, preview: %s",
                         url, resp.status_code,
                         resp.headers.get("Content-Type", "?"),
                         resp.text[:100])
                if resp.status_code == 200 and ("<?xml" in resp.text[:100] or "<rss" in resp.text[:200]):
                    return self._parse_rss(resp.text, limit)
            except Exception as e:
                log.debug("RSSHub %s error: %s", url, e)
                continue

        raise Exception("RSSHub: no working instance")

    def _fetch_nitter(self, limit: int) -> list[dict]:
        """Метод 4: Nitter зеркала."""
        urls = [
            f"https://nitter.poast.org/{self.username}/rss",
            f"https://nitter.privacydev.net/{self.username}/rss",
            f"https://nitter.woodland.cafe/{self.username}/rss",
        ]

        for url in urls:
            try:
                resp = self._session.get(url, timeout=20)
                log.info("Nitter %s → %d, preview: %s",
                         url, resp.status_code, resp.text[:100])
                if resp.status_code == 200 and ("<?xml" in resp.text[:100] or "<rss" in resp.text[:200]):
                    return self._parse_rss(resp.text, limit)
            except Exception as e:
                log.debug("Nitter %s error: %s", url, e)
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

            text = self._strip_html(description) if description else title
            if not text:
                continue

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
        return "[перевод недоступен]"

# ─── Sentiment Analysis ────────────────────────────────────────────────────────

def analyze_sentiment(text: str) -> tuple[str, str]:
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
    text = post["text"]
    if not text or len(text) < 5:
        return None

    url = post.get("url", "")
    date = post.get("date", "")
    media = post.get("media", [])

    translated = translate_to_russian(text)
    emoji, label = analyze_sentiment(text)

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

fetcher = TruthSocialFetcher(TRUTHSOCIAL_USERNAME)


def poll_once():
    try:
        log.info("Polling @%s ...", TRUTHSOCIAL_USERNAME)

        posts = fetcher.fetch(limit=10)

        if not posts:
            log.info("No posts returned.")
            return

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
        raise SystemExit("❌ TELEGRAM_BOT_TOKEN не задан!")
    if not TELEGRAM_CHAT_ID:
        raise SystemExit("❌ TELEGRAM_CHAT_ID не задан!")

    init_db()

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
        "✅ Config OK — @%s every %d min → chat %s (thread: %s)",
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
