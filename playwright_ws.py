"""Hybrid monitor: curl_cffi for API polling + Playwright for screenshots.

curl_cffi impersonates Chrome's TLS fingerprint, bypassing Cloudflare.
Playwright is used ONLY for taking screenshots (not for API requests).

Architecture:
- curl_cffi polls Truth Social API every 60 seconds (bypasses Cloudflare)
- When a new post is found, Playwright takes a screenshot of it
- CDP WebSocket interception is also attempted for real-time detection
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
    """Monitor Truth Social using curl_cffi (API) + Playwright (screenshots)."""

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
        self._browser = None
        self._context = None
        self._page = None
        self._pw = None
        self._cdp = None
        self._curl_session = None

        os.makedirs(screenshot_dir, exist_ok=True)

    # ── curl_cffi HTTP client (Cloudflare bypass) ────────────────

    def _get_curl_session(self):
        """Get or create curl_cffi session with Chrome impersonation."""
        if self._curl_session is None:
            from curl_cffi.requests import Session
            self._curl_session = Session(impersonate="chrome120")
            logger.info("curl_cffi: session created (impersonate=chrome120)")
        return self._curl_session

    def _curl_get_json(self, url: str) -> dict | list | None:
        """Make a GET request using curl_cffi and return JSON."""
        try:
            session = self._get_curl_session()
            resp = session.get(url, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            else:
                logger.warning(f"curl_cffi: {url} returned HTTP {resp.status_code}")
                return None
        except Exception as e:
            logger.error(f"curl_cffi request error: {e}")
            return None

    def _fetch_account_id_sync(self) -> Optional[str]:
        """Fetch account ID using curl_cffi."""
        url = TRUTHSOCIAL_API.format(username=self.username)
        data = self._curl_get_json(url)
        if data and isinstance(data, dict):
            account_id = str(data.get("id", ""))
            if account_id:
                logger.info(f"curl_cffi: account @{self.username} -> ID {account_id}")
                return account_id
        logger.error("curl_cffi: failed to get account ID")
        return None

    def _fetch_statuses_sync(self) -> list:
        """Fetch latest statuses using curl_cffi."""
        if not self._account_id:
            return []
        url = TRUTHSOCIAL_TIMELINE.format(account_id=self._account_id)
        data = self._curl_get_json(url)
        if isinstance(data, list):
            return data
        return []

    # ── Main entry point ─────────────────────────────────────────

    async def start(self):
        """Start monitoring."""
        self._running = True

        # Step 1: Get account ID via curl_cffi (bypasses Cloudflare)
        loop = asyncio.get_event_loop()
        self._account_id = await loop.run_in_executor(None, self._fetch_account_id_sync)

        if not self._account_id:
            logger.error("Cannot get account ID. Retrying in 30s...")
            await asyncio.sleep(30)
            self._account_id = await loop.run_in_executor(None, self._fetch_account_id_sync)

        if not self._account_id:
            logger.error("Failed to get account ID after retry. Monitor cannot work.")
            return

        # Step 2: Mark existing posts as seen
        await self._initial_fetch()

        # Step 3: Start Playwright for screenshots (best-effort)
        playwright_ready = await self._init_playwright()

        # Step 4: Run polling + optional WS interception
        tasks = [self._poll_loop()]
        if playwright_ready:
            tasks.append(self._ws_intercept_loop())

        await asyncio.gather(*tasks, return_exceptions=True)

    # ── Playwright init (for screenshots + WS interception) ──────

    async def _init_playwright(self) -> bool:
        """Initialize Playwright browser for screenshots. Returns True if ready."""
        try:
            from playwright.async_api import async_playwright
            from playwright_stealth import stealth_async

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
            await stealth_async(self._page)
            logger.info("Playwright: stealth mode enabled")

            # Setup CDP for WS interception
            try:
                cdp = await self._context.new_cdp_session(self._page)
                await cdp.send("Network.enable")
                cdp.on("Network.webSocketFrameReceived", self._on_ws_frame)
                self._cdp = cdp
                logger.info("Playwright: CDP WebSocket interception enabled")
            except Exception as e:
                logger.warning(f"Playwright: CDP setup failed: {e}")

            # Navigate to profile page
            url = TRUTHSOCIAL_URL.format(username=self.username)
            logger.info(f"Playwright: navigating to {url}")

            try:
                await self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(5)

                title = await self._page.title()
                logger.info(f"Playwright: page title = {title}")

                if "Just a moment" in title or "Cloudflare" in title:
                    logger.info("Playwright: waiting for Cloudflare challenge...")
                    await asyncio.sleep(10)

                logger.info("Playwright: browser ready for screenshots")
                return True

            except Exception as e:
                logger.warning(f"Playwright: navigation failed: {e}")
                # Even if navigation fails, we can still try screenshots later
                return True

        except ImportError as e:
            logger.warning(f"Playwright not available: {e}")
            return False
        except Exception as e:
            logger.error(f"Playwright init error: {e}")
            return False

    # ── WebSocket interception (via CDP) ─────────────────────────

    def _on_ws_frame(self, params):
        """Handle intercepted WebSocket frame from CDP."""
        try:
            response = params.get("response", {})
            payload = response.get("payloadData", "")
            if not payload:
                return

            data = json.loads(payload)
            if data.get("event") != "update":
                return

            inner = json.loads(data.get("payload", "{}"))
            account = inner.get("account", {})
            acct = account.get("acct", "").lower()
            uname = account.get("username", "").lower()

            if acct != self.username and uname != self.username:
                return

            post_id = str(inner.get("id", ""))
            if not post_id or post_id in self._seen_ids:
                return

            content = strip_html(inner.get("content", ""))

            post = Post(
                id=post_id,
                username=self.username,
                content=content,
                created_at=inner.get("created_at", ""),
                url=inner.get("url", f"https://truthsocial.com/@{self.username}/{post_id}"),
                sensitive=inner.get("sensitive", False),
                spoiler_text=inner.get("spoiler_text", ""),
                media_urls=[
                    m.get("url", "")
                    for m in inner.get("media_attachments", [])
                    if m.get("url")
                ],
                source="playwright_ws",
            )

            logger.info(f"WS intercepted new post: {post_id}")
            # Schedule async handler
            asyncio.create_task(self._handle_ws_post(post))

        except (json.JSONDecodeError, Exception):
            pass

    async def _handle_ws_post(self, post: Post):
        """Handle a post detected via WS interception."""
        if post.id in self._seen_ids:
            return
        self._seen_ids.add(post.id)
        post.screenshot_path = await self._take_screenshot(post.id)
        await self.on_post(post)

    async def _ws_intercept_loop(self):
        """Keep the browser alive for WS interception and screenshots."""
        while self._running:
            try:
                # Periodically refresh the page to keep it alive
                await asyncio.sleep(300)  # every 5 min
                if self._page and self._running:
                    try:
                        await self._page.reload(wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(60)

    # ── API polling via curl_cffi ────────────────────────────────

    async def _initial_fetch(self):
        """Mark existing posts as seen (no notifications)."""
        loop = asyncio.get_event_loop()
        statuses = await loop.run_in_executor(None, self._fetch_statuses_sync)

        for status in statuses:
            self._seen_ids.add(str(status.get("id", "")))
        logger.info(f"Marked {len(self._seen_ids)} existing posts as seen")

    async def _poll_loop(self):
        """Poll for new posts using curl_cffi."""
        # Short delay before first poll
        await asyncio.sleep(10)

        while self._running:
            try:
                await self._poll()
            except Exception as e:
                logger.error(f"Poll error: {e}")
            await asyncio.sleep(self.interval)

    async def _poll(self):
        """Poll for new posts."""
        loop = asyncio.get_event_loop()
        statuses = await loop.run_in_executor(None, self._fetch_statuses_sync)

        if not statuses:
            return

        new_count = 0
        for status in statuses:
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
                source="curl_cffi_poll",
            )

            # Take screenshot via Playwright (if available)
            post.screenshot_path = await self._take_screenshot(post_id)

            await self.on_post(post)

        if new_count > 0:
            logger.info(f"Poll: {new_count} new posts")

    # ── Screenshot via Playwright ────────────────────────────────

    async def _take_screenshot(self, post_id: str) -> Optional[str]:
        """Take a screenshot of a post using Playwright."""
        if not self._page:
            return None

        try:
            # Navigate to the post page directly
            post_url = f"https://truthsocial.com/@{self.username}/{post_id}"
            await self._page.goto(post_url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(3)

            # Try to find the post/article element
            post_el = await self._page.query_selector("article, .status-body, [data-testid='post']")

            if post_el:
                filepath = os.path.join(
                    self.screenshot_dir,
                    f"post_{post_id}_{int(datetime.now().timestamp())}.png",
                )
                await post_el.screenshot(path=filepath)
                logger.info(f"Screenshot saved: {filepath}")
                return filepath
            else:
                # Full page screenshot as fallback
                filepath = os.path.join(
                    self.screenshot_dir,
                    f"post_{post_id}_{int(datetime.now().timestamp())}.png",
                )
                await self._page.screenshot(path=filepath, full_page=False)
                logger.info(f"Full page screenshot saved: {filepath}")
                return filepath

        except Exception as e:
            logger.error(f"Screenshot error: {e}")
            return None

    # ── Cleanup ──────────────────────────────────────────────────

    async def _cleanup(self):
        """Clean up resources."""
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
        try:
            if self._curl_session:
                self._curl_session.close()
        except Exception:
            pass
        logger.info("Playwright cleaned up")

    def stop(self):
        self._running = False
        logger.info("Monitor stopped")
