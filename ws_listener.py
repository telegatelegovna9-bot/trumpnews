"""WebSocket Streaming API listener for Truth Social (Mastodon-compatible).

Connects to wss://truthsocial.com/api/v1/streaming and listens for new posts
from a specific account. This provides near real-time detection (1-5 seconds).

Truth Social is built on Mastodon, so it supports the standard streaming API.
The public:local stream shows all public posts — we filter by username.

NOTE: The streaming API requires an OAuth access token. Without authentication
the server returns HTTP 403. This listener will disable itself after the first
403 to avoid spamming the server with reconnection attempts.
"""
import asyncio
import json
import logging
import re
from typing import Callable, Optional

import websockets

from models import Post

logger = logging.getLogger(__name__)

TRUTHSOCIAL_WS = "wss://truthsocial.com/api/v1/streaming"


class WSListener:
    """WebSocket streaming listener for Truth Social."""

    def __init__(
        self,
        username: str,
        on_post: Callable,
        access_token: Optional[str] = None,
    ):
        self.username = username.lower()
        self.on_post = on_post
        self.access_token = access_token
        self._running = False
        self._disabled = False  # Set to True if 403 received

    async def start(self):
        """Start listening on the WebSocket stream."""
        self._running = True

        if not self.access_token:
            logger.warning("WS: no access token — streaming API requires auth. Disabling WS listener.")
            self._disabled = True
            return

        reconnect_delay = 5
        while self._running and not self._disabled:
            try:
                await self._listen()
            except Exception as e:
                err_str = str(e)
                # 403 means auth required — stop retrying
                if "403" in err_str or "401" in err_str:
                    logger.warning(f"WS: got {err_str} — streaming requires auth. Disabling WS listener.")
                    self._disabled = True
                    return

                logger.warning(f"WS error: {e}. Reconnecting in {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 1.5, 60)

    async def _listen(self):
        """Connect and listen for messages."""
        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        stream_url = f"{TRUTHSOCIAL_WS}?stream=public:local"
        logger.info(f"WS connecting to {stream_url}")

        async with websockets.connect(
            stream_url,
            additional_headers=headers,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            logger.info("WS connected to Truth Social streaming")

            async for message in ws:
                if not self._running:
                    break

                try:
                    data = json.loads(message)
                    await self._handle_message(data)
                except json.JSONDecodeError:
                    logger.debug(f"WS non-JSON message: {message[:100]}")
                except Exception as e:
                    logger.error(f"WS message handling error: {e}")

    async def _handle_message(self, data: dict):
        """Handle incoming WebSocket message."""
        event = data.get("event", "")
        payload_str = data.get("payload", "{}")

        if event != "update":
            return

        try:
            payload = json.loads(payload_str)
        except (json.JSONDecodeError, TypeError):
            return

        account = payload.get("account", {})
        acct = account.get("acct", "").lower()
        username = account.get("username", "").lower()

        if acct != self.username and username != self.username:
            return

        post_id = str(payload.get("id", ""))
        if not post_id:
            return

        content = payload.get("content", "")
        content = re.sub(r"<[^>]+>", "", content).strip()
        content = re.sub(r"&amp;", "&", content)
        content = re.sub(r"&lt;", "<", content)
        content = re.sub(r"&gt;", ">", content)
        content = re.sub(r"&quot;", '"', content)
        content = re.sub(r"&#39;", "'", content)

        created_at = payload.get("created_at", "")
        url = payload.get("url", f"https://truthsocial.com/@{self.username}/{post_id}")
        sensitive = payload.get("sensitive", False)
        spoiler_text = payload.get("spoiler_text", "")

        media_urls = []
        for media in payload.get("media_attachments", []):
            media_url = media.get("url") or media.get("preview_url")
            if media_url:
                media_urls.append(media_url)

        post = Post(
            id=post_id,
            username=self.username,
            content=content,
            created_at=created_at,
            url=url,
            sensitive=sensitive,
            spoiler_text=spoiler_text,
            media_urls=media_urls,
            source="websocket",
        )

        logger.info(f"WS new post from @{self.username}: {post_id}")
        await self.on_post(post)

    def stop(self):
        self._running = False
        logger.info("WS listener stopped")
