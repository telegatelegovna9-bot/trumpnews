"""Truth Social monitor — curl_cffi (primary) + Playwright (screenshots).

curl_cffi impersonates Chrome's TLS fingerprint — bypasses Cloudflare.
Playwright is used only for screenshots when curl_cffi works.
"""
import asyncio
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

# Proxy support — set PROXY_URL in .env
# Examples:
#   http://user:pass@proxy.example.com:8080
#   socks5://user:pass@proxy.example.com:1080
PROXY_URL = os.getenv("PROXY_URL", "")

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


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
        self._account_id: Optional[str] = None
        self._scraper = None
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        os.makedirs(screenshot_dir, exist_ok=True)

    async def start(self):
        self._running = True

        # Init curl_cffi
        if not self._init_curl():
            logger.error("curl_cffi init failed")
            return

        # Get account ID via curl_cffi
        loop = asyncio.get_event_loop()
        self._account_id = await loop.run_in_executor(None, self._fetch_account_id)

        if not self._account_id:
            logger.info("curl_cffi blocked. Trying Playwright Firefox...")
            if await self._try_playwright_api():
                logger.info("Playwright API worked!")
            else:
                logger.warning("Both methods blocked. Waiting 10 min...")
                await asyncio.sleep(600)
                # Try once more
                self._account_id = await loop.run_in_executor(None, self._fetch_account_id)
                if not self._account_id:
                    logger.error("Still blocked. Monitor will keep retrying.")

        if not self._account_id:
            # Start poll loop anyway — will retry
            await self._poll_loop()
            return

        # Mark existing posts
        await self._initial_fetch()

        # Init Playwright for screenshots (best-effort)
        await self._init_playwright()

        # Start polling
        await self._poll_loop()

    async def _try_playwright_api(self) -> bool:
        """Try to get account ID via Playwright Firefox (bypasses Cloudflare sometimes)."""
        try:
            from playwright.async_api import async_playwright

            pw = await async_playwright().start()

            launch_args = {}
            if PROXY_URL:
                launch_args["proxy"] = {"server": PROXY_URL}

            browser = await pw.firefox.launch(headless=True, **launch_args)
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
            )
            page = await context.new_page()

            url = TRUTHSOCIAL_URL.format(username=self.username)
            await page.goto(url, wait_until="commit", timeout=30000)
            await asyncio.sleep(5)

            title = await page.title()
            logger.info(f"Playwright fallback: title = {title}")

            if "Just a moment" in title or "Cloudflare" in title:
                # Wait for Cloudflare
                for i in range(12):
                    await asyncio.sleep(5)
                    title = await page.title()
                    if "Just a moment" not in title and "Cloudflare" not in title:
                        break

            if "Just a moment" in title:
                await browser.close()
                await pw.stop()
                return False

            # Try to get account ID via browser API
            account = await page.evaluate("""
                async () => {
                    const r = await fetch('/api/v1/accounts/lookup?acct=""" + self.username + """', {credentials:'include'});
                    if (!r.ok) return null;
                    return await r.json();
                }
            """)

            if account and account.get("id"):
                self._account_id = str(account["id"])
                logger.info(f"Playwright fallback: account ID = {self._account_id}")
                # Store for later use
                self._pw = pw
                self._browser = browser
                self._context = context
                self._page = page
                return True

            await browser.close()
            await pw.stop()
            return False

        except Exception as e:
            logger.error(f"Playwright fallback error: {e}")
            return False

    def _init_curl(self) -> bool:
        try:
            from curl_cffi.requests import Session

            # Try multiple impersonation profiles
            profiles = ["chrome131", "chrome120", "chrome116", "safari17_0", "safari15_5", "edge101"]

            for profile in profiles:
                try:
                    kwargs = {"impersonate": profile, "headers": HEADERS}
                    if PROXY_URL:
                        kwargs["proxies"] = {"https": PROXY_URL, "http": PROXY_URL}

                    self._scraper = Session(**kwargs)
                    # Quick test
                    resp = self._scraper.get("https://truthsocial.com/", timeout=10)
                    logger.info(f"curl_cffi {profile}: HTTP {resp.status_code}")
                    if resp.status_code != 403:
                        logger.info(f"Using profile: {profile}")
                        return True
                except Exception as e:
                    logger.debug(f"Profile {profile} failed: {e}")
                    continue

            # All profiles got 403 — use last one anyway
            logger.warning("All profiles got 403, using chrome131 anyway")
            kwargs = {"impersonate": "chrome131", "headers": HEADERS}
            if PROXY_URL:
                kwargs["proxies"] = {"https": PROXY_URL, "http": PROXY_URL}
            self._scraper = Session(**kwargs)
            return True

        except ImportError:
            logger.error("curl_cffi not installed")
            return False
        except Exception as e:
            logger.error(f"curl_cffi init error: {e}")
            return False

    def _fetch_account_id(self) -> Optional[str]:
        try:
            url = TRUTHSOCIAL_API.format(username=self.username)
            resp = self._scraper.get(url, timeout=20)
            logger.info(f"Account lookup: HTTP {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                account_id = str(data.get("id", ""))
                if account_id:
                    logger.info(f"Account ID: {account_id}")
                    return account_id
            elif resp.status_code == 403:
                logger.warning("Cloudflare blocking (403)")
            else:
                logger.warning(f"Unexpected status: {resp.status_code}")
        except Exception as e:
            logger.error(f"Account lookup error: {e}")
        return None

    def _fetch_statuses(self) -> list:
        if not self._account_id:
            return []
        try:
            url = TRUTHSOCIAL_TIMELINE.format(account_id=self._account_id)
            resp = self._scraper.get(url, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
            elif resp.status_code == 403:
                logger.warning("Statuses: Cloudflare blocking (403)")
        except Exception as e:
            logger.error(f"Statuses error: {e}")
        return []

    async def _init_playwright(self):
        try:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            self._browser = await self._pw.firefox.launch(headless=True)
            self._context = await self._browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
            )
            self._page = await self._context.new_page()
            logger.info("Playwright ready for screenshots")
        except Exception as e:
            logger.warning(f"Playwright init failed (screenshots disabled): {e}")

    async def _initial_fetch(self):
        loop = asyncio.get_event_loop()
        statuses = await loop.run_in_executor(None, self._fetch_statuses)

        if not statuses:
            logger.warning("No posts found")
            return

        # Send latest post
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

        for s in statuses:
            self._seen_ids.add(str(s.get("id", "")))
        logger.info(f"Marked {len(self._seen_ids)} posts as seen")

    async def _poll_loop(self):
        await asyncio.sleep(self.interval)
        cf_failures = 0

        while self._running:
            try:
                loop = asyncio.get_event_loop()
                statuses = await loop.run_in_executor(None, self._fetch_statuses)

                if not statuses:
                    cf_failures += 1
                    if cf_failures >= 5:
                        logger.info(f"Cloudflare blocking ({cf_failures}x). Waiting 10 min...")
                        await asyncio.sleep(600)
                        cf_failures = 0
                    continue
                else:
                    cf_failures = 0

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

            filepath = os.path.join(self.screenshot_dir, f"post_{post_id}_{int(datetime.now().timestamp())}.png")

            post_el = None
            for sel in ["article", ".status-body", "[data-testid='status']"]:
                post_el = await self._page.query_selector(sel)
                if post_el:
                    box = await post_el.bounding_box()
                    if box and box['height'] > 100:
                        break
                    post_el = None

            if post_el:
                await post_el.screenshot(path=filepath)
            else:
                await self._page.screenshot(path=filepath, full_page=False)
                filepath = await self._crop_screenshot(filepath, filepath, post_id)

            logger.info(f"Screenshot: {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"Screenshot error: {e}")
            return None

    async def _crop_screenshot(self, full_path: str, output_path: str, post_id: str) -> str:
        try:
            from PIL import Image
            img = Image.open(full_path)
            width, height = img.size

            box = await self._page.evaluate("""
                () => {
                    for (const sel of ['article', '.status-body', '[data-testid="status"]']) {
                        const el = document.querySelector(sel);
                        if (el) {
                            const r = el.getBoundingClientRect();
                            if (r.height > 100) return {x: r.x, y: r.y, w: r.width, h: r.height};
                        }
                    }
                    return null;
                }
            """)

            if box and box.get('h', 0) > 50:
                cropped = img.crop((max(0, int(box['x'])), max(0, int(box['y'])),
                                   min(width, int(box['x'] + box['w'])), min(height, int(box['y'] + box['h']))))
                cropped.save(output_path)
            else:
                left = max(0, width // 2 - 400)
                cropped = img.crop((left, 60, min(width, left + 800), min(height, height - 50)))
                cropped.save(output_path)

            return output_path
        except Exception as e:
            logger.error(f"Crop error: {e}")
            return full_path

    def stop(self):
        self._running = False
