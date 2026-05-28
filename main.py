"""
Truth Social → Telegram Monitor

Simple: Playwright loads page → scrapes posts → sends to Telegram.
No unnecessary messages. Only notifies when new posts are found.
"""
import asyncio
import logging
import os
import signal
import sys

from dotenv import load_dotenv

from models import Post
from notifier import TelegramNotifier
from playwright_ws import PlaywrightMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

load_dotenv()


class TrumpMonitor:
    def __init__(self):
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.thread_id = os.getenv("TELEGRAM_THREAD_ID", "")
        self.username = os.getenv("TRUTHSOCIAL_USERNAME", "realDonaldTrump")
        self.poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

        self._running = False
        self._seen_ids: set = set()
        self._post_queue: asyncio.Queue = asyncio.Queue()
        self.notifier: TelegramNotifier = None
        self.playwright: PlaywrightMonitor = None

    async def start(self):
        if not self.bot_token or not self.chat_id:
            logger.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID!")
            sys.exit(1)

        self._running = True
        self.notifier = TelegramNotifier(
            bot_token=self.bot_token,
            chat_id=self.chat_id,
            thread_id=self.thread_id,
        )

        logger.info(f"Starting monitor for @{self.username} (poll: {self.poll_interval}s)")

        # Run Playwright monitor + queue processor
        await asyncio.gather(
            self._run_playwright(),
            self._process_queue(),
            return_exceptions=True,
        )

    async def _on_post_detected(self, post: Post):
        if post.id in self._seen_ids:
            return
        self._seen_ids.add(post.id)
        await self._post_queue.put(post)
        logger.info(f"New post: {post.id}")

    async def _process_queue(self):
        while self._running:
            try:
                post = await asyncio.wait_for(self._post_queue.get(), timeout=5)
            except asyncio.TimeoutError:
                continue

            try:
                post.translation = await self._translate(post.content)
                post.sentiment = await self._analyze_sentiment(post.content)
                await self.notifier.send_post(post)
                logger.info(f"Sent to Telegram: {post.id}")
            except Exception as e:
                logger.error(f"Error sending post {post.id}: {e}")

    async def _translate(self, text: str) -> str:
        try:
            from deep_translator import GoogleTranslator
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: GoogleTranslator(source="auto", target="ru").translate(text[:4500])
            )
            return result or ""
        except Exception as e:
            logger.error(f"Translation error: {e}")
            return ""

    async def _analyze_sentiment(self, text: str) -> str:
        try:
            from textblob import TextBlob
            loop = asyncio.get_event_loop()
            polarity = await loop.run_in_executor(None, lambda: TextBlob(text).sentiment.polarity)
            if polarity > 0.1:
                return "positive"
            elif polarity < -0.1:
                return "negative"
            return "neutral"
        except Exception as e:
            logger.error(f"Sentiment error: {e}")
            return ""

    async def _run_playwright(self):
        retry_delay = 60  # Start with 60s to avoid Cloudflare rate limiting
        while self._running:
            try:
                self.playwright = PlaywrightMonitor(
                    username=self.username,
                    on_post=self._on_post_detected,
                    interval=self.poll_interval,
                )
                await self.playwright.start()
                if self._running:
                    logger.info(f"Retrying in {retry_delay}s...")
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 600)  # Max 10 min
            except Exception as e:
                logger.error(f"Error: {e}")
                if self._running:
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 600)

    async def stop(self):
        self._running = False
        if self.playwright:
            self.playwright.stop()
        if self.notifier:
            await self.notifier.close()


async def main():
    monitor = TrumpMonitor()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(monitor.stop()))
        except NotImplementedError:
            pass

    try:
        await monitor.start()
    except KeyboardInterrupt:
        await monitor.stop()


if __name__ == "__main__":
    asyncio.run(main())
