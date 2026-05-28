"""Truthbrush-based poller for Truth Social API.

truthbrush is a dedicated Python library for Truth Social that handles
Cloudflare protection out of the box. It provides fast API access for polling.

This is our Fallback method — polls every 60 seconds.
"""
import asyncio
import logging
import re
from typing import Callable, Optional

from models import Post

logger = logging.getLogger(__name__)


class TruthbrushPoller:
    """Poll Truth Social using truthbrush library."""

    def __init__(
        self,
        username: str,
        on_post: Callable,
        interval: int = 60,
        truthsocial_username: Optional[str] = None,
        truthsocial_password: Optional[str] = None,
    ):
        self.username = username.lower()
        self.on_post = on_post
        self.interval = interval
        self._running = False
        self._api = None
        self._account_id = None
        self._truthsocial_username = truthsocial_username
        self._truthsocial_password = truthsocial_password
        self._seen_ids: set = set()
        self._initialized = False

    async def _init_api(self):
        """Initialize truthbrush API client."""
        try:
            from truthbrush import Api

            if self._truthsocial_username and self._truthsocial_password:
                self._api = Api(
                    username=self._truthsocial_username,
                    password=self._truthsocial_password,
                )
                logger.info("truthbrush: logged in with credentials")
            else:
                # Try without auth (public endpoints)
                self._api = Api()
                logger.info("truthbrush: using anonymous access")

            # Lookup account ID
            loop = asyncio.get_event_loop()
            account = await loop.run_in_executor(
                None, self._api.lookup, self.username
            )
            self._account_id = str(account.get("id", ""))
            logger.info(f"truthbrush: account @{self.username} -> ID {self._account_id}")
            self._initialized = True

        except ImportError:
            logger.error("truthbrush not installed! pip install truthbrush")
            self._initialized = False
        except Exception as e:
            logger.error(f"truthbrush init error: {e}")
            self._initialized = False

    async def start(self):
        """Start polling loop."""
        self._running = True

        await self._init_api()
        if not self._initialized:
            logger.warning("truthbrush: initialization failed, poller disabled")
            return

        # First run — mark existing posts as seen (don't send old posts)
        await self._initial_fetch()

        while self._running:
            try:
                await self._poll()
            except Exception as e:
                logger.error(f"truthbrush poll error: {e}")
            await asyncio.sleep(self.interval)

    async def _initial_fetch(self):
        """Fetch current posts and mark them as seen (no notifications)."""
        try:
            loop = asyncio.get_event_loop()
            statuses = await loop.run_in_executor(
                None,
                lambda: list(self._api.account_statuses(self._account_id, limit=20)),
            )
            for status in statuses:
                self._seen_ids.add(str(status.get("id", "")))
            logger.info(f"truthbrush: marked {len(self._seen_ids)} existing posts as seen")
        except Exception as e:
            logger.error(f"truthbrush initial fetch error: {e}")

    async def _poll(self):
        """Poll for new posts."""
        loop = asyncio.get_event_loop()
        statuses = await loop.run_in_executor(
            None,
            lambda: list(self._api.account_statuses(self._account_id, limit=5)),
        )

        new_count = 0
        for status in statuses:
            post_id = str(status.get("id", ""))
            if post_id in self._seen_ids:
                continue

            self._seen_ids.add(post_id)
            new_count += 1

            content = status.get("content", "")
            content = re.sub(r"<[^>]+>", "", content).strip()
            content = re.sub(r"&amp;", "&", content)
            content = re.sub(r"&lt;", "<", content)
            content = re.sub(r"&gt;", ">", content)
            content = re.sub(r"&quot;", '"', content)
            content = re.sub(r"&#39;", "'", content)

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
                source="truthbrush",
            )

            logger.info(f"truthbrush new post: {post_id}")
            await self.on_post(post)

        if new_count > 0:
            logger.info(f"truthbrush: {new_count} new posts")

    def stop(self):
        self._running = False
        logger.info("truthbrush poller stopped")
