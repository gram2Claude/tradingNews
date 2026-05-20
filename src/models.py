from dataclasses import dataclass
from datetime import datetime


@dataclass
class Company:
    id: int | None
    name: str
    start_date: str | None


@dataclass
class Source:
    id: int | None
    code: str
    name: str
    base_url: str
    enabled: bool


@dataclass
class Person:
    id: int | None
    company_id: int
    full_name: str
    status: str | None
    brand: str | None
    from_seed: bool


@dataclass
class NewsItem:
    id: int | None
    company_id: int
    source_id: int
    url: str
    headline: str
    body: str | None
    published_at: datetime
    mood: str | None = None
    mood_reason: str | None = None
    status: str = "new"
    error_msg: str | None = None
    retry_count: int = 0
    tokens_used: int | None = None
