"""lmsic.com/analytics/ideas — трейдинговые рекомендации инвесткомпании ЛМС.

Recon: tests/fixtures/LMSIC_RECON.md
Spec: work_directory/01_specs/07_lmsic_ideas_spec.md
Plan: work_directory/02_plans/07_claude_lmsic_ideas_plan.md (v2)

Recommendation-only source — пишет в таблицу ``recommendations`` через
``item_destination = ItemDestination.RECOMMENDATIONS``. Архитектура диспатча
готова после task 06.

T1 — scaffold (done). T2 — listing parser + HTTP layer (this revision).
T3-T6 — extract fields, body cleaning, synthetic URL, fetch() integration.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from typing import Iterable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import Node
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.sources.base import FetchContext, ItemDestination, RawItem, Source

log = logging.getLogger(__name__)

LISTING_PATH = "/analytics/ideas/"
HTTP_TIMEOUT_S = 20.0
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _is_transient_http(exc: BaseException) -> bool:
    """Retry only network-level errors and 5xx. 4xx is terminal; 3xx is
    anomalous on a listing endpoint and surfaces via _http_get directly."""
    if isinstance(exc, (
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.ConnectTimeout,
        httpx.RemoteProtocolError,
    )):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


class LmsicSource(Source):
    code = "lmsic"
    item_destination = ItemDestination.RECOMMENDATIONS

    def __init__(
        self,
        base_url: str,
        user_agent: str = "Mozilla/5.0 (compatible; trading-news/0.1)",
        context: FetchContext | None = None,
    ) -> None:
        if context is None:
            raise ValueError(
                "LmsicSource requires FetchContext (need company aliases for issuer matching)"
            )
        super().__init__(base_url=base_url, user_agent=user_agent, context=context)
        # Persistent client — listing fetch is single GET, но client как контекст
        # consistent с x5_ir/finam (close в __exit__). follow_redirects=False —
        # listing не должен редиректить; редирект → аномалия, raise (SSRF-defense).
        self._client = httpx.Client(
            headers={"User-Agent": self.user_agent},
            timeout=HTTP_TIMEOUT_S,
            follow_redirects=False,
        )

    # ---------------------------------------------------------------- public

    def close(self) -> None:
        self._client.close()

    def fetch(self, since: datetime) -> Iterable[RawItem]:
        # T6 — full integration. Сейчас — placeholder pending T3-T5.
        log.info("lmsic fetch placeholder (T2): no items yielded")
        return iter([])

    # ------------------------------------------------------------- internals

    def _listing_url(self) -> str:
        return urljoin(self.base_url, LISTING_PATH)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception(_is_transient_http),
        reraise=True,
    )
    def _http_get(self, url: str) -> str:
        """GET с explicit status handling (plan v2, codex P2.5):

        - 200 → return body
        - 3xx → RuntimeError ("listing не должен редиректить — аномалия / SSRF guard")
        - 4xx → httpx.HTTPStatusError, terminal (tenacity не retry'ит)
        - 5xx → httpx.HTTPStatusError, tenacity retry'ит до 3 раз
        - network errors → tenacity retry
        """
        r = self._client.get(url)
        if r.is_redirect:
            loc = r.headers.get("location", "<missing>")
            raise RuntimeError(
                f"lmsic: unexpected redirect from {url} → {loc} "
                f"(listing endpoint must not redirect)"
            )
        # raise_for_status: 4xx и 5xx → HTTPStatusError; retry-фильтр пропустит только 5xx
        r.raise_for_status()
        return r.text

    def _fetch_listing(self) -> str:
        return self._http_get(self._listing_url())

    # ---- parsing primitives ------------------------------------------------

    @staticmethod
    def _published_at(date_str: str) -> datetime:
        """`DD.MM.YYYY` → UTC datetime at 23:59 Europe/Moscow (recon §12).

        Конец дня выбран чтобы lmsic-recs сортировались после finam-news того
        же дня в Obsidian-view.
        """
        d = datetime.strptime(date_str.strip(), "%d.%m.%Y").date()
        local = datetime.combine(d, time(23, 59), tzinfo=MOSCOW_TZ)
        return local.astimezone(timezone.utc)

    @staticmethod
    def _parse_card(node: Node) -> dict:
        """Извлекает структурные поля одного `<li class="ideas-page__list-item">`.

        Возвращает dict с date/issuer/body_html. body_html — raw innerHTML
        блока preview-text (нужен в T4 для `<br>` → newline preservation).
        Бросает ValueError если обязательное поле отсутствует.
        """
        date_node = node.css_first("span.ideas-card__date")
        if date_node is None:
            raise ValueError("lmsic card: span.ideas-card__date missing")
        title_node = node.css_first("div.ideas-card__title")
        if title_node is None:
            raise ValueError("lmsic card: div.ideas-card__title missing")
        body_node = node.css_first("div.ideas-card__preview-text")
        # body_html допускаем пустым — bare-bones card без preview-text
        # технически валиден; downstream `_extract_fields` отработает на ""
        body_html = (body_node.html or "") if body_node is not None else ""
        return {
            "date": date_node.text(strip=True),
            "issuer": title_node.text(strip=True),
            "body_html": body_html,
        }

    def _match_company(self, issuer: str) -> bool:
        """Case-insensitive substring match issuer строки против strong
        keywords (aliases + brands из persons table).

        Plan v2 codex P1.3: НЕ из ``CompanyCfg.brands`` (такого поля нет),
        а из ``FetchContext.load_keywords().strong``.

        Known limitation MVP: матчинг только по title. Если lmsic переименует
        X5-issuer на «КЦ ИКС 5» а title-aliases не покроют — пропустим idea.
        Future task — body-fallback.
        """
        if not issuer:
            return False
        assert self.context is not None  # guaranteed by __init__
        keywords = self.context.load_keywords().strong
        issuer_lc = issuer.lower()
        return any(kw and kw.lower() in issuer_lc for kw in keywords)
