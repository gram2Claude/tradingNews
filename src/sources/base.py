from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


@dataclass
class RawItem:
    """One news item discovered from a source."""

    url: str
    headline: str
    body: str | None
    published_at: datetime   # timezone-aware UTC


class Source(ABC):
    code: str

    def __init__(self, base_url: str, user_agent: str = "Mozilla/5.0 (compatible; trading-news/0.1)") -> None:
        self.base_url = base_url
        self.user_agent = user_agent

    @abstractmethod
    def fetch(self, since: datetime) -> Iterable[RawItem]:
        """Yield items published on/after `since` (UTC). Best-effort newest-first."""
        raise NotImplementedError
