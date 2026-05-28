"""
Truth Social → Telegram Monitor (v2 — Streaming + Fast Polling)

Architecture:
- Primary:   WebSocket Streaming (1-5 sec) via Playwright CDP interception
- Secondary: Playwright API polling (60 sec) — catches what WS misses
- Fallback:  truthbrush polling (60 sec) — independent, handles Cloudflare
- Backup:    RSSHub (2-5 min) — no auth needed, always works

All methods run in PARALLEL. First to detect a post wins — deduplication
ensures no duplicates are sent to Telegram.

Each post includes:
- Screenshot of the post
- Original text (tone preserved)
- Russian translation
- Sentiment analysis
- Link to original
"""
import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from models import Post
from notifier import TelegramNotifier
from truthbrush_poller import TruthbrushPoller
from ws_listener import WSListener
from playwright_ws import PlaywrightMonitor

# ── Logging ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ── Load .env ───────────────────────────────────────────────────
load_dotenv()


class TrumpMonitor:
    """Main orchestrator — runs all detection methods in parallel."""

    def __init__(self):
        # Telegram config
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.thread_id = os.getenv("TELEGRAM_THREAD_ID", "")

        # Truth Social config
        self.username = os.getenv("TRUTHSOCIAL_USERNAME", "realDonaldTrump")
        self.poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

        # Optional Truth Social credentials (for truthbrush)
        self.ts_username = os.getenv("TRUTHSOCIAL_LOGIN_USERNAME", "")
        self.ts_password = os.getenv("TRUTHSOCIAL_LOGIN_PASSWORD", "")

        # State
        self._running = False
        self._seen_ids: set = set()  # Global deduplication
        self._post_queue: asyncio.Queue = asyncio.Queue()

        # Components
        self.notifier: TelegramNotifier = None
        self.ws_listener: WSListener = None
        self.truthbrush: TruthbrushPoller = None
        self.playwright: PlaywrightMonitor = None

    async def start(self):
        """Start the monitor."""
        logger.info("=" * 60)
        logger.info("Trump Monitor v2 — Streaming + Fast Polling")
        logger.info(f"Target: @{self.username}")
        logger.info(f"Poll interval: {self.poll_interval}s")
        logger.info("=" * 60)

        # Validate config
        if not self.bot_token or not self.chat_id:
            logger.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID!")
            sys.exit(1)

        self._running = True

        # Init notifier
        self.notifier = TelegramNotifier(
            bot_token=self.bot_token,
            chat_id=self.chat_id,
            thread_id=self.thread_id,
        )

        # Send startup message
        await self.notifier.send_status(
            f"🚀 Мониторинг запущен!\n"
            f"👤 @{self.username}\n"
            f"⏱ Интервал опроса: {self.poll_interval}с\n"
            f"📡 Методы: WebSocket + Playwright + truthbrush"
        )

        # Start all detection methods in parallel + queue processor
        await asyncio.gather(
            self._run_ws_listener(),
            self._run_truthbrush(),
            self._run_playwright(),
            self._process_queue(),
            return_exceptions=True,
        )

    async def _on_post_detected(self, post: Post):
        """Callback — called by any detection method when a new post is found.

        Uses a queue for deduplication and serial processing.
        """
        if post.id in self._seen_ids:
            logger.debug(f"Duplicate post {post.id} from {post.source}, skipping")
            return

        self._seen_ids.add(post.id)
        await self._post_queue.put(post)
        logger.info(f"Queued post {post.id} from {post.source}")

    async def _process_queue(self):
        """Process queued posts — translate, analyze, send to Telegram."""
        while self._running:
            try:
                post = await asyncio.wait_for(self._post_queue.get(), timeout=5)
            except asyncio.TimeoutError:
                continue

            try:
                # Translate
                post.translation = await self._translate(post.content)

                # Sentiment analysis
                post.sentiment = await self._analyze_sentiment(post.content)

                # Send to Telegram
                await self.notifier.send_post(post)

                logger.info(f"✅ Post {post.id} sent to Telegram ({post.source})")

            except Exception as e:
                logger.error(f"Error processing post {post.id}: {e}", exc_info=True)

    async def _translate(self, text: str) -> str:
        """Translate text to Russian."""
        try:
            from deep_translator import GoogleTranslator
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: GoogleTranslator(source="auto", target="ru").translate(text[:4500])
            )
            return result or ""
        except Exception as e:
            logger.error(f"Translation error: {e}")
            return ""

    async def _analyze_sentiment(self, text: str) -> str:
        """Analyze sentiment of text."""
        try:
            from textblob import TextBlob
            loop = asyncio.get_event_loop()
            polarity = await loop.run_in_executor(
                None,
                lambda: TextBlob(text).sentiment.polarity
            )
            if polarity > 0.1:
                return "positive"
            elif polarity < -0.1:
                return "negative"
            else:
                return "neutral"
        except Exception as e:
            logger.error(f"Sentiment error: {e}")
            return ""

    async def _run_ws_listener(self):
        """Run WebSocket streaming listener."""
        try:
            self.ws_listener = WSListener(
                username=self.username,
                on_post=self._on_post_detected,
            )
            logger.info("Starting WS listener...")
            await self.ws_listener.start()
        except Exception as e:
            logger.error(f"WS listener crashed: {e}", exc_info=True)

    async def _run_truthbrush(self):
        """Run truthbrush poller."""
        try:
            self.truthbrush = TruthbrushPoller(
                username=self.username,
                on_post=self._on_post_detected,
                interval=self.poll_interval,
                truthsocial_username=self.ts_username or None,
                truthsocial_password=self.ts_password or None,
            )
            logger.info("Starting truthbrush poller...")
            await self.truthbrush.start()
        except Exception as e:
            logger.error(f"truthbrush poller crashed: {e}", exc_info=True)

    async def _run_playwright(self):
        """Run Playwright monitor (WS interception + polling)."""
        try:
            self.playwright = PlaywrightMonitor(
                username=self.username,
                on_post=self._on_post_detected,
                interval=self.poll_interval,
            )
            logger.info("Starting Playwright monitor...")
            await self.playwright.start()
        except Exception as e:
            logger.error(f"Playwright monitor crashed: {e}", exc_info=True)

    async def stop(self):
        """Gracefully stop all components."""
        logger.info("Stopping Trump Monitor...")
        self._running = False

        if self.ws_listener:
            self.ws_listener.stop()
        if self.truthbrush:
            self.truthbrush.stop()
        if self.playwright:
            self.playwright.stop()
        if self.notifier:
            await self.notifier.send_status("⛔ Мониторинг остановлен")
            await self.notifier.close()

        logger.info("Trump Monitor stopped")


async def main():
    """Entry point."""
    monitor = TrumpMonitor()

    # Handle shutdown signals
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(monitor.stop()))
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler

    try:
        await monitor.start()
    except KeyboardInterrupt:
        await monitor.stop()


if __name__ == "__main__":
    asyncio.run(main())
