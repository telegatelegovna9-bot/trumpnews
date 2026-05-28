"""Playwright-based Truth Social monitor.

Simple approach: load page, wait for Cloudflare, scrape DOM, take screenshots.
No overengineering — just what works.
"""
import asyncio
import logging
import os
import re
from datetime import datetime
from typing import Callable, Optional

from models import Post

logger = logging.getLogger(__name__)

TRUTHSOCIAL_URL = "https://truthsocial.com/@{username}"


def strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", "", html).strip()
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#39;", "'", text)
    text = re.sub(r"&nbsp;", " ", text)
    return text


class PlaywrightMonitor:
    def __init__(self, username: str, on_post: Callable, interval: int = 60, screenshot_dir: str = "screenshots"):
        self.username = username.lower()
        self.on_post = on_post
        self.interval = interval
        self.screenshot_dir = screenshot_dir
        self._running = False
        self._seen_ids: set = set()
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        os.makedirs(screenshot_dir, exist_ok=True)

    async def start(self):
        self._running = True

        if not await self._init_browser():
            return

        await self._initial_scrape()
        await self._poll_loop()

    async def _init_browser(self) -> bool:
        try:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=[
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-infobars",
                    "--window-size=1920,1080",
                ],
            )
            self._context = await self._browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-US",
                timezone_id="America/New_York",
            )
            self._page = await self._context.new_page()

            # Stealth
            await self._page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                delete navigator.__proto__.webdriver;
                Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
                Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
            """)
            logger.info("Browser ready")
            return True

        except Exception as e:
            logger.error(f"Browser init error: {e}")
            return False

    async def _load_page(self) -> bool:
        """Navigate to profile page and wait for Cloudflare. Returns True if page loaded."""
        url = TRUTHSOCIAL_URL.format(username=self.username)

        for attempt in range(3):
            try:
                logger.info(f"Loading page (attempt {attempt+1}/3)...")
                await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(5)

                title = await self._page.title()
                logger.info(f"Page title: {title}")

                if "Just a moment" not in title and "Cloudflare" not in title:
                    logger.info("Cloudflare passed!")
                    return True

                # Wait more — sometimes it auto-resolves
                logger.info("Waiting for Cloudflare...")
                for i in range(6):
                    await asyncio.sleep(5)
                    title = await self._page.title()
                    if "Just a moment" not in title and "Cloudflare" not in title:
                        logger.info("Cloudflare passed!")
                        return True

                # Try reload
                if attempt < 2:
                    logger.info("Reloading...")
                    await self._page.reload(wait_until="domcontentloaded", timeout=20000)
                    await asyncio.sleep(3)

            except Exception as e:
                logger.warning(f"Navigation error: {e}")
                if attempt < 2:
                    await asyncio.sleep(10)

        logger.warning("Could not pass Cloudflare")
        return False

    async def _fetch_posts(self) -> list:
        """Fetch posts via browser API (has Cloudflare cookies)."""
        try:
            # Get account ID
            account = await self._page.evaluate("""
                async () => {
                    const r = await fetch('/api/v1/accounts/lookup?acct=""" + self.username + """', {credentials:'include'});
                    if (!r.ok) return null;
                    return await r.json();
                }
            """)
            if not account or not account.get("id"):
                logger.warning("Could not get account ID")
                return []

            account_id = str(account["id"])
            logger.info(f"Account ID: {account_id}")

            # Get statuses
            statuses = await self._page.evaluate("""
                async (id) => {
                    const r = await fetch('/api/v1/accounts/' + id + '/statuses?limit=20', {credentials:'include'});
                    if (!r.ok) return [];
                    return await r.json();
                }
            """, account_id)

            if isinstance(statuses, list):
                logger.info(f"Got {len(statuses)} statuses")
                return statuses

        except Exception as e:
            logger.error(f"Fetch error: {e}")
        return []

    async def _initial_scrape(self):
        """Load page, fetch posts, send latest to Telegram."""
        if not await self._load_page():
            return

        statuses = await self._fetch_posts()
        if not statuses:
            # Wait a bit and retry
            await asyncio.sleep(10)
            statuses = await self._fetch_posts()

        if not statuses:
            logger.warning("No posts found")
            return

        # Send latest post to Telegram
        latest = statuses[0]
        post_id = str(latest.get("id", ""))
        content = strip_html(latest.get("content", ""))

        post = Post(
            id=post_id,
            username=self.username,
            content=content,
            created_at=latest.get("created_at", ""),
            url=latest.get("url", f"https://truthsocial.com/@{self.username}/{post_id}"),
            sensitive=latest.get("sensitive", False),
            spoiler_text=latest.get("spoiler_text", ""),
            media_urls=[m.get("url", "") for m in latest.get("media_attachments", []) if m.get("url")],
            source="startup",
        )

        post.screenshot_path = await self._take_screenshot(post_id)
        await self.on_post(post)
        logger.info(f"Sent latest post {post_id}")

        # Mark all as seen
        for s in statuses:
            self._seen_ids.add(str(s.get("id", "")))
        logger.info(f"Marked {len(self._seen_ids)} posts as seen")

    async def _poll_loop(self):
        """Poll for new posts."""
        await asyncio.sleep(self.interval)

        while self._running:
            try:
                statuses = await self._fetch_posts()

                if not statuses:
                    # Maybe page lost cookies — reload
                    logger.info("No statuses, reloading page...")
                    if await self._load_page():
                        statuses = await self._fetch_posts()

                new_count = 0
                for status in statuses:
                    post_id = str(status.get("id", ""))
                    if not post_id or post_id in self._seen_ids:
                        continue

                    self._seen_ids.add(post_id)
                    new_count += 1

                    content = strip_html(status.get("content", ""))
                    post = Post(
                        id=post_id,
                        username=self.username,
                        content=content,
                        created_at=status.get("created_at", ""),
                        url=status.get("url", f"https://truthsocial.com/@{self.username}/{post_id}"),
                        sensitive=status.get("sensitive", False),
                        spoiler_text=status.get("spoiler_text", ""),
                        media_urls=[m.get("url", "") for m in status.get("media_attachments", []) if m.get("url")],
                        source="poll",
                    )

                    post.screenshot_path = await self._take_screenshot(post_id)
                    await self.on_post(post)

                if new_count > 0:
                    logger.info(f"Found {new_count} new posts")

            except Exception as e:
                logger.error(f"Poll error: {e}")

            await asyncio.sleep(self.interval)

    async def _take_screenshot(self, post_id: str) -> Optional[str]:
        if not self._page:
            return None
        try:
            post_url = f"https://truthsocial.com/@{self.username}/{post_id}"
            await self._page.goto(post_url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(3)

            post_el = await self._page.query_selector("article, .status-body")
            filepath = os.path.join(self.screenshot_dir, f"post_{post_id}_{int(datetime.now().timestamp())}.png")

            if post_el:
                await post_el.screenshot(path=filepath)
            else:
                await self._page.screenshot(path=filepath, full_page=False)

            logger.info(f"Screenshot: {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"Screenshot error: {e}")
            return None

    def stop(self):
        self._running = False
