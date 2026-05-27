"""finam.ru — публикации по тикеру через Playwright (servicepipe WAF).

Стратегия (см. tests/fixtures/FINAM_RECON.md + estimates/04_codex_finam_est.md):
* `/quote/moex/<ticker>/publications/` — listing с ~70 items, дата в URL
* Date filter из URL → не открываем article для item'ов вне since-window
* Slug relevance filter (codex P1.1) — режет broad-market noise (SpaceX, ETH, золото)
* Bulk URL dedup — один SQL на старте, потом O(1) set lookup
* Per-page goto через PlaywrightSource._goto (stealth + domcontentloaded + sleep)
* item_type classification — делает analyzer, не fetch
"""
from __future__ import annotations

import html as html_module
import logging
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Iterable, NamedTuple
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from selectolax.parser import HTMLParser

from src.sources.base import RawItem
from src.sources.playwright_base import (
    ARTICLE_SLEEP_S,
    PlaywrightSource,
    WARMUP_SLEEP_S,
    _BrowserDead,
    _ChallengeFailure,
)

log = logging.getLogger(__name__)

LISTING_PATH = "/quote/moex/{ticker}/publications/"

# Селектор для warmup verify — ссылка на любой publications/item/ article.
# На listing-странице компании их 60+ (всегда visible). На challenge-странице
# servicepipe их нет вовсе. Это надёжнее чем generic `h1` или container div'ы.
WARMUP_SELECTOR = 'a[href*="/publications/item/"]'

# URL pattern: /publications/item/<slug>-YYYYMMDD-HHMM/
# Слаг — Latin-transliterated, может содержать [a-z0-9-_].
_ITEM_URL_RE = re.compile(
    r'href="(/publications/item/[a-z0-9\-_]+-(\d{8})-(\d{4})/)"'
)

# Slug relevance filter (codex 04 P1.1).
# Finam listing содержит broad-market мусор (SpaceX, ETH, золото) — отсекаем.
# Slugs Latin-transliterated, word-boundary через "-".
_FINAM_RELEVANT_SLUG_PARTS = [
    # X5 direct (включая kh5 — Latin BSI/GOST транслитерация Х5; finam её использует)
    "x5", "kh5", "iks-5", "iks5", "korporativnyy-centr-iks",
    # Пятёрочка (Latin transliteration) + declensions
    "pyaterochka", "pyaterochki", "pyaterochke", "pyaterochku", "pyaterochkoy",
    # Перекрёсток (две транслитерации yo / е) + декл.
    "perekrestok", "perekrjostok", "perekrestka", "perekrjostka",
    "perekrestke", "perekrjostke", "perekrestku", "perekrjostku",
    # Чижик
    "chizhik", "chizhika", "chizhike", "chizhiku",
    # Russian retail competitors (word-bounded — "magnit" / "lenta" редко в составе других tokens)
    "magnit", "magnita", "lenta", "lenty", "okey", "o-key",
    "fix-price", "fixprice", "vkusvill",
    # X5 ownership / holdings (AFK Sistema → X5 recommendation case)
    "afk-sistema", "sistema", "sistemy",
    # Retail-sector general keywords
    "ritejl", "ritail", "riteyl", "prodovolstvenn", "fmcg",
]


class _ParseError(Exception):
    """Article HTML не отвечает ожидаемой структуре (селекторы не найдены)."""


class ListingHit(NamedTuple):
    url: str        # full URL with scheme/host
    slug: str       # часть пути перед -YYYYMMDD-HHMM/
    published_at: datetime  # parsed from URL — Moscow → UTC


# ---------------------------------------------------- module-level helpers


def _slug_relevant(slug: str) -> bool:
    """True если slug содержит хотя бы один из _FINAM_RELEVANT_SLUG_PARTS как
    hyphen-bounded токен. case-insensitive."""
    s = slug.lower()
    for part in _FINAM_RELEVANT_SLUG_PARTS:
        pattern = r"(?:^|-)" + re.escape(part) + r"(?:-|$)"
        if re.search(pattern, s):
            return True
    return False


