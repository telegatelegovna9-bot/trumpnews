"""
Truth Social → Telegram Bot
Мониторит новые посты, делает скриншоты и отправляет в Telegram с переводом.

Использует Playwright (headless Chrome) для:
- Обхода Cloudflare и любых блокировок
- Скриншотов отдельных постов
- Извлечения текста из рендеренной страницы
"""

import os
import re
import logging
import sqlite3
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
from dotenv import load_dotenv
from deep_translator import GoogleTranslator
from textblob import TextBlob
from playwright.sync_api import sync_playwright, Page
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

# ─── Playwright Browser ───────────────────────────────────────────────────────

class Browser:
    """Управляет headless Chrome через Playwright."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._context = None

    def start(self):
        log.info("Starting Playwright browser...")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        log.info("✅ Browser started.")

    def stop(self):
        if self._context:
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
        log.info("Browser stopped.")

    def new_page(self) -> Page:
        return self._context.new_page()

    def fetch_posts(self, username: str, limit: int = 10) -> list[dict]:
        """
        Загружает профиль Truth Social и извлекает посты.
        Возвращает: [{"id": str, "text": str, "url": str, "date": str}, ...]
        """
        page = self.new_page()
        try:
            url = f"https://truthsocial.com/@{username}"
            log.info("Loading %s ...", url)
            page.goto(url, wait_until="networkidle", timeout=30000)

            # Ждём пока посты загрузятся
            page.wait_for_timeout(3000)

            # Пробуем разные селекторы для постов
            posts = self._extract_posts(page, username, limit)

            if not posts:
                # Если не нашли — скроллим вниз и пробуем снова
                page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                page.wait_for_timeout(2000)
                posts = self._extract_posts(page, username, limit)

            log.info("Found %d posts on page.", len(posts))
            return posts

        except Exception as e:
            log.error("Failed to fetch posts: %s", e)
            # Сохраняем скриншот для дебага
            try:
                page.screenshot(path="/app/debug_page.png")
                log.info("Debug screenshot saved to /app/debug_page.png")
            except Exception:
                pass
            return []
        finally:
            page.close()

    def _extract_posts(self, page: Page, username: str, limit: int) -> list[dict]:
        """Извлекает посты из рендеренной страницы."""
        posts = []

        # Селекторы для Truth Social (Mastodon-based UI)
        # Попробуем несколько вариантов
        selectors = [
            'article[data-testid="status"]',
            'div.status-wrapper',
            'article.status',
            'div[class*="status"]',
            'div[class*="post"]',
        ]

        elements = []
        used_selector = None
        for sel in selectors:
            elements = page.query_selector_all(sel)
            if elements:
                used_selector = sel
                log.info("Found %d elements with selector: %s", len(elements), sel)
                break

        if not elements:
            # Fallback: ищем статьи на странице
            elements = page.query_selector_all("article")
            if elements:
                used_selector = "article"
                log.info("Fallback: found %d <article> elements", len(elements))

        for i, el in enumerate(elements[:limit]):
            try:
                # Извлекаем текст
                text_el = el.query_selector('[class*="status-content"], [class*="post-content"], .e-content, [class*="content"]')
                if not text_el:
                    text_el = el  # берём весь элемент

                raw_text = text_el.inner_text().strip()
                if not raw_text or len(raw_text) < 10:
                    continue

                # Извлекаем ссылку на пост
                link_el = el.query_selector('a[href*="/@' + username + '/"]')
                post_url = ""
                post_id = f"post_{i}_{hash(raw_text[:100])}"

                if link_el:
                    href = link_el.get_attribute("href")
                    if href:
                        post_url = f"https://truthsocial.com{href}" if href.startswith("/") else href
                        # Извлекаем ID из URL
                        parts = href.rstrip("/").split("/")
                        if parts and parts[-1].isdigit():
                            post_id = parts[-1]

                # Дата
                time_el = el.query_selector("time")
                date = ""
                if time_el:
                    date = time_el.get_attribute("datetime") or time_el.inner_text()
                    if date:
                        date = date[:10]

                posts.append({
                    "id": post_id,
                    "text": raw_text,
                    "url": post_url,
                    "date": date,
                    "element_index": i,  # индекс элемента на странице для скриншота
                })

            except Exception as e:
                log.debug("Failed to extract post %d: %s", i, e)
                continue

        return posts

    def screenshot_post(self, username: str, element_index: int) -> bytes | None:
        """
        Делает скриншот конкретного поста на странице профиля.
        Возвращает PNG bytes или None.
        """
        page = self.new_page()
        try:
            url = f"https://truthsocial.com/@{username}"
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)

            # Ищем элементы постов
            selectors = [
                'article[data-testid="status"]',
                'div.status-wrapper',
                'article.status',
                'div[class*="status"]',
                'div[class*="post"]',
                'article',
            ]

            elements = []
            for sel in selectors:
                elements = page.query_selector_all(sel)
                if elements:
                    break

            if not elements or element_index >= len(elements):
                log.warning("Cannot find post element at index %d (found %d)", element_index, len(elements))
                return None

            el = elements[element_index]

            # Скроллим к элементу
            el.scroll_into_view_if_needed()
            page.wait_for_timeout(500)

            # Делаем скриншот элемента
            screenshot = el.screenshot(type="png")
            log.info("Screenshot taken: %d bytes", len(screenshot))
            return screenshot

        except Exception as e:
            log.error("Screenshot failed: %s", e)
            return None
        finally:
            page.close()

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

# ─── Telegram Sender ───────────────────────────────────────────────────────────

def send_photo_to_telegram(photo_bytes: bytes, caption: str):
    """Отправляет фото с подписью в Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if TELEGRAM_THREAD_ID:
        data["message_thread_id"] = int(TELEGRAM_THREAD_ID)

    files = {
        "photo": ("post.png", photo_bytes, "image/png"),
    }
    resp = requests.post(url, data=data, files=files, timeout=30)
    resp.raise_for_status()
    return resp.json()


