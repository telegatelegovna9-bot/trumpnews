"""Hybrid monitor: Playwright solves Cloudflare + screenshots, curl_cffi polls API.

Strategy:
1. Playwright loads the profile page and solves Cloudflare JS challenge
2. Extract Cloudflare clearance cookies from the browser
3. Use those cookies with curl_cffi for API polling (bypasses Cloudflare)
4. Fallback: scrape posts directly from the page DOM if API fails
5. Playwright also handles screenshots

This is the most reliable approach because:
- Playwright can solve Cloudflare JS challenges (real browser)
- curl_cffi with cookies can make fast API requests
- DOM scraping works even if API is blocked
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
    """Monitor Truth Social using Playwright (Cloudflare bypass) + curl_cffi (API with cookies)."""

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
        self._cf_cookies: list = []  # Cloudflare cookies from browser

        os.makedirs(screenshot_dir, exist_ok=True)

    # ── Main entry point ─────────────────────────────────────────

    async def start(self):
        """Start monitoring."""
        self._running = True

        # Step 1: Init Playwright and solve Cloudflare
        if not await self._init_playwright():
            logger.error("Playwright init failed. Cannot monitor.")
            return

        # Step 2: Extract cookies and setup curl_cffi
        await self._setup_curl_with_cookies()

        # Step 3: Get account ID (try curl_cffi first, then DOM scraping)
        self._account_id = await self._get_account_id()
        if not self._account_id:
            logger.warning("Could not get account ID. Will use DOM scraping only.")

        # Step 4: Mark existing posts as seen
        await self._initial_fetch()

        # Step 5: Run polling + WS interception
        await asyncio.gather(
            self._poll_loop(),
            self._ws_intercept_loop(),
            self._cookie_refresh_loop(),
            return_exceptions=True,
        )

    # ── Playwright init ──────────────────────────────────────────

    async def _init_playwright(self) -> bool:
        """Initialize Playwright, solve Cloudflare, return True if ready."""
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
                    "--disable-features=IsolateOrigins,site-per-process",
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
            except Exception as e:
                logger.warning(f"Playwright: navigation error: {e}")

            # Wait for Cloudflare challenge
            await asyncio.sleep(5)

            try:
                title = await self._page.title()
                logger.info(f"Playwright: page title = {title}")

                if "Just a moment" in title or "Cloudflare" in title:
                    logger.info("Playwright: waiting for Cloudflare challenge to solve...")
                    # Wait up to 30 seconds for challenge to resolve
                    for i in range(6):
                        await asyncio.sleep(5)
                        title = await self._page.title()
                        if "Just a moment" not in title and "Cloudflare" not in title:
                            logger.info(f"Playwright: Cloudflare challenge solved! Title: {title}")
                            break
                        logger.info(f"Playwright: still waiting for Cloudflare... ({(i+1)*5}s)")
            except Exception:
                pass

            # Extract cookies
            try:
                cookies = await self._context.cookies()
                self._cf_cookies = [
                    {"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c["path"]}
                    for c in cookies
                ]
                logger.info(f"Playwright: extracted {len(self._cf_cookies)} cookies")
            except Exception as e:
                logger.warning(f"Playwright: cookie extraction failed: {e}")

            logger.info("Playwright: browser ready")
            return True

        except ImportError as e:
            logger.error(f"Playwright not available: {e}")
            return False
        except Exception as e:
            logger.error(f"Playwright init error: {e}")
            return False

    # ── curl_cffi with browser cookies ───────────────────────────

    async def _setup_curl_with_cookies(self):
        """Setup curl_cffi session with cookies from Playwright."""
        try:
            from curl_cffi.requests import Session

            self._curl_session = Session(impersonate="chrome120")

            # Add Cloudflare cookies to the session
            for cookie in self._cf_cookies:
                self._curl_session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie["domain"],
                    path=cookie["path"],
                )

            logger.info(f"curl_cffi: session created with {len(self._cf_cookies)} cookies")

        except ImportError:
            logger.warning("curl_cffi not available")

    async def _get_account_id(self) -> Optional[str]:
        """Get account ID using multiple methods."""
        # Method 1: curl_cffi with cookies
        if self._curl_session:
            loop = asyncio.get_event_loop()
            account_id = await loop.run_in_executor(None, self._fetch_account_id_curl)
            if account_id:
                return account_id

        # Method 2: Playwright page.evaluate (browser context)
        account_id = await self._fetch_account_id_playwright()
        if account_id:
            return account_id

        # Method 3: DOM scraping from profile page
        account_id = await self._scrape_account_id_from_dom()
        return account_id

    def _fetch_account_id_curl(self) -> Optional[str]:
        """Fetch account ID using curl_cffi."""
        try:
            url = TRUTHSOCIAL_API.format(username=self.username)
            resp = self._curl_session.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                account_id = str(data.get("id", ""))
                if account_id:
                    logger.info(f"curl_cffi: account @{self.username} -> ID {account_id}")
                    return account_id
            logger.warning(f"curl_cffi: lookup returned HTTP {resp.status_code}")
        except Exception as e:
            logger.error(f"curl_cffi lookup error: {e}")
        return None

    async def _fetch_account_id_playwright(self) -> Optional[str]:
        """Fetch account ID using Playwright's page.evaluate."""
        if not self._page:
            return None

        for attempt in range(3):
            try:
                resp = await self._page.evaluate(
                    """async (url) => {
                        try {
                            const r = await fetch(url, { credentials: 'include' });
                            if (!r.ok) return { error: 'HTTP ' + r.status, status: r.status };
                            return await r.json();
                        } catch(e) {
                            return { error: e.message };
                        }
                    }""",
                    TRUTHSOCIAL_API.format(username=self.username),
                )

                if isinstance(resp, dict):
                    if resp.get("error"):
                        logger.warning(f"Playwright API: {resp['error']} (attempt {attempt+1})")
                    else:
                        account_id = str(resp.get("id", ""))
                        if account_id:
                            logger.info(f"Playwright API: account ID = {account_id}")
                            return account_id

            except Exception as e:
                logger.warning(f"Playwright API error (attempt {attempt+1}): {e}")

            if attempt < 2:
                await asyncio.sleep(5)

        return None

    async def _scrape_account_id_from_dom(self) -> Optional[str]:
        """Try to extract account ID from the profile page DOM."""
        if not self._page:
            return None

        try:
            # Look for account ID in page scripts or data attributes
            account_id = await self._page.evaluate("""
                () => {
                    // Try to find account ID in meta tags or data attributes
                    const meta = document.querySelector('meta[name="account-id"]');
                    if (meta) return meta.content;

                    // Try to find in script tags (Truth Social embeds data)
                    const scripts = document.querySelectorAll('script');
                    for (const s of scripts) {
                        const text = s.textContent;
                        const match = text.match(/"id"\\s*:\\s*"(\\d+)"/);
                        if (match) return match[1];
                    }

                    // Try to find in link tags
                    const links = document.querySelectorAll('a[href*="/api/v1/accounts/"]');
                    for (const link of links) {
                        const match = link.href.match(/accounts\\/(\\d+)/);
                        if (match) return match[1];
                    }

                    return null;
                }
            """)

            if account_id:
                logger.info(f"DOM scraping: account ID = {account_id}")
                return str(account_id)

        except Exception as e:
            logger.error(f"DOM scraping error: {e}")

        return None

    # ── Polling ──────────────────────────────────────────────────

    async def _initial_fetch(self):
        """Mark existing posts as seen."""
        statuses = await self._fetch_statuses()
        for status in statuses:
            self._seen_ids.add(str(status.get("id", "")))
        logger.info(f"Marked {len(self._seen_ids)} existing posts as seen")

    async def _poll_loop(self):
        """Poll for new posts."""
        await asyncio.sleep(10)

        while self._running:
            try:
                await self._poll()
            except Exception as e:
                logger.error(f"Poll error: {e}")
            await asyncio.sleep(self.interval)

    async def _poll(self):
        """Poll for new posts using best available method."""
        statuses = await self._fetch_statuses()

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
                source="poll",
            )

            # Take screenshot
            post.screenshot_path = await self._take_screenshot(post_id)

            await self.on_post(post)

        if new_count > 0:
            logger.info(f"Poll: {new_count} new posts")

    async def _fetch_statuses(self) -> list:
        """Fetch statuses using best available method."""
        # Method 1: curl_cffi with cookies (fastest)
        if self._curl_session and self._account_id:
            loop = asyncio.get_event_loop()
            statuses = await loop.run_in_executor(None, self._fetch_statuses_curl)
            if statuses:
                return statuses

        # Method 2: Playwright page.evaluate
        if self._page and self._account_id:
            statuses = await self._fetch_statuses_playwright()
            if statuses:
                return statuses

        # Method 3: DOM scraping (last resort)
        return await self._scrape_statuses_from_dom()

    def _fetch_statuses_curl(self) -> list:
        """Fetch statuses using curl_cffi."""
        try:
            url = TRUTHSOCIAL_TIMELINE.format(account_id=self._account_id)
            resp = self._curl_session.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
            logger.warning(f"curl_cffi: statuses returned HTTP {resp.status_code}")
        except Exception as e:
            logger.error(f"curl_cffi statuses error: {e}")
        return []

    async def _fetch_statuses_playwright(self) -> list:
        """Fetch statuses using Playwright's page.evaluate."""
        try:
            url = TRUTHSOCIAL_TIMELINE.format(account_id=self._account_id)
            resp = await self._page.evaluate(
                """async (url) => {
                    try {
                        const r = await fetch(url, { credentials: 'include' });
                        if (!r.ok) return { error: 'HTTP ' + r.status };
                        return await r.json();
                    } catch(e) {
                        return { error: e.message };
                    }
                }""",
                url,
            )

            if isinstance(resp, dict) and resp.get("error"):
                logger.warning(f"Playwright statuses: {resp['error']}")
                return []

            if isinstance(resp, list):
                return resp

        except Exception as e:
            logger.error(f"Playwright statuses error: {e}")
        return []

    async def _scrape_statuses_from_dom(self) -> list:
        """Scrape statuses directly from the profile page DOM."""
        if not self._page:
            return []

        try:
            # Reload the page to get fresh content
            try:
                await self._page.reload(wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(3)
            except Exception:
                pass

            # Extract posts from DOM
            statuses = await self._page.evaluate("""
                () => {
                    const posts = [];
                    // Truth Social uses various selectors for posts
                    const articles = document.querySelectorAll('article, [data-testid="status"], .status-body');

                    for (const article of articles) {
                        try {
                            // Try to get post ID from link
                            const links = article.querySelectorAll('a[href*="/@realDonaldTrump/"]');
                            let postId = null;
                            let postUrl = null;

                            for (const link of links) {
                                const match = link.href.match(/\\/(\\d+)$/);
                                if (match) {
                                    postId = match[1];
                                    postUrl = link.href;
                                    break;
                                }
                            }

                            if (!postId) continue;

                            // Get content
                            const contentEl = article.querySelector('.status-content, [data-testid="status-content"], p');
                            const content = contentEl ? contentEl.textContent.trim() : '';

                            // Get timestamp
                            const timeEl = article.querySelector('time');
                            const createdAt = timeEl ? timeEl.getAttribute('datetime') || timeEl.textContent : '';

                            posts.push({
                                id: postId,
                                content: content,
                                created_at: createdAt,
                                url: postUrl || `https://truthsocial.com/@realDonaldTrump/${postId}`,
                                account: { acct: 'realDonaldTrump', username: 'realDonaldTrump' },
                                media_attachments: [],
                                sensitive: false,
                                spoiler_text: ''
                            });
                        } catch(e) {
                            // Skip this article
                        }
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
        """Keep the browser alive for WS interception."""
        while self._running:
            try:
                await asyncio.sleep(300)
                if self._page and self._running:
                    try:
                        await self._page.reload(wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(60)

    # ── Cookie refresh ───────────────────────────────────────────

    async def _cookie_refresh_loop(self):
        """Periodically refresh Cloudflare cookies."""
        while self._running:
            try:
                await asyncio.sleep(600)  # every 10 minutes
                if self._context and self._running:
                    cookies = await self._context.cookies()
                    self._cf_cookies = [
                        {"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c["path"]}
                        for c in cookies
                    ]
                    # Update curl_cffi session cookies
                    if self._curl_session:
                        self._curl_session.cookies.clear()
                        for cookie in self._cf_cookies:
                            self._curl_session.cookies.set(
                                cookie["name"], cookie["value"],
                                domain=cookie["domain"], path=cookie["path"],
                            )
                    logger.info(f"Cookies refreshed: {len(self._cf_cookies)} cookies")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cookie refresh error: {e}")
                await asyncio.sleep(60)

    # ── Screenshot ───────────────────────────────────────────────

    async def _take_screenshot(self, post_id: str) -> Optional[str]:
        """Take a screenshot of a post using Playwright."""
        if not self._page:
            return None

        try:
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
