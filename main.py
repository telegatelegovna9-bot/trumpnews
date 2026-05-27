"""
Truth Social → Telegram Bot v3
Перехватывает API через Playwright + скриншоты постов.
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
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

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
    conn.execute("CREATE TABLE IF NOT EXISTS sent_posts (post_id TEXT PRIMARY KEY, sent_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()

def is_sent(post_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT 1 FROM sent_posts WHERE post_id = ?", (post_id,)).fetchone()
    conn.close()
    return row is not None

def mark_sent(post_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO sent_posts (post_id, sent_at) VALUES (?, ?)",
                 (post_id, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()

def get_last_id() -> str | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM meta WHERE key = 'last_post_id'").fetchone()
    conn.close()
    return row[0] if row else None

def set_last_id(post_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_post_id', ?)", (post_id,))
    conn.commit()
    conn.close()

# ─── Translation ───────────────────────────────────────────────────────────────

def translate_to_russian(text: str) -> str:
    if not text or len(text.strip()) < 5:
        return text
    try:
        return GoogleTranslator(source="en", target="ru").translate(text) or text
    except Exception as e:
        log.warning("Translation failed: %s", e)
        return "[перевод недоступен]"

# ─── Sentiment ─────────────────────────────────────────────────────────────────

def analyze_sentiment(text: str) -> tuple[str, str]:
    clean = re.sub(r"https?://\S+", "", text).strip()
    if len(clean) < 10:
        return "⚪", "нейтрально"
    polarity = TextBlob(clean).sentiment.polarity
    if polarity > 0.15:
        return "🟢", "позитивный"
    elif polarity < -0.15:
        return "🔴", "негативный"
    return "⚪", "нейтральный"

# ─── Telegram ──────────────────────────────────────────────────────────────────

def send_photo(photo_bytes: bytes, caption: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
    if TELEGRAM_THREAD_ID:
        data["message_thread_id"] = int(TELEGRAM_THREAD_ID)
    resp = requests.post(url, data=data, files={"photo": ("post.png", photo_bytes, "image/png")}, timeout=30)
    resp.raise_for_status()

def send_text(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if TELEGRAM_THREAD_ID:
        payload["message_thread_id"] = int(TELEGRAM_THREAD_ID)
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()

# ─── Health Check ──────────────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass

def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log.info("Health server on port %d", port)
    server.serve_forever()

# ─── Core: Fetch posts via Playwright API interception ─────────────────────────

STEALTH_JS = """
// Stealth: скрываем headless-фингерпринты
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = {runtime: {}};
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : originalQuery(parameters)
);
"""


def is_cloudflare_challenge(page) -> bool:
    """Проверяет, находимся ли мы на Cloudflare challenge page."""
    try:
        text = page.inner_text("body", timeout=3000)
        indicators = [
            "Performing security verification",
            "Enable JavaScript and cookies",
            "security service to protect",
            "Checking your browser",
            "Just a moment",
        ]
        return any(ind.lower() in text.lower() for ind in indicators)
    except Exception:
        return False


def fetch_posts_via_playwright(pw_browser, limit: int = 10) -> list[dict]:
    """
    Загружает профиль через Playwright с stealth-патчами,
    ждёт завершения Cloudflare challenge, затем перехватывает API и скрейпит DOM.
    """
    context = pw_browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.112 Safari/537.36",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
    )
    page = context.new_page()

    # Применяем stealth-патчи ДО загрузки страницы
    page.add_init_script(STEALTH_JS)

    # Перехватываем API-ответы с постами
    api_posts = []
    seen_ids = set()

    def handle_response(response):
        url = response.url
        if "/api/v1/accounts/" in url and "/statuses" in url:
            log.info("API intercepted: %s", url.split("?")[0][-60:])
            if "pinned=true" not in url and "only_media=true" not in url:
                try:
                    data = response.json()
                    if isinstance(data, list):
                        for item in data:
                            post_id = str(item.get("id", ""))
                            if post_id and post_id not in seen_ids:
                                seen_ids.add(post_id)
                                api_posts.append(item)
                        log.info("Accepted %d statuses (total: %d)", len(data), len(api_posts))
                except Exception:
                    pass

    page.on("response", handle_response)

    try:
        profile_url = f"https://truthsocial.com/@{TRUTHSOCIAL_USERNAME}"
        log.info("Loading %s ...", profile_url)
        page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)

        # Ждём завершения Cloudflare challenge (до 30 сек)
        for attempt in range(6):
            page.wait_for_timeout(5000)
            if not is_cloudflare_challenge(page):
                log.info("✅ Cloudflare challenge passed (attempt %d)", attempt + 1)
                break
            log.info("Cloudflare challenge active, waiting... (attempt %d)", attempt + 1)
            # Иногда нужен клик или перезагрузка
            if attempt == 3:
                log.info("Reloading page...")
                page.reload(wait_until="domcontentloaded", timeout=60000)
        else:
            log.warning("Cloudflare challenge NOT passed after 30s")

        # Даём время на загрузку контента после challenge
        page.wait_for_timeout(3000)

        # Метод 2: API через браузер (обходит Cloudflare)
        if not api_posts:
            log.info("Trying in-browser API fetch...")
            try:
                lookup_data = page.evaluate("""
                    async () => {
                        const r = await fetch('/api/v1/accounts/lookup?acct=realDonaldTrump');
                        if (!r.ok) return {error: r.status};
                        return await r.json();
                    }
                """)
                log.info("In-browser lookup: %s", str(lookup_data)[:200])

                if lookup_data and lookup_data.get("id"):
                    account_id = str(lookup_data["id"])
                    statuses_data = page.evaluate("""
                        async (accountId) => {
                            const r = await fetch(`/api/v1/accounts/${accountId}/statuses?limit=10`);
                            if (!r.ok) return {error: r.status};
                            return await r.json();
                        }
                    """, account_id)
                    log.info("In-browser statuses: got %s items", len(statuses_data) if isinstance(statuses_data, list) else "error")

                    if isinstance(statuses_data, list):
                        for item in statuses_data:
                            post_id = str(item.get("id", ""))
                            if post_id and post_id not in seen_ids:
                                seen_ids.add(post_id)
                                api_posts.append(item)
                else:
                    log.warning("In-browser lookup failed: %s", lookup_data)
            except Exception as e:
                log.warning("In-browser API error: %s", e)

        # Метод 3: Playwright API interception (если SPA делает XHR)
        if api_posts:
            log.info("Using %d posts from API", len(api_posts))
            posts = []
            for item in api_posts[:limit]:
                text = strip_html(item.get("content", ""))
                if not text or len(text) < 10:
                    continue
                posts.append({
                    "id": str(item["id"]),
                    "text": text,
                    "url": item.get("url", ""),
                    "date": item.get("created_at", "")[:10],
                    "media": [m.get("type", "") for m in item.get("media_attachments", [])],
                })
            context.close()
            return posts

        # Метод 4: DOM-скрейпинг (последний fallback)
        log.info("No API data, trying DOM scraping...")
        posts = scrape_dom(page, limit)

        context.close()
        return posts

    except Exception as e:
        log.error("fetch_posts failed: %s", e)
        context.close()
        return []


def scrape_dom(page, limit: int) -> list[dict]:
    """Извлекает посты из DOM разными способами."""
    # Пробуем разные селекторы
    selector_groups = [
        ['article[data-testid="status"]', 'article.status', 'article'],
        ['div[class*="status"]', 'div[class*="Status"]'],
        ['div[class*="post"]', 'div[class*="Post"]'],
        ['div[class*="item"]'],
    ]

    elements = []
    used_sel = ""
    for group in selector_groups:
        for sel in group:
            els = page.query_selector_all(sel)
            if els and len(els) > 0:
                elements = els
                used_sel = sel
                break
        if elements:
            break

    if not elements:
        # Дебаг: дампим часть HTML чтобы понять структуру
        try:
            html = page.content()
            soup = BeautifulSoup(html, "html.parser")
            articles = soup.find_all("article")
            divs_with_class = soup.find_all("div", class_=True)
            log.warning("DOM debug: page has %d <article>, %d <div> with classes", len(articles), len(divs_with_class))

            # Показываем топ-10 div'ов с классами (по длине текста)
            div_info = []
            for d in divs_with_class:
                txt = d.get_text(strip=True)
                if len(txt) > 30:
                    div_info.append((len(txt), d.get("class", []), txt[:120]))
            div_info.sort(key=lambda x: -x[0])
            for length, classes, txt in div_info[:10]:
                log.warning("  div len=%d classes=%s text=%.120s", length, classes, txt)

            # Ищем элементы с data-testid
            testids = soup.find_all(attrs={"data-testid": True})
            if testids:
                log.warning("Found %d elements with data-testid:", len(testids))
                for el in testids[:10]:
                    log.warning("  data-testid=%s tag=%s text=%.100s", el.get("data-testid"), el.name, el.get_text(strip=True))

            # Пишем HTML в файл
            dump_path = Path(__file__).parent / "debug_page.html"
            dump_path.write_text(html[:100000], encoding="utf-8")
            log.info("HTML dumped to %s (%d bytes)", dump_path, len(html))
        except Exception as e:
            log.warning("DOM debug failed: %s", e)
        return []

    log.info("DOM: found %d elements with '%s'", len(elements), used_sel)

    posts = []
    for i, el in enumerate(elements[:limit]):
        try:
            raw_text = el.inner_text().strip()
            if not raw_text or len(raw_text) < 15:
                continue

            # Ищем ссылку
            link_el = el.query_selector(f'a[href*="/@{TRUTHSOCIAL_USERNAME}/"]')
            post_url = ""
            post_id = f"dom_{i}_{hash(raw_text[:80])}"

            if link_el:
                href = link_el.get_attribute("href") or ""
                post_url = f"https://truthsocial.com{href}" if href.startswith("/") else href
                parts = href.rstrip("/").split("/")
                if parts and parts[-1].isdigit():
                    post_id = parts[-1]

            # Дата
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
            log.debug("DOM extract %d: %s", i, e)

    return posts


def strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", "", html)
    for entity, char in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'"), ("&quot;", '"'), ("&nbsp;", " ")]:
        text = text.replace(entity, char)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ─── Direct Mastodon API ──────────────────────────────────────────────────────

_cached_account_id: str | None = None

def get_account_id(username: str) -> str | None:
    """Получает ID аккаунта через Mastodon-compatible API."""
    global _cached_account_id
    if _cached_account_id:
        return _cached_account_id

    url = f"https://truthsocial.com/api/v1/accounts/lookup?acct={username}"
    try:
        log.info("API lookup: %s", url)
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "application/json",
        })
        log.info("API lookup status: %d", r.status_code)
        if r.status_code == 200:
            data = r.json()
            account_id = str(data.get("id", ""))
            if account_id:
                _cached_account_id = account_id
                log.info("✅ Account ID: %s", account_id)
                return account_id
        else:
            log.warning("API lookup failed: %d — %s", r.status_code, r.text[:300])
    except Exception as e:
        log.warning("API lookup error: %s", e)
    return None


def fetch_posts_via_api(username: str = TRUTHSOCIAL_USERNAME, limit: int = 10) -> list[dict]:
    """Получает посты напрямую через Mastodon API (без браузера)."""
    account_id = get_account_id(username)
    if not account_id:
        log.warning("Cannot get account ID, API method unavailable")
        return []

    url = f"https://truthsocial.com/api/v1/accounts/{account_id}/statuses"
    params = {"limit": limit}
    try:
        log.info("API fetch: %s?limit=%d", url, limit)
        r = requests.get(url, params=params, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "application/json",
        })
        log.info("API fetch status: %d", r.status_code)

        if r.status_code != 200:
            log.warning("API fetch failed: %d — %s", r.status_code, r.text[:300])
            return []

        data = r.json()
        if not isinstance(data, list):
            log.warning("API returned non-list: %s", type(data).__name__)
            return []

        posts = []
        for item in data:
            post_id = str(item.get("id", ""))
            text = strip_html(item.get("content", ""))
            if not text or len(text) < 10:
                continue
            posts.append({
                "id": post_id,
                "text": text,
                "url": item.get("url", ""),
                "date": item.get("created_at", "")[:10],
                "media": [m.get("type", "") for m in item.get("media_attachments", [])],
            })

        log.info("✅ API: got %d posts", len(posts))
        return posts

    except Exception as e:
        log.error("API fetch error: %s", e)
        return []


def take_screenshot(pw_browser, post_url: str) -> bytes | None:
    """Делает скриншот поста по его URL."""
    if not post_url:
        return None

    context = pw_browser.new_context(
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.112 Safari/537.36",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    page = context.new_page()
    page.add_init_script(STEALTH_JS)

    try:
        log.info("Screenshot: loading %s", post_url)
        page.goto(post_url, wait_until="domcontentloaded", timeout=60000)

        # Ждём завершения Cloudflare challenge
        for _ in range(5):
            page.wait_for_timeout(4000)
            if not is_cloudflare_challenge(page):
                break
        else:
            page.wait_for_timeout(3000)

        # Ищем пост на странице
        selectors = [
            'article[data-testid="status"]',
            'article.status',
            'div[class*="status"]',
            'article',
        ]

        elements = []
        for sel in selectors:
            elements = page.query_selector_all(sel)
            if elements:
                break

        if not elements:
            log.warning("Screenshot: no elements found on %s", post_url)
            # Скриншот всей страницы как fallback
            screenshot = page.screenshot(type="png")
            context.close()
            return screenshot

        # Берём первый (основной) элемент — на странице поста он один
        el = elements[0]
        el.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        screenshot = el.screenshot(type="png")
        log.info("Screenshot: %d bytes", len(screenshot))
        context.close()
        return screenshot
    except Exception as e:
        log.error("Screenshot failed: %s", e)
        context.close()
        return None

# ─── Poll ──────────────────────────────────────────────────────────────────────

def poll_once(pw_browser):
    log.info("Polling @%s ...", TRUTHSOCIAL_USERNAME)

    # Метод 1: Прямой API-запрос (быстрый, без браузера)
    posts = fetch_posts_via_api(TRUTHSOCIAL_USERNAME, limit=10)

    # Метод 2: Playwright (fallback)
    if not posts:
        log.info("API вернул 0 постов, пробуем Playwright...")
        posts = fetch_posts_via_playwright(pw_browser, limit=10)

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

        url = post.get("url", "")
        date = post.get("date", "")

        # Пробуем скриншот
        screenshot = None
        if url:
            screenshot = take_screenshot(pw_browser, url)

        try:
            if screenshot:
                caption = (
                    f"<b>🔴 Пост из Truth Social</b>\n"
                    f"📅 {date} | {emoji} Тон: <b>{label}</b>\n\n"
                    f"<b>🇷🇺 Перевод:</b>\n{translated}\n\n"
                    f'🔗 <a href="{url}">Открыть оригинал</a>'
                )
                send_photo(screenshot, caption)
                log.info("✅ Screenshot + translation sent for %s", post_id)
            else:
                msg = (
                    f"<b>🔴 Пост из Truth Social</b>\n"
                    f"📅 {date} | {emoji} Тон: <b>{label}</b>\n\n"
                    f"<b>🇺🇸 Оригинал:</b>\n{text}\n\n"
                    f"<b>🇷🇺 Перевод:</b>\n{translated}\n\n"
                    f'🔗 <a href="{url}">Открыть оригинал</a>'
                )
                send_text(msg)
                log.info("⚠️ Text sent for %s", post_id)

            mark_sent(post_id)
            new_count += 1
        except Exception as e:
            log.error("Send failed %s: %s", post_id, e)

    log.info("Done. %d new post(s).", new_count)

# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("❌ TELEGRAM_BOT_TOKEN не задан!")
    if not TELEGRAM_CHAT_ID:
        raise SystemExit("❌ TELEGRAM_CHAT_ID не задан!")

    init_db()

    # Healthcheck
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()

    # Browser
    log.info("Starting Playwright...")
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
    log.info("✅ Browser ready.")

    # First run
    if get_last_id() is None:
        log.info("First run — marking existing posts...")
        try:
            # Пробуем API, потом Playwright
            posts = fetch_posts_via_api(TRUTHSOCIAL_USERNAME, limit=5)
            if not posts:
                posts = fetch_posts_via_playwright(browser, limit=5)
            for p in posts:
                mark_sent(p["id"])
            if posts:
                set_last_id(posts[0]["id"])
                log.info("Marked %d existing posts.", len(posts))
        except Exception as e:
            log.warning("Startup failed: %s", e)

    log.info("🚀 Bot started. Polling every %d min.", POLL_INTERVAL)

    try:
        while True:
            try:
                poll_once(browser)
            except Exception as e:
                log.error("Poll error: %s", e)
            time.sleep(POLL_INTERVAL * 60)
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down...")
    finally:
        browser.close()
        pw.stop()


if __name__ == "__main__":
    main()
