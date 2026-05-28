"""Truth Social monitor using cloudscraper (Cloudflare bypass) + Playwright (screenshots).

cloudscraper automatically solves Cloudflare JS challenges.
Playwright is used only for taking screenshots of posts.

Architecture:
1. cloudscraper polls Truth Social API (handles Cloudflare automatically)
2. When new post found → Playwright takes screenshot
3. DOM scraping via Playwright as fallback if cloudscraper fails
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime
from typing import Callable, Optional

from models import Post

logger = logging.getLogger(__name__)

TRUTHSOCIAL_API = "https://truthsocial.com/api/v1/accounts/lookup?acct={username}"
TRUTHSOCIAL_TIMELINE = "https://truthsocial.com/api/v1/accounts/{account_id}/statuses?limit=20"
TRUTHSOCIAL_URL = "https://truthsocial.com/@{username}"


def strip_html(html: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r"<[^>]+>", "", html).strip()
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#39;", "'", text)
    text = re.sub(r"&nbsp;", " ", text)
    return text


class PlaywrightMonitor:
    """Monitor Truth Social using cloudscraper (API) + Playwright (screenshots)."""

    def __init__(
        self,
        username: str,
        on_post: Callable,
        interval: int = 60,
        screenshot_dir: str = "screenshots",
    ):
        self.username = username.lower()
        self.on_post = on_post
        self.interval = interval
        self.screenshot_dir = screenshot_dir
        self._running = False
        self._seen_ids: set = set()
        self._account_id: Optional[str] = None
        self._scraper = None
        self._browser = None
        self._context = None
        self._page = None
        self._pw = None

        os.makedirs(screenshot_dir, exist_ok=True)

    # ── cloudscraper setup ───────────────────────────────────────

    def _init_scraper(self):
        """Initialize cloudscraper session."""
        try:
            import cloudscraper
            self._scraper = cloudscraper.create_scraper(
                browser={
                    "browser": "chrome",
                    "platform": "windows",
                    "desktop": True,
                },
                delay=5,
            )
            logger.info("cloudscraper: session created")
            return True
        except ImportError:
            logger.error("cloudscraper not installed! pip install cloudscraper")
            return False
        except Exception as e:
            logger.error(f"cloudscraper init error: {e}")
            return False

    def _fetch_account_id(self) -> Optional[str]:
        """Fetch account ID using cloudscraper."""
        try:
            url = TRUTHSOCIAL_API.format(username=self.username)
            resp = self._scraper.get(url, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                account_id = str(data.get("id", ""))
                if account_id:
                    logger.info(f"cloudscraper: account @{self.username} -> ID {account_id}")
                    return account_id
            logger.warning(f"cloudscraper: lookup returned HTTP {resp.status_code}")
        except Exception as e:
            logger.error(f"cloudscraper lookup error: {e}")
        return None

    def _fetch_statuses(self) -> list:
        """Fetch latest statuses using cloudscraper."""
        if not self._account_id:
            return []
        try:
            url = TRUTHSOCIAL_TIMELINE.format(account_id=self._account_id)
            resp = self._scraper.get(url, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
            logger.warning(f"cloudscraper: statuses returned HTTP {resp.status_code}")
        except Exception as e:
            logger.error(f"cloudscraper statuses error: {e}")
        return []

    # ── Main entry point ─────────────────────────────────────────

    async def start(self):
        """Start monitoring."""
        self._running = True

        # Step 1: Init cloudscraper
        if not self._init_scraper():
            logger.error("cloudscraper init failed. Cannot monitor.")
            return

        # Step 2: Get account ID
        loop = asyncio.get_event_loop()
        self._account_id = await loop.run_in_executor(None, self._fetch_account_id)

        if not self._account_id:
            logger.warning("First attempt failed. Retrying in 15s...")
            await asyncio.sleep(15)
            self._account_id = await loop.run_in_executor(None, self._fetch_account_id)

        if not self._account_id:
            logger.error("Failed to get account ID. Monitor cannot work.")
            return

        # Step 3: Mark existing posts as seen
        await self._initial_fetch()

        # Step 4: Init Playwright for screenshots (best-effort)
        await self._init_playwright()

        # Step 5: Run polling loop
        await self._poll_loop()

    # ── Playwright init (for screenshots only) ───────────────────

    async def _init_playwright(self):
        """Initialize Playwright browser for screenshots. Non-fatal if fails."""
        try:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            self._page = await self._context.new_page()

            # Apply stealth patches
            await self._apply_stealth(self._page)

            # Navigate to profile page
            url = TRUTHSOCIAL_URL.format(username=self.username)
            logger.info(f"Playwright: navigating to {url}")
            try:
                await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)
                title = await self._page.title()
                logger.info(f"Playwright: page title = {title}")
            except Exception as e:
                logger.warning(f"Playwright: navigation error (screenshots may fail): {e}")

            logger.info("Playwright: ready for screenshots")

        except ImportError:
            logger.warning("Playwright not available. Screenshots disabled.")
        except Exception as e:
            logger.warning(f"Playwright init error (screenshots disabled): {e}")

    async def _apply_stealth(self, page):
        """Apply stealth JavaScript to avoid bot detection."""
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [
                    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                    { name: 'Native Client', filename: 'internal-nacl-plugin' },
                ],
            });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
            window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
            delete navigator.__proto__.webdriver;
        """)

    # ── Polling ──────────────────────────────────────────────────

    async def _initial_fetch(self):
        """Mark existing posts as seen."""
        loop = asyncio.get_event_loop()
        statuses = await loop.run_in_executor(None, self._fetch_statuses)
        for status in statuses:
            self._seen_ids.add(str(status.get("id", "")))
        logger.info(f"Marked {len(self._seen_ids)} existing posts as seen")

    async def _poll_loop(self):
        """Poll for new posts every interval seconds."""
        await asyncio.sleep(5)

        while self._running:
            try:
                loop = asyncio.get_event_loop()
                statuses = await loop.run_in_executor(None, self._fetch_statuses)

                if not statuses:
                    # Try DOM scraping as fallback
                    statuses = await self._scrape_from_dom()

                new_count = 0
                for status in statuses:
                    post_id = str(status.get("id", ""))
                    if not post_id or post_id in self._seen_ids:
                        continue

                    self._seen_ids.add(post_id)
                    new_count += 1

                    content = strip_html(status.get("content", ""))
                    account = status.get("account", {})
                    username = account.get("acct", account.get("username", self.username))

                    post = Post(
                        id=post_id,
                        username=username,
                        content=content,
                        created_at=status.get("created_at", ""),
                        url=status.get("url", f"https://truthsocial.com/@{self.username}/{post_id}"),
                        sensitive=status.get("sensitive", False),
                        spoiler_text=status.get("spoiler_text", ""),
                        media_urls=[
                            m.get("url", "")
                            for m in status.get("media_attachments", [])
                            if m.get("url")
                        ],
                        source="cloudscraper",
                    )

                    # Take screenshot
                    post.screenshot_path = await self._take_screenshot(post_id)

                    await self.on_post(post)

                if new_count > 0:
                    logger.info(f"Poll: {new_count} new posts")

            except Exception as e:
                logger.error(f"Poll error: {e}")

            await asyncio.sleep(self.interval)

    # ── DOM scraping fallback ────────────────────────────────────

    async def _scrape_from_dom(self) -> list:
        """Scrape posts from the profile page DOM using Playwright."""
        if not self._page:
            return []

        try:
            # Reload to get fresh content
            try:
                await self._page.reload(wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(3)
            except Exception:
                pass

            statuses = await self._page.evaluate("""
                () => {
                    const posts = [];
                    const articles = document.querySelectorAll('article, [data-testid="status"], .status-body, div[class*="status"]');

                    for (const article of articles) {
                        try {
                            // Find post link with ID
                            const links = article.querySelectorAll('a');
                            let postId = null;
                            let postUrl = null;

                            for (const link of links) {
                                const href = link.href || '';
                                const match = href.match(/@(\\w+)\\/(\\d+)$/);
                                if (match) {
                                    postId = match[2];
                                    postUrl = href;
                                    break;
                                }
                            }

                            if (!postId) continue;

                            // Get text content
                            const contentEl = article.querySelector('p, [data-testid="status-content"], .status-content');
                            const content = contentEl ? contentEl.textContent.trim() : article.textContent.trim().substring(0, 500);

                            // Get timestamp
                            const timeEl = article.querySelector('time');
                            const createdAt = timeEl ? (timeEl.getAttribute('datetime') || timeEl.textContent) : '';

                            posts.push({
                                id: postId,
                                content: content,
                                created_at: createdAt,
                                url: postUrl,
                                account: { acct: 'realdonaldtrump', username: 'realdonaldtrump' },
                                media_attachments: [],
                                sensitive: false,
                                spoiler_text: ''
                            });
                        } catch(e) {}
                    }

                    return posts;
                }
            """)

            if statuses:
                logger.info(f"DOM scraping: found {len(statuses)} posts")
            return statuses

        except Exception as e:
            logger.error(f"DOM scraping error: {e}")
        return []

    # ── Screenshot ───────────────────────────────────────────────

    async def _take_screenshot(self, post_id: str) -> Optional[str]:
        """Take a screenshot of a post."""
        if not self._page:
            return None

        try:
            post_url = f"https://truthsocial.com/@{self.username}/{post_id}"
            await self._page.goto(post_url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(3)

            post_el = await self._page.query_selector("article, .status-body, [data-testid='post']")

            filepath = os.path.join(
                self.screenshot_dir,
                f"post_{post_id}_{int(datetime.now().timestamp())}.png",
            )

            if post_el:
                await post_el.screenshot(path=filepath)
            else:
                await self._page.screenshot(path=filepath, full_page=False)

            logger.info(f"Screenshot saved: {filepath}")
            return filepath

        except Exception as e:
            logger.error(f"Screenshot error: {e}")
            return None

    # ── Cleanup ──────────────────────────────────────────────────

    def stop(self):
        self._running = False
        logger.info("Monitor stopped")
