from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterable

from src.config import CompanyCfg


class ItemDestination(Enum):
    """Куда fetcher диспатчит RawItem'ы конкретного source'а.

    NEWS — таблица `news` (x5_ir, finam — default).
    RECOMMENDATIONS — таблица `recommendations` (lmsic и будущие recommendation-only).

    γ-стратегия (см. spec 06): finam остаётся в NEWS+item_type, новая таблица
    наполняется только source'ами которые знают что они recommendation-only.
    """

    NEWS = "news"
    RECOMMENDATIONS = "recommendations"


@dataclass
class RawItem:
    """One item discovered from a source.

    Базовые поля заполняют все source'ы. Опциональные структурные поля
    (target_price, recommendation_action, potential_pct, multipliers_json)
    заполняют только recommendation-source'ы (lmsic — задача 07); для news
    они остаются None. Контракт фиксирован в задаче 06 чтобы избежать
    переделок RawItem на каждом новом source'е.
    """

    url: str
    headline: str
    body: str | None
    published_at: datetime   # timezone-aware UTC
    target_price: float | None = None
    recommendation_action: str | None = None  # 'buy' | 'hold' | 'sell' | None
    potential_pct: float | None = None
    multipliers_json: str | None = None


@dataclass
class Keywords:
    """Two-tier keyword set for substring filters.

    ``strong``: aliases + brands. Self-sufficient — one strong match is enough
    to accept an article. These are unique enough to the company that a hit
    has near-zero false-positive risk (``Пятёрочка``, ``X5 Retail Group``).

    ``weak``: surnames. Russian surnames are short and frequently homonymous
    (``Гусев``, ``Хан``, ``Козлов``) — a bare surname match against a news
    article will catch the wrong person more often than the right one. Weak
    alone is not a pass condition; we surface them for diagnostics and for
    downstream name_matcher in the analyzer stage, but do not let weak alone
    decide fetch acceptance.

    See variant B in `_archive/03_estimates/02_codex_rbc_news_est.md` discussion.
    """

    strong: list[str] = field(default_factory=list)
    weak: list[str] = field(default_factory=list)


@dataclass
class FetchContext:
    """Per-(company, source) context for fetch.

    Sources that need company-specific data (aliases, brands, surnames,
    last_published_at, ...) consume this. Sources that don't (e.g. x5_ir
    which scrapes a single company-owned site) ignore it.
    """

    company_cfg: CompanyCfg
    company_id: int
    source_id: int
    db_path: Path
    _keywords_cache: Keywords | None = field(default=None, repr=False, compare=False)

    def load_keywords(self) -> Keywords:
        """Return ``Keywords(strong=aliases+brands, weak=surnames)``. Cached.

        Aliases come from ``CompanyCfg.aliases`` (config.yaml). Brands and
        surnames come from the ``persons`` table loaded at ``init-db`` from
        the ``seed_persons`` CSV.
        """
        if self._keywords_cache is not None:
            return self._keywords_cache

        aliases: list[str] = list(self.company_cfg.aliases) or [self.company_cfg.name]
        brands: list[str] = []
        surnames: list[str] = []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            for row in conn.execute(
                "SELECT DISTINCT brand FROM persons "
                "WHERE company_id = ? AND brand IS NOT NULL AND brand != ''",
                (self.company_id,),
            ):
                brands.append(row["brand"])
            for row in conn.execute(
                "SELECT full_name FROM persons WHERE company_id = ?",
                (self.company_id,),
            ):
                parts = (row["full_name"] or "").strip().rsplit(maxsplit=1)
                if parts:
                    surnames.append(parts[-1])

        strong = _dedup_preserve_order(aliases + brands)
        # Weak excludes anything that's already a strong term — e.g. if some
        # brand happens to coincide with a surname, keep it as strong only.
        weak = _dedup_preserve_order(
            s for s in surnames if s and s not in set(strong)
        )
        self._keywords_cache = Keywords(strong=strong, weak=weak)
        return self._keywords_cache


def _dedup_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        k = (x or "").strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


class Source(ABC):
    code: str
    # Куда fetcher пишет RawItem'ы этого source'а. Default — NEWS (backward-compat
    # для x5_ir / finam). recommendation-only source'ы переопределяют
    # в class-level (`item_destination = ItemDestination.RECOMMENDATIONS`).
    item_destination: ItemDestination = ItemDestination.NEWS

    def __init__(
        self,
        base_url: str,
        user_agent: str = "Mozilla/5.0 (compatible; trading-news/0.1)",
        context: FetchContext | None = None,
    ) -> None:
        self.base_url = base_url
        self.user_agent = user_agent
        self.context = context

    @abstractmethod
    def fetch(self, since: datetime) -> Iterable[RawItem]:
        """Yield items published on/after `since` (UTC). Best-effort newest-first."""
        raise NotImplementedError

    def close(self) -> None:
        """Release any persistent resources (e.g. HTTP client). Subclasses override."""

    def __enter__(self) -> "Source":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