def _parse_listing(html: str, base_url: str) -> Iterable[ListingHit]:
    """Извлечь все /publications/item/<slug>-YYYYMMDD-HHMM/ links из listing HTML."""
    seen: set[str] = set()
    for m in _ITEM_URL_RE.finditer(html):
        path, ymd, hm = m.group(1), m.group(2), m.group(3)
        if path in seen:
            continue
        seen.add(path)
        try:
            msk = datetime.strptime(f"{ymd}{hm}", "%Y%m%d%H%M")
        except ValueError:
            log.debug("finam: invalid date in URL %r", path)
            continue
        # Moscow time (UTC+3) → UTC
        published_at = (msk - timedelta(hours=3)).replace(tzinfo=timezone.utc)
        full_url = urljoin(base_url, path)
        # slug — часть между "/item/" и "-YYYYMMDD-HHMM/"
        slug = path.split("/item/", 1)[1].rsplit(f"-{ymd}-{hm}/", 1)[0]
        yield ListingHit(url=full_url, slug=slug, published_at=published_at)


def _parse_article(url: str, html: str, listing_meta: ListingHit) -> RawItem:
    """Парсинг finam article HTML.

    Source of truth для published_at — URL (FINAM_RECON §4: meta tag finam не ставит).
    Headline — `<meta property="og:title">` (надёжнее <h1> со whitespace).
    Body — контейнер с классом `finfin-local-plugin-publication-item-item`.
    """
    # Headline из og:title. og:title content is HTML-escaped (e.g. &quot;X5&quot;),
    # размуливаем через _clean_text — иначе сущности утекают в slug файла и H1.
    og_match = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
    if og_match:
        headline = _clean_text(og_match.group(1))
    else:
        h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
        if h1_match:
            headline = _clean_text(re.sub(r"<[^>]+>", "", h1_match.group(1)))
        else:
            raise _ParseError(f"no headline in {url}")

    # Body — container с finfin-local-plugin-publication-item-item классом.
    # _extract_body уже применяет _clean_text внутри.
    body = _extract_body(html)
    if not body or len(body) < 50:
        raise _ParseError(f"empty/short body in {url}")

    return RawItem(
        url=url,
        headline=headline,
        body=body,
        published_at=listing_meta.published_at,
    )


# Контейнер `finfin-local-plugin-publication-item-item` содержит direct-children
# div'ы как со статьей, так и с виджетами (ценовой ticker, "Купить на демосчёт",
# AI-инсайты, social footer, JS/CSS блоки). Whitelist по class'у — самый
# надёжный способ оставить только сам текст статьи.
#
# Эмпирически (см. tests/fixtures/finam_article_*.html, обе захвачены 2026-05-21):
#   - `bold font-xl`            — lead/аннотация (1-2 предложения)
#   - `mt2x p-margin font-xl`   — собственно текст статьи
# Остальные direct-children — мусор: ticker, кнопки, related, JS/CSS, footer.
_FINAM_CONTENT_CLASSES = (
    "bold font-xl",
    "p-margin",
)


def _extract_body(html: str) -> str:
    """Извлечь чистый текст статьи finam.

    Используем selectolax вместо regex — стрипает все script/style автоматически
    (decompose), и позволяет точечно выбирать direct-children контейнера по
    class-whitelist'у. До этой версии стрипали через regex и оставляли в body
    шапку, ценовой ticker, JS-код виджетов и social footer (~25 КБ мусора на
    статью при реальном тексте 2-3 КБ).
    """
    tree = HTMLParser(html)
    container = tree.css_first(
        '[class*="finfin-local-plugin-publication-item-item"]'
    )
    if container is None:
        return ""

    # Strip JS/CSS внутри контейнера: их содержимое после strip'а тегов выглядит
    # как обычный текст и засоряет body. decompose() удаляет node целиком.
    for node in container.css("script, style"):
        node.decompose()

    parts: list[str] = []
    for child in container.iter():
        cls = (child.attributes or {}).get("class") or ""
        if not any(marker in cls for marker in _FINAM_CONTENT_CLASSES):
            continue
        text = child.text(separator="\n", strip=True)
        if text:
            parts.append(text)

    return _clean_text("\n\n".join(parts))


# Совместный нормализатор whitespace / non-breaking space / HTML-сущностей.
# Применяется к body после извлечения — даёт стабильный текст для LLM и для
# хранения в БД (никаких `\xa0`, `&nbsp;`, `&quot;` и пр.).
_WS_LINE_RE = re.compile(r"[ \t ]+")
_WS_BLANK_RE = re.compile(r"\n{3,}")


