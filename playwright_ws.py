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

        # Try initial scrape
        success = await self._initial_scrape()

        if not success:
            # Cloudflare blocked — wait long before retrying
            logger.info("Initial scrape failed. Waiting 10 min before polling...")
            await asyncio.sleep(600)

        # Start polling
        await self._poll_loop()

    async def _init_browser(self) -> bool:
        try:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()

            # Firefox headless — works best with Cloudflare
            logger.info("Starting Firefox...")
            self._browser = await self._pw.firefox.launch(headless=True)
            self._context = await self._browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
                locale="en-US",
            )
            self._page = await self._context.new_page()
            logger.info("Firefox ready")
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
                # Use commit — earliest state, just wait for first HTTP response
                await self._page.goto(url, wait_until="commit", timeout=30000)
                await asyncio.sleep(3)

                title = await self._page.title()
                logger.info(f"Page title: {title}")

                if "Just a moment" not in title and "Cloudflare" not in title:
                    logger.info("Cloudflare passed!")
                    return True

                # Wait for Cloudflare to auto-resolve
                logger.info("Waiting for Cloudflare...")
                for i in range(12):  # 60 seconds
                    await asyncio.sleep(5)
                    title = await self._page.title()
                    if "Just a moment" not in title and "Cloudflare" not in title:
                        logger.info("Cloudflare passed!")
                        return True
                    logger.debug(f"Still waiting... ({(i+1)*5}s)")

                # Try reload
                if attempt < 2:
                    logger.info("Reloading...")
                    try:
                        await self._page.reload(wait_until="commit", timeout=15000)
                        await asyncio.sleep(5)
                        title = await self._page.title()
                        if "Just a moment" not in title and "Cloudflare" not in title:
                            logger.info("Cloudflare passed after reload!")
                            return True
                    except Exception:
                        pass

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

    async def _initial_scrape(self) -> bool:
        """Load page, fetch posts, send latest to Telegram. Returns True if successful."""
        if not await self._load_page():
            return False

        statuses = await self._fetch_posts()
        if not statuses:
            await asyncio.sleep(10)
            statuses = await self._fetch_posts()

        if not statuses:
            logger.warning("No posts found")
            return False

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
        return True

    async def _poll_loop(self):
        """Poll for new posts."""
        await asyncio.sleep(self.interval)
        cf_failures = 0

        while self._running:
            try:
                statuses = await self._fetch_posts()

                if not statuses:
                    cf_failures += 1
                    if cf_failures >= 3:
                        # Cloudflare is blocking — wait longer
                        logger.info(f"Cloudflare blocking ({cf_failures}x). Waiting 10 min...")
                        await asyncio.sleep(600)
                        # Try reloading page
                        await self._load_page()
                        cf_failures = 0
                    else:
                        logger.info("No statuses, reloading page...")
                        if await self._load_page():
                            statuses = await self._fetch_posts()
                else:
                    cf_failures = 0  # Reset on success

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

            # Try to find the post element with multiple selectors
            post_el = None
            selectors = [
                "article",
                ".status-body",
                "[data-testid='status']",
                "div[class*='status']",
                "div[class*='post']",
            ]
            for sel in selectors:
                post_el = await self._page.query_selector(sel)
                if post_el:
                    # Verify it's actually the post (not navbar etc)
                    box = await post_el.bounding_box()
                    if box and box['height'] > 100 and box['width'] > 200:
                        logger.debug(f"Found post element with selector: {sel}")
                        break
                    post_el = None

            if post_el:
                # Screenshot just the post element
                await post_el.screenshot(path=filepath)
                logger.info(f"Screenshot (cropped): {filepath}")
            else:
                # Fallback: full page screenshot + crop with Pillow
                full_path = filepath + ".full.png"
                await self._page.screenshot(path=full_path, full_page=False)
                filepath = await self._crop_screenshot(full_path, filepath, post_id)
                logger.info(f"Screenshot (cropped from full): {filepath}")

            return filepath
        except Exception as e:
            logger.error(f"Screenshot error: {e}")
            return None

    async def _crop_screenshot(self, full_path: str, output_path: str, post_id: str) -> str:
        """Crop full page screenshot to just the post area using Pillow."""
        try:
            from PIL import Image

            img = Image.open(full_path)
            width, height = img.size

            # Find the post element's position on the page
            box = await self._page.evaluate("""
                (postId) => {
                    // Try to find the post container
                    const selectors = ['article', '.status-body', '[data-testid="status"]', 'div[class*="status"]'];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el) {
                            const rect = el.getBoundingClientRect();
                            if (rect.height > 100 && rect.width > 200) {
                                return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
                            }
                        }
                    }
                    // Fallback: crop center of page
                    return null;
                }
            """, post_id)

            if box and box.get('height', 0) > 50:
                # Crop to the post element
                left = max(0, int(box['x']))
                top = max(0, int(box['y']))
                right = min(width, int(box['x'] + box['width']))
                bottom = min(height, int(box['y'] + box['height']))
                cropped = img.crop((left, top, right, bottom))
                cropped.save(output_path)
            else:
                # Fallback: crop center portion of the page (typical post area)
                # Remove navbar (top ~60px) and sidebar, keep center column
                left = max(0, width // 2 - 400)
                top = 60
                right = min(width, width // 2 + 400)
                bottom = min(height, height - 50)
                cropped = img.crop((left, top, right, bottom))
                cropped.save(output_path)

            # Clean up full screenshot
            try:
                os.remove(full_path)
            except Exception:
                pass

            return output_path

        except ImportError:
            logger.warning("Pillow not installed, using full screenshot")
            os.rename(full_path, output_path)
            return output_path
        except Exception as e:
            logger.error(f"Crop error: {e}")
            try:
                os.rename(full_path, output_path)
            except Exception:
                pass
            return output_path

    def stop(self):
        self._running = False
