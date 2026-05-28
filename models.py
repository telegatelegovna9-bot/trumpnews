"""Data model for a Truth Social post."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Post:
    id: str
    username: str
    content: str
    created_at: str = ""
    url: str = ""
    sensitive: bool = False
    spoiler_text: str = ""
    translation: str = ""
    sentiment: str = ""
    screenshot_path: Optional[str] = None
    media_urls: list = field(default_factory=list)
    source: str = ""  # which method discovered this post

    def short(self) -> str:
        return f"Post(id={self.id}, @{self.username}, via={self.source}, text={self.content[:60]}...)"
