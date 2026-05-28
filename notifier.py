"""Telegram notifier — sends post notifications with screenshot, translation, and sentiment."""
import asyncio
import logging
import os
from typing import Optional

import aiohttp

from models import Post

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Send post notifications to Telegram."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        thread_id: Optional[str] = None,
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.api_base = f"https://api.telegram.org/bot{bot_token}"
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send_post(self, post: Post):
        """Send a post notification to Telegram.

        Sends screenshot with caption containing:
        - Original text (tone preserved)
        - Russian translation
        - Sentiment analysis
        - Link to original
        """
        try:
            caption = self._build_caption(post)

            if post.screenshot_path and os.path.exists(post.screenshot_path):
                await self._send_photo(post.screenshot_path, caption)
            else:
                await self._send_message(caption)

            logger.info(f"Sent to Telegram: post {post.id} via {post.source}")

        except Exception as e:
            logger.error(f"Telegram send error: {e}", exc_info=True)

    def _build_caption(self, post: Post) -> str:
        """Build Telegram message caption."""
        parts = []

        # Header
        source_emoji = {
            "websocket": "⚡",
            "playwright_ws": "⚡",
            "truthbrush": "🔄",
            "playwright_poll": "🔄",
            "rss": "📡",
        }.get(post.source, "📌")

        parts.append(f"{source_emoji} <b>Пост из Truth Social</b>")
        parts.append(f"👤 @{post.username}")

        if post.created_at:
            parts.append(f"📅 {post.created_at[:19].replace('T', ' ')}")

        # Sentiment
        if post.sentiment:
            sentiment_emoji = {
                "positive": "🟢 Позитивный",
                "negative": "🔴 Негативный",
                "neutral": "⚪ Нейтральный",
                "mixed": "🟡 Смешанный",
            }.get(post.sentiment, post.sentiment)
            parts.append(f"🎭 Тон: {sentiment_emoji}")

        parts.append("")

        # Spoiler warning
        if post.spoiler_text:
            parts.append(f"⚠️ <b>{post.spoiler_text}</b>")
            parts.append("")

        # Original text (preserving tone)
        parts.append("📝 <b>Оригинал:</b>")
        # Truncate if too long for Telegram caption (1024 char limit for photo captions)
        original = post.content
        if len(original) > 800:
            original = original[:800] + "..."
        parts.append(original)

        # Translation
        if post.translation:
            parts.append("")
            parts.append("🇷🇺 <b>Перевод:</b>")
            translation = post.translation
            if len(translation) > 800:
                translation = translation[:800] + "..."
            parts.append(translation)

        # Media note
        if post.media_urls:
            parts.append(f"\n📎 Медиа: {len(post.media_urls)} файл(ов)")

        # Link
        if post.url:
            parts.append(f"\n🔗 <a href=\"{post.url}\">Открыть оригинал</a>")

        # Source info
        parts.append(f"\n<i>Обнаружен через: {post.source}</i>")

        return "\n".join(parts)

    async def _send_photo(self, photo_path: str, caption: str):
        """Send photo with caption."""
        session = await self._get_session()

        url = f"{self.api_base}/sendPhoto"

        data = aiohttp.FormData()
        data.add_field("chat_id", self.chat_id)
        data.add_field("caption", caption)
        data.add_field("parse_mode", "HTML")
        if self.thread_id:
            data.add_field("message_thread_id", self.thread_id)

        with open(photo_path, "rb") as f:
            data.add_field("photo", f, filename=os.path.basename(photo_path))

            async with session.post(url, data=data) as resp:
                result = await resp.json()
                if not result.get("ok"):
                    logger.error(f"Telegram sendPhoto error: {result}")
                    # Fallback to text message
                    await self._send_message(caption)
                else:
                    logger.debug("Photo sent to Telegram")

    async def _send_message(self, text: str):
        """Send text message."""
        session = await self._get_session()

        url = f"{self.api_base}/sendMessage"

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        if self.thread_id:
            payload["message_thread_id"] = self.thread_id

        async with session.post(url, json=payload) as resp:
            result = await resp.json()
            if not result.get("ok"):
                logger.error(f"Telegram sendMessage error: {result}")

    async def send_status(self, text: str):
        """Send a status/heartbeat message."""
        await self._send_message(f"🤖 <b>Trump Monitor</b>\n\n{text}")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
