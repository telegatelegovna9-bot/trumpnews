"""Truth Social monitor — Playwright-based with Cloudflare bypass.

Uses Playwright with Chrome's new headless mode (--headless=new) which is
less detectable by Cloudflare. Scrapes posts directly from the page DOM.

Strategy:
1. Launch Chrome with --headless=new + stealth patches
2. Navigate to profile page
3. Wait for Cloudflare challenge to resolve (up to 60s)
4. Scrape posts from DOM
5. Take screenshots
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
    """Monitor Truth Social using Playwright with DOM scraping."""

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
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

        os.makedirs(screenshot_dir, exist_ok=True)

    async def start(self):
        """Start monitoring."""
        self._running = True

        if not await self._init_browser():
            return

        # Mark existing posts as seen
        await self._initial_scrape()

        # Main polling loop
        await self._poll_loop()

    async def _init_browser(self) -> bool:
        """Initialize Playwright browser with maximum stealth."""
        try:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()

            # Use --headless=new (Chrome's new headless mode, less detectable)
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
                    "--start-maximized",
                ],
            )

            self._context = await self._browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="America/New_York",
            )

            self._page = await self._context.new_page()

            # Apply stealth
            await self._apply_stealth(self._page)
            logger.info("Playwright: browser initialized with stealth")

            # Navigate to profile
            url = TRUTHSOCIAL_URL.format(username=self.username)
            logger.info(f"Playwright: navigating to {url}")

            try:
                await self._page.goto(url, wait_until="commit", timeout=30000)
            except Exception as e:
                logger.warning(f"Playwright: navigation commit error: {e}")

            # Wait for Cloudflare challenge
            await self._wait_for_cloudflare()

            # Check final state
            try:
                title = await self._page.title()
                url_now = self._page.url
                logger.info(f"Playwright: final title = {title}")
                logger.info(f"Playwright: final URL = {url_now}")
            except Exception:
                pass

            return True

        except Exception as e:
            logger.error(f"Playwright init error: {e}", exc_info=True)
            return False

    async def _wait_for_cloudflare(self):
        """Wait for Cloudflare challenge to resolve."""
        logger.info("Playwright: waiting for Cloudflare challenge...")

        for i in range(12):  # Up to 60 seconds
            await asyncio.sleep(5)

            try:
                title = await self._page.title()
                content = await self._page.content()

                # Check if we're past Cloudflare
                if "Just a moment" not in title and "Cloudflare" not in title:
                    logger.info(f"Playwright: Cloudflare passed! Title: {title}")
                    return

                # Check if there's a Turnstile checkbox to click
                turnstile = await self._page.query_selector(
                    'input[type="checkbox"], .cf-turnstile, #challenge-stage, iframe[src*="challenges.cloudflare.com"]'
                )
                if turnstile:
                    logger.info("Playwright: found Turnstile/challenge element, attempting click...")
                    try:
                        await turnstile.click()
                        await asyncio.sleep(3)
                    except Exception:
                        pass

                logger.info(f"Playwright: still on Cloudflare... ({(i+1)*5}s)")

            except Exception:
                pass

        logger.warning("Playwright: Cloudflare challenge did not resolve in 60s")

    async def _apply_stealth(self, page):
        """Apply stealth JavaScript."""
        await page.add_init_script("""
            // Core stealth
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            delete navigator.__proto__.webdriver;

            // Plugins
            Object.defineProperty(navigator, 'plugins', {
                get: () => {
                    const p = [
                        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
                    ];
                    p.length = 3;
                    return p;
                },
            });

            // Languages & platform
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });

            // Chrome object
            window.chrome = {
                runtime: { OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' }, OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' }, PlatformArch: { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' }, PlatformNaclArch: { ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' }, PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' }, RequestUpdateCheckStatus: { NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' } },
                loadTimes: function() { return { commitLoadTime: Date.now() / 1000, connectionInfo: 'h2', finishDocumentLoadTime: Date.now() / 1000, finishLoadTime: Date.now() / 1000, firstPaintAfterLoadTime: 0, firstPaintTime: Date.now() / 1000, navigationType: 'Other', npnNegotiatedProtocol: 'h2', requestTime: Date.now() / 1000 - 0.5, startLoadTime: Date.now() / 1000 - 0.3, wasAlternateProtocolAvailable: false, wasFetchedViaSpdy: true, wasNpnNegotiated: true }; },
                csi: function() { return { onloadT: Date.now(), pageT: Date.now() - performance.timing.navigationStart, startE: performance.timing.navigationStart, tran: 15 }; },
                app: { isInstalled: false, InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }, RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } },
            };

            // Permissions
            const origQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (params) =>
                params.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : origQuery(params);

            // WebGL
            const getParam = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(p) {
                if (p === 37445) return 'Intel Inc.';
                if (p === 37446) return 'Intel Iris OpenGL Engine';
                return getParam.apply(this, arguments);
            };

            // Hardware
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
            Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });

            // Screen
            Object.defineProperty(screen, 'availWidth', { get: () => 1920 });
            Object.defineProperty(screen, 'availHeight', { get: () => 1040 });
            Object.defineProperty(screen, 'width', { get: () => 1920 });
            Object.defineProperty(screen, 'height', { get: () => 1080 });
            Object.defineProperty(screen, 'colorDepth', { get: () => 24 });

            // Connection
            Object.defineProperty(navigator, 'connection', {
                get: () => ({ downlink: 10, effectiveType: '4g', rtt: 50, saveData: false }),
            });
        """)

    async def _initial_scrape(self):
        """Scrape page and mark existing posts as seen."""
        statuses = await self._scrape_posts()
        for s in statuses:
            self._seen_ids.add(str(s.get("id", "")))
        logger.info(f"Marked {len(self._seen_ids)} existing posts as seen")

    async def _poll_loop(self):
        """Main polling loop — scrape page every interval."""
        await asyncio.sleep(5)

        while self._running:
            try:
                # Reload page to get fresh content
                try:
                    await self._page.reload(wait_until="domcontentloaded", timeout=20000)
                    await asyncio.sleep(3)
                except Exception as e:
                    logger.warning(f"Page reload error: {e}")
                    # Try navigating again
                    try:
                        url = TRUTHSOCIAL_URL.format(username=self.username)
                        await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        await asyncio.sleep(3)
                    except Exception:
                        pass

                statuses = await self._scrape_posts()

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
                        media_urls=status.get("media_urls", []),
                        source="playwright_dom",
                    )

                    # Take screenshot
                    post.screenshot_path = await self._take_screenshot(post_id)
                    await self.on_post(post)

                if new_count > 0:
                    logger.info(f"Poll: {new_count} new posts")
                else:
                    logger.debug(f"Poll: no new posts ({len(statuses)} total)")

            except Exception as e:
                logger.error(f"Poll error: {e}", exc_info=True)

            await asyncio.sleep(self.interval)

    async def _scrape_posts(self) -> list:
        """Scrape posts from the current page DOM."""
        try:
            statuses = await self._page.evaluate("""
                () => {
                    const posts = [];

                    // Try multiple selectors for Truth Social posts
                    const selectors = [
                        'article',
                        '[data-testid="status"]',
                        '.status-body',
                        'div[class*="status"]',
                        'div[class*="post"]',
                    ];

                    let articles = [];
                    for (const sel of selectors) {
                        const found = document.querySelectorAll(sel);
                        if (found.length > 0) {
                            articles = found;
                            break;
                        }
                    }

                    // If no articles found, try to find any links to posts
                    if (articles.length === 0) {
                        const allLinks = document.querySelectorAll('a[href*="/@"]');
                        const seen = new Set();
                        for (const link of allLinks) {
                            const href = link.href || '';
                            const match = href.match(/@(\\w+)\\/(\\d+)$/);
                            if (match && !seen.has(match[2])) {
                                seen.add(match[2]);
                                const parent = link.closest('div') || link.parentElement;
                                posts.push({
                                    id: match[2],
                                    content: parent ? parent.textContent.trim().substring(0, 500) : '',
                                    created_at: '',
                                    url: href,
                                    account: { acct: match[1] },
                                    media_urls: [],
                                });
                            }
                        }
                        return posts;
                    }

                    for (const article of articles) {
                        try {
                            // Find post link
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

                            // Get text
                            const contentEl = article.querySelector(
                                'p, [data-testid="status-content"], .status-content, div[class*="content"]'
                            );
                            const content = contentEl
                                ? contentEl.textContent.trim()
                                : article.textContent.trim().substring(0, 500);

                            // Get time
                            const timeEl = article.querySelector('time');
                            const createdAt = timeEl
                                ? (timeEl.getAttribute('datetime') || timeEl.textContent)
                                : '';

                            // Get media
                            const mediaUrls = [];
                            const imgs = article.querySelectorAll('img[src*="media"], video source');
                            for (const img of imgs) {
                                const src = img.src || img.getAttribute('src');
                                if (src && !src.includes('avatar') && !src.includes('header')) {
                                    mediaUrls.push(src);
                                }
                            }

                            posts.push({
                                id: postId,
                                content: content,
                                created_at: createdAt,
                                url: postUrl,
                                account: { acct: 'realdonaldtrump' },
                                media_urls: mediaUrls,
                            });
                        } catch(e) {}
                    }

                    return posts;
                }
            """)

            if statuses:
                logger.info(f"DOM: scraped {len(statuses)} posts")
            return statuses or []

        except Exception as e:
            logger.error(f"DOM scraping error: {e}")
            return []

    async def _take_screenshot(self, post_id: str) -> Optional[str]:
        """Take a screenshot of a post."""
        if not self._page:
            return None

        try:
            post_url = f"https://truthsocial.com/@{self.username}/{post_id}"

            # Try to navigate to the post
            try:
                await self._page.goto(post_url, wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(2)
            except Exception:
                pass

            # Try to find and screenshot just the post element
            post_el = await self._page.query_selector("article, .status-body, [data-testid='post']")

            filepath = os.path.join(
                self.screenshot_dir,
                f"post_{post_id}_{int(datetime.now().timestamp())}.png",
            )

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
        logger.info("Monitor stopped")
