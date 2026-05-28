"""Playwright-based Truth Social monitor with WebSocket interception.

This is our most reliable Cloudflare bypass — uses a real browser.
It also intercepts WebSocket frames that Truth Social sends to the browser,
providing near real-time post detection.

Additionally provides screenshot capture for posts.
"""
import asyncio
import json
import logging
import os
import re
import tempfile
from typing import Callable, Optional
from datetime import datetime, timezone

from models import Post

logger = logging.getLogger(__name__)

TRUTHSOCIAL_URL = "https://truthsocial.com/@{username}"
TRUTHSOCIAL_API = "https://truthsocial.com/api/v1/accounts/lookup?acct={username}"
TRUTHSOCIAL_TIMELINE = "https://truthsocial.com/api/v1/accounts/{account_id}/statuses?limit=20"


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
    """Monitor Truth Social using Playwright with WS interception and screenshots."""

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
        self._ws_new_posts: list = []  # Posts detected via WS interception
        self._browser = None
        self._context = None
        self._page = None
        self._pw = None
        self._cdp = None

        os.makedirs(screenshot_dir, exist_ok=True)

    async def start(self):
        """Start Playwright monitor — runs WS interception + polling in parallel."""
        self._running = True

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

            # Stealth: hide webdriver flag from Cloudflare
            await self._page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                window.chrome = { runtime: {} };
            """)

            # Setup CDP for WebSocket frame interception
            await self._setup_ws_interception()

            # Navigate to profile page
            url = TRUTHSOCIAL_URL.format(username=self.username)
            logger.info(f"Playwright: navigating to {url}")

            # Use domcontentloaded instead of networkidle — networkidle hangs
            # because Truth Social keeps WS connections open forever
            try:
                await self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as nav_err:
                logger.warning(f"Playwright: navigation failed: {nav_err}")
                # Try loading just the API directly
                logger.info("Playwright: trying API endpoint directly...")
                try:
                    await self._page.goto(
                        f"https://truthsocial.com/api/v1/accounts/lookup?acct={self.username}",
                        wait_until="commit",
                        timeout=15000,
                    )
                    logger.info("Playwright: API endpoint loaded")
                except Exception as api_err:
                    logger.error(f"Playwright: API endpoint also failed: {api_err}")
                    raise

            # Wait for Cloudflare challenge to resolve (if any)
            await asyncio.sleep(3)

            # Check if we're past Cloudflare
            try:
                page_title = await self._page.title()
                logger.info(f"Playwright: page title = {page_title}")

                if "Just a moment" in page_title or "Cloudflare" in page_title:
                    logger.info("Playwright: waiting for Cloudflare challenge...")
                    await asyncio.sleep(10)
                    # Try reload
                    await self._page.reload(wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(5)
            except Exception:
                pass  # Title check is best-effort

            logger.info("Playwright: page loaded")

            # Get account ID from API
            await self._fetch_account_id()

            # Initial fetch — mark existing posts as seen
            await self._initial_fetch()

            # Run WS interception + periodic polling concurrently
            await asyncio.gather(
                self._ws_monitor_loop(),
                self._poll_loop(),
            )

        except Exception as e:
            logger.error(f"Playwright error: {e}", exc_info=True)
        finally:
            await self._cleanup()

    async def _setup_ws_interception(self):
        """Setup CDP session to intercept WebSocket frames."""
        try:
            cdp = await self._context.new_cdp_session(self._page)

            # Enable Network domain to get WebSocket events
            await cdp.send("Network.enable")

            self._ws_frames = []

            # Listen for WebSocket frame received events
            cdp.on("Network.webSocketFrameReceived", self._on_ws_frame)
            cdp.on("Network.webSocketCreated", self._on_ws_created)

            self._cdp = cdp
            logger.info("Playwright: CDP WebSocket interception enabled")

        except Exception as e:
            logger.warning(f"Playwright: CDP setup failed (WS interception disabled): {e}")
            self._cdp = None

    def _on_ws_created(self, params):
        """Log WebSocket creation."""
        url = params.get("url", "")
        logger.info(f"WS created: {url}")

    def _on_ws_frame(self, params):
        """Handle intercepted WebSocket frame."""
        try:
            response = params.get("response", {})
            payload = response.get("payloadData", "")
            if not payload:
                return

            data = json.loads(payload)
            self._process_ws_data(data)

        except (json.JSONDecodeError, Exception):
            pass  # Not JSON or parse error — ignore

    def _process_ws_data(self, data: dict):
        """Process WebSocket data for new posts."""
        # Mastodon streaming API format
        event = data.get("event", "")
        if event != "update":
            return

        try:
            payload = json.loads(data.get("payload", "{}"))
        except (json.JSONDecodeError, TypeError):
            return

        account = payload.get("account", {})
        acct = account.get("acct", "").lower()
        uname = account.get("username", "").lower()

        if acct != self.username and uname != self.username:
            return

        post_id = str(payload.get("id", ""))
        if not post_id or post_id in self._seen_ids:
            return

        content = strip_html(payload.get("content", ""))

        post = Post(
            id=post_id,
            username=self.username,
            content=content,
            created_at=payload.get("created_at", ""),
            url=payload.get("url", f"https://truthsocial.com/@{self.username}/{post_id}"),
            sensitive=payload.get("sensitive", False),
            spoiler_text=payload.get("spoiler_text", ""),
            media_urls=[
                m.get("url", "")
                for m in payload.get("media_attachments", [])
                if m.get("url")
            ],
            source="playwright_ws",
        )

        logger.info(f"Playwright WS intercepted new post: {post_id}")
        self._ws_new_posts.append(post)

    async def _ws_monitor_loop(self):
        """Process posts detected via WS interception."""
        while self._running:
            if self._ws_new_posts:
                post = self._ws_new_posts.pop(0)
                if post.id not in self._seen_ids:
                    self._seen_ids.add(post.id)
                    try:
                        # Take screenshot of the post
                        post.screenshot_path = await self._take_post_screenshot(post.id)
                        await self.on_post(post)
                    except Exception as e:
                        logger.error(f"WS post handling error: {e}")
            else:
                await asyncio.sleep(1)

    async def _fetch_account_id(self):
        """Fetch account ID via API. Retries up to 3 times."""
        for attempt in range(3):
            try:
                resp = await self._page.evaluate(
                    """async (url) => {
                        const r = await fetch(url);
                        if (!r.ok) throw new Error('HTTP ' + r.status);
                        return await r.json();
                    }""",
                    TRUTHSOCIAL_API.format(username=self.username),
                )
                self._account_id = str(resp.get("id", ""))
                if self._account_id:
                    logger.info(f"Playwright: account ID = {self._account_id}")
                    return
                else:
                    logger.warning(f"Playwright: API returned empty account ID (attempt {attempt+1})")
            except Exception as e:
                logger.warning(f"Playwright: failed to get account ID (attempt {attempt+1}): {e}")

            if attempt < 2:
                await asyncio.sleep(5)

        logger.error("Playwright: could not fetch account ID after 3 attempts")

    async def _initial_fetch(self):
        """Mark existing posts as seen."""
        if not self._account_id:
            return

        try:
            url = TRUTHSOCIAL_TIMELINE.format(account_id=self._account_id)
            resp = await self._page.evaluate(
                """async (url) => {
                    try {
                        const r = await fetch(url);
                        if (!r.ok) return { error: 'HTTP ' + r.status };
                        return await r.json();
                    } catch(e) {
                        return { error: e.message };
                    }
                }""",
                url,
            )

            if isinstance(resp, dict) and resp.get("error"):
                logger.warning(f"Playwright initial fetch: API error: {resp['error']}")
                return

            if isinstance(resp, list):
                for status in resp:
                    self._seen_ids.add(str(status.get("id", "")))
                logger.info(f"Playwright: marked {len(self._seen_ids)} existing posts")

        except Exception as e:
            logger.error(f"Playwright initial fetch error: {e}")

    async def _poll_loop(self):
        """Periodic polling loop — fallback if WS misses something."""
        # Wait a bit before first poll
        await asyncio.sleep(30)

        while self._running:
            try:
                await self._poll()
            except Exception as e:
                logger.error(f"Playwright poll error: {e}")
            await asyncio.sleep(self.interval)

    async def _poll(self):
        """Poll for new posts via API."""
        if not self._account_id:
            # Try to get account ID if we don't have it
            await self._fetch_account_id()
            if not self._account_id:
                return

        url = TRUTHSOCIAL_TIMELINE.format(account_id=self._account_id)
        try:
            resp = await self._page.evaluate(
                """async (url) => {
                    try {
                        const r = await fetch(url);
                        if (!r.ok) return { error: 'HTTP ' + r.status };
                        return await r.json();
                    } catch(e) {
                        return { error: e.message };
                    }
                }""",
                url,
            )
        except Exception as e:
            logger.error(f"Playwright poll: evaluate failed: {e}")
            return

        if isinstance(resp, dict) and resp.get("error"):
            logger.warning(f"Playwright poll: API error: {resp['error']}")
            return

        if not isinstance(resp, list):
            return

        new_count = 0
        for status in resp:
            post_id = str(status.get("id", ""))
            if post_id in self._seen_ids:
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
                media_urls=[
                    m.get("url", "")
                    for m in status.get("media_attachments", [])
                    if m.get("url")
                ],
                source="playwright_poll",
            )

            post.screenshot_path = await self._take_post_screenshot(post_id)
            await self.on_post(post)

        if new_count > 0:
            logger.info(f"Playwright poll: {new_count} new posts")

    async def _take_post_screenshot(self, post_id: str) -> Optional[str]:
        """Take a screenshot of a specific post on the profile page."""
        try:
            # Reload the page to see the post
            await self._page.reload(wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            # Try to find the post element by data attribute or link
            post_url = f"/@{self.username}/{post_id}"
            post_el = await self._page.query_selector(
                f'[href*="{post_id}"], [data-id="{post_id}"], article:has(a[href*="{post_id}"])'
            )

            if not post_el:
                # Fallback: find by matching content link pattern
                post_el = await self._page.query_selector(f'a[href*="{post_id}"]')
                if post_el:
                    # Go up to the article/container
                    post_el = await post_el.evaluate_handle("el => el.closest('article') || el.closest('.status-body') || el.parentElement")
                    post_el = post_el.as_element()

            if post_el:
                filepath = os.path.join(
                    self.screenshot_dir,
                    f"post_{post_id}_{int(datetime.now().timestamp())}.png"
                )
                await post_el.screenshot(path=filepath)
                logger.info(f"Screenshot saved: {filepath}")
                return filepath
            else:
                # Full page screenshot as fallback
                filepath = os.path.join(
                    self.screenshot_dir,
                    f"post_{post_id}_{int(datetime.now().timestamp())}.png"
                )
                await self._page.screenshot(path=filepath, full_page=False)
                logger.info(f"Full page screenshot saved: {filepath}")
                return filepath

        except Exception as e:
            logger.error(f"Screenshot error: {e}")
            return None

    async def _cleanup(self):
        """Clean up Playwright resources."""
        try:
            if self._cdp:
                await self._cdp.detach()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        logger.info("Playwright cleaned up")

    def stop(self):
        self._running = False
        logger.info("Playwright monitor stopped")