def send_text_to_telegram(text: str):
    """Отправляет текстовое сообщение в Telegram (fallback если скриншот не удался)."""
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

browser = Browser()


def poll_once():
    try:
        log.info("Polling @%s ...", TRUTHSOCIAL_USERNAME)

        posts = browser.fetch_posts(TRUTHSOCIAL_USERNAME, limit=10)

        if not posts:
            log.info("No posts found.")
            return

        # Сортируем от старых к новым
        posts_sorted = list(reversed(posts))
        new_count = 0

        for post in posts_sorted:
            post_id = post["id"]

            if is_sent(post_id):
                continue

            text = post["text"]
            if len(text) < 10:
                mark_sent(post_id)
                continue

            # Sentiment
            emoji, label = analyze_sentiment(text)

            # Перевод
            translated = translate_to_russian(text)

            # Подпись к скриншоту
            caption = (
                f"<b>🔴 Пост из Truth Social</b>\n"
                f"📅 {post.get('date', '')} | {emoji} Тон: <b>{label}</b>\n\n"
                f"<b>🇷🇺 Перевод:</b>\n"
                f"{translated}\n\n"
                f"🔗 <a href=\"{post.get('url', '')}\">Открыть оригинал</a>"
            )

            # Делаем скриншот
            screenshot = browser.screenshot_post(TRUTHSOCIAL_USERNAME, post.get("element_index", 0))

            try:
                if screenshot:
                    send_photo_to_telegram(screenshot, caption)
                    log.info("✅ Sent screenshot + translation for post %s", post_id)
                else:
                    # Fallback: отправляем текстом
                    fallback = (
                        f"<b>🔴 Пост из Truth Social</b>\n"
                        f"📅 {post.get('date', '')} | {emoji} Тон: <b>{label}</b>\n\n"
                        f"<b>🇺🇸 Оригинал:</b>\n{text}\n\n"
                        f"<b>🇷🇺 Перевод:</b>\n{translated}\n\n"
                        f"🔗 <a href=\"{post.get('url', '')}\">Открыть оригинал</a>"
                    )
                    send_text_to_telegram(fallback)
                    log.info("⚠️ Sent text (screenshot failed) for post %s", post_id)

                mark_sent(post_id)
                new_count += 1

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
    browser.start()

    if get_last_id() is None:
        log.info("First run — fetching current posts to avoid spam...")
        try:
            posts = browser.fetch_posts(TRUTHSOCIAL_USERNAME, limit=5)
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

# ─── Health Check Server (for Railway) ────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass  # Suppress health check logs


def start_health_server():
    """Запускает простой HTTP-сервер для Railway healthcheck."""
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log.info("Health server on port %d", port)
    server.serve_forever()

# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    startup_check()

    # Запускаем healthcheck-сервер в отдельном потоке (Railway требует)
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()

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
    finally:
        browser.stop()


if __name__ == "__main__":
    main()
