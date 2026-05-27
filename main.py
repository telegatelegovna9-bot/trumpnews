"""
Truth Social → Telegram Bot
Скриншоты постов + перевод + sentiment.
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
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if TELEGRAM_THREAD_ID:
        data["message_thread_id"] = int(TELEGRAM_THREAD_ID)

    files = {"photo": ("post.png", photo_bytes, "image/png")}
    resp = requests.post(url, data=data, files=files, timeout=30)
    resp.raise_for_status()
    return resp.json()


def send_text_to_telegram(text: str):
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

# ─── Health Check Server ──────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log.info("Health server on port %d", port)
    server.serve_forever()

# ─── Main Loop ─────────────────────────────────────────────────────────────────

def main():
    # Проверка конфигурации
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("❌ TELEGRAM_BOT_TOKEN не задан!")
    if not TELEGRAM_CHAT_ID:
        raise SystemExit("❌ TELEGRAM_CHAT_ID не задан!")

    init_db()

    # Healthcheck в отдельном потоке
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()

    # Запускаем браузер
    log.info("Starting Playwright browser...")
    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    )
    context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
    )
    log.info("✅ Browser started.")

    # Первый запуск — помечаем существующие посты
    if get_last_id() is None:
        log.info("First run — marking existing posts...")
        try:
            posts = fetch_posts(context, limit=5)
            for post in posts:
                mark_sent(post["id"])
            if posts:
                set_last_id(posts[0]["id"])
                log.info("Marked %d existing posts.", len(posts))
        except Exception as e:
            log.warning("Startup fetch failed: %s", e)

    log.info("🚀 Bot started. Polling every %d min.", POLL_INTERVAL)

    # Основной цикл (в главном потоке!)
    try:
        while True:
            try:
                poll_once(context)
            except Exception as e:
                log.error("Poll error: %s", e)
            time.sleep(POLL_INTERVAL * 60)
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down...")
    finally:
        context.close()
        browser.close()
        pw.stop()


def fetch_posts(context, limit: int = 10) -> list[dict]:
    """Загружает профиль и извлекает посты."""
    page = context.new_page()
    try:
        url = f"https://truthsocial.com/@{TRUTHSOCIAL_USERNAME}"
        log.info("Loading %s ...", url)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)  # Ждём рендер

        # Ищем посты разными селекторами
        selectors = [
            'article[data-testid="status"]',
            'div.status-wrapper',
            'article.status',
            'div[class*="status"]',
            'article',
        ]

        elements = []
        for sel in selectors:
            elements = page.query_selector_all(sel)
            if elements:
                log.info("Found %d elements with: %s", len(elements), sel)
                break

        if not elements:
            page.evaluate("window.scrollTo(0, 1000)")
            page.wait_for_timeout(2000)
            for sel in selectors:
                elements = page.query_selector_all(sel)
                if elements:
                    break

        posts = []
        for i, el in enumerate(elements[:limit]):
            try:
                text_el = el.query_selector(
                    '[class*="status-content"], [class*="post-content"], .e-content, [class*="content"]'
                )
                raw_text = (text_el or el).inner_text().strip()
                if not raw_text or len(raw_text) < 10:
                    continue

                link_el = el.query_selector(f'a[href*="/@{TRUTHSOCIAL_USERNAME}/"]')
                post_url = ""
                post_id = f"post_{i}_{hash(raw_text[:100])}"

                if link_el:
                    href = link_el.get_attribute("href")
                    if href:
                        post_url = f"https://truthsocial.com{href}" if href.startswith("/") else href
                        parts = href.rstrip("/").split("/")
                        if parts and parts[-1].isdigit():
                            post_id = parts[-1]

                time_el = el.query_selector("time")
                date = ""
                if time_el:
                    date = (time_el.get_attribute("datetime") or time_el.inner_text())[:10]

                posts.append({
                    "id": post_id,
                    "text": raw_text,
                    "url": post_url,
                    "date": date,
                    "element_index": i,
                })
            except Exception as e:
                log.debug("Extract post %d failed: %s", i, e)

        log.info("Extracted %d posts.", len(posts))
        return posts

    finally:
        page.close()


def screenshot_post(context, element_index: int) -> bytes | None:
    """Делает скриншот поста."""
    page = context.new_page()
    try:
        url = f"https://truthsocial.com/@{TRUTHSOCIAL_USERNAME}"
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        selectors = [
            'article[data-testid="status"]',
            'div.status-wrapper',
            'article.status',
            'div[class*="status"]',
            'article',
        ]

        elements = []
        for sel in selectors:
            elements = page.query_selector_all(sel)
            if elements:
                break

        if not elements or element_index >= len(elements):
            log.warning("Cannot find element %d (found %d)", element_index, len(elements))
            return None

        el = elements[element_index]
        el.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        screenshot = el.screenshot(type="png")
        log.info("Screenshot: %d bytes", len(screenshot))
        return screenshot
    except Exception as e:
        log.error("Screenshot failed: %s", e)
        return None
    finally:
        page.close()


def poll_once(context):
    """Один цикл опроса."""
    log.info("Polling @%s ...", TRUTHSOCIAL_USERNAME)

    posts = fetch_posts(context, limit=10)
    if not posts:
        log.info("No posts found.")
        return

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

        emoji, label = analyze_sentiment(text)
        translated = translate_to_russian(text)

        caption = (
            f"<b>🔴 Пост из Truth Social</b>\n"
            f"📅 {post.get('date', '')} | {emoji} Тон: <b>{label}</b>\n\n"
            f"<b>🇷🇺 Перевод:</b>\n"
            f"{translated}\n\n"
            f'🔗 <a href="{post.get("url", "")}">Открыть оригинал</a>'
        )

        screenshot = screenshot_post(context, post.get("element_index", 0))

        try:
            if screenshot:
                send_photo_to_telegram(screenshot, caption)
                log.info("✅ Screenshot sent for post %s", post_id)
            else:
                fallback = (
                    f"<b>🔴 Пост из Truth Social</b>\n"
                    f"📅 {post.get('date', '')} | {emoji} Тон: <b>{label}</b>\n\n"
                    f"<b>🇺🇸 Оригинал:</b>\n{text}\n\n"
                    f"<b>🇷🇺 Перевод:</b>\n{translated}\n\n"
                    f'🔗 <a href="{post.get("url", "")}">Открыть оригинал</a>'
                )
                send_text_to_telegram(fallback)
                log.info("⚠️ Text sent (screenshot failed) for post %s", post_id)

            mark_sent(post_id)
            new_count += 1
        except Exception as e:
            log.error("Send failed for %s: %s", post_id, e)

    log.info("Done. %d new post(s).", new_count)


if __name__ == "__main__":
    main()