def _clean_text(s: str) -> str:
    if not s:
        return ""
    s = html_module.unescape(s)
    # NBSP и прочие unicode-пробелы → обычный пробел
    s = s.replace(" ", " ").replace(" ", " ").replace(" ", " ")
    # Контрольные символы (кроме \n / \t) — убрать
    s = "".join(
        ch for ch in s if ch == "\n" or ch == "\t" or unicodedata.category(ch)[0] != "C"
    )
    # Сжать горизонтальные пробелы внутри строк
    lines = [_WS_LINE_RE.sub(" ", line).strip() for line in s.split("\n")]
    s = "\n".join(lines)
    # Сжать вертикальные blank-строки до максимум одной пустой
    s = _WS_BLANK_RE.sub("\n\n", s)
    return s.strip()


# ---------------------------------------------------------- FinamSource


class FinamSource(PlaywrightSource):
    code = "finam"

    def __init__(self, base_url, user_agent=None, context=None):
        if context is None:
            raise ValueError("FinamSource requires FetchContext")
        ticker_raw = context.company_cfg.finam_ticker
        if not ticker_raw:
            raise ValueError(
                f"company {context.company_cfg.name!r} has no finam_ticker "
                "configured (set companies[X].finam_ticker in config.yaml)"
            )
        ticker = str(ticker_raw).strip().lower()
        if not re.fullmatch(r"[a-z0-9]+", ticker):
            raise ValueError(
                f"finam_ticker must match [a-z0-9]+ (got {ticker_raw!r})"
            )
        super().__init__(base_url=base_url, user_agent=user_agent, context=context)
        self._ticker = ticker

    def fetch(self, since):
        # 1. Warmup на главной — нужен только для session cookies (servicepipe).
        # Verify тут НЕ делаем: на главной finam нет publication-селектора.
        self._goto(self.base_url, sleep_s=WARMUP_SLEEP_S)

        # 2. Bulk-load known URLs (codex 03 P1.4).
        # δ-completion (task 08): после per-item dispatch finam-recs живут в
        # таблице recommendations, не в news. Без UNION одни и те же URL'ы
        # переанализировались каждый cycle (LLM-tokens leak).
        with sqlite3.connect(self.context.db_path) as conn:
            known_urls = {
                row[0]
                for row in conn.execute(
                    "SELECT url FROM news WHERE source_id=? "
                    "UNION SELECT url FROM recommendations WHERE source_id=?",
                    (self.context.source_id, self.context.source_id),
                )
            }
        log.info("finam: %d known URLs pre-loaded for dedup", len(known_urls))

        # 3. Listing page — здесь делаем verify (WARMUP_SELECTOR ожидается видимым)
        listing_url = urljoin(
            self.base_url, LISTING_PATH.format(ticker=self._ticker)
        )
        listing_html = self._goto(listing_url, sleep_s=WARMUP_SLEEP_S)
        warmup = self._verify_warmup_success(WARMUP_SELECTOR)
        hits = list(_parse_listing(listing_html, base_url=self.base_url))

        stats = {
            "listing_hits": len(hits),
            "relevance_filtered": 0,
            "date_filtered": 0,
            "already_in_db": 0,
            "fetched": 0,
            "kept": 0,
            "timeout_errors": 0,
            "challenge_failures": 0,
            "browser_dead": 0,
            "parse_errors": 0,
        }
        results: list[RawItem] = []
        rejected_slugs: list[str] = []

        # 4. Per-article — фильтр сначала, goto только если все три passed
        for hit in hits:
            if not _slug_relevant(hit.slug):
                stats["relevance_filtered"] += 1
                rejected_slugs.append(hit.slug)
                continue
            if hit.published_at < since:
                stats["date_filtered"] += 1
                continue
            if hit.url in known_urls:
                stats["already_in_db"] += 1
                continue
            try:
                article_html = self._goto(hit.url, sleep_s=ARTICLE_SLEEP_S)
                item = _parse_article(hit.url, article_html, listing_meta=hit)
                results.append(item)
                stats["fetched"] += 1
                stats["kept"] += 1
            except _BrowserDead:
                stats["browser_dead"] += 1
                raise
            except _ChallengeFailure:
                stats["challenge_failures"] += 1
                raise
            except PlaywrightTimeoutError:
                stats["timeout_errors"] += 1
            except _ParseError as exc:
                stats["parse_errors"] += 1
                log.warning("finam: parse error on %s: %s", hit.url, exc)

        log.info("finam fetch summary: %s | warmup=%s", stats, warmup)
        if rejected_slugs:
            log.info(
                "finam relevance_filtered slugs (%d): %s",
                len(rejected_slugs),
                ", ".join(rejected_slugs),
            )
        return results
