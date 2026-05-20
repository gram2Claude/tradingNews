"""X5 press releases parser.

The site's official news sitemap (``/news-sitemap.xml``) is published by Rank
Math and lags reality by months on this host, so it is unreliable as a fresh
discovery channel. We use the paginated listing instead:

* ``/ru/press-center/press-releases/page/N/`` — N=1..MAX
* Each page contains ~12 ``<a href="https://www.x5.ru/ru/news/SLUG/">`` cards.

For each article URL we fetch the detail page and extract:

* ``headline``    — ``<meta property="og:title">`` (with ``- X5`` suffix stripped)
* ``published_at`` — ``<meta property="article:published_time">`` (ISO 8601 with
  timezone offset, normalized to UTC)
* ``body``        — text of the ``.content`` block (fallback: ``main``)

Listing pages are ordered newest-first; once we find an article with
``published_at < since`` we stop pagination.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Iterable, Iterator
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser

from src.sources.base import RawItem, Source

log = logging.getLogger(__name__)

LISTING_PATH = "/ru/press-center/press-releases/"
RU_NEWS_PREFIX_RE = re.compile(r"https://www\.x5\.ru/ru/news/[a-z0-9\-]+/")
MAX_PAGES = 50  # ~600 articles ceiling per fetch cycle
PAGE_DELAY_S = 0.4
ARTICLE_DELAY_S = 0.4


class X5IRSource(Source):
    code = "x5_ir"

    def __init__(self, base_url: str = "https://www.x5.ru/", **kw) -> None:
        super().__init__(base_url=base_url, **kw)
        self._host = _host(base_url)

    # ---------------------------------------------------------------- public

    def fetch(self, since: datetime) -> Iterable[RawItem]:
        since_utc = _to_utc(since)
        seen: set[str] = set()
        stop = False

        for page in range(1, MAX_PAGES + 1):
            page_url = self._page_url(page)
            try:
                html = self._http_get(page_url)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    log.info("x5_ir: pagination ended at page %d (404)", page)
                    break
                raise

            urls = list(extract_news_urls(html))
            if not urls:
                log.info("x5_ir: no news urls on page %d — stopping", page)
                break

            log.debug("x5_ir: page %d → %d urls", page, len(urls))
            for url in urls:
                if url in seen:
                    continue
                seen.add(url)
                try:
                    item = self._fetch_article(url)
                except Exception as exc:
                    log.warning("x5_ir: failed to parse %s: %s", url, exc)
                    continue

                if item.published_at < since_utc:
                    log.info(
                        "x5_ir: hit cutoff at %s (published %s < since %s)",
                        url, item.published_at.isoformat(), since_utc.isoformat(),
                    )
                    stop = True
                    break
                yield item
                time.sleep(ARTICLE_DELAY_S)

            if stop:
                break
            time.sleep(PAGE_DELAY_S)

    # ------------------------------------------------------------- internals

    def _page_url(self, n: int) -> str:
        if n == 1:
            return urljoin(self._host, LISTING_PATH)
        return urljoin(self._host, f"{LISTING_PATH}page/{n}/")

    def _http_get(self, url: str) -> str:
        with httpx.Client(headers={"User-Agent": self.user_agent}, timeout=20, follow_redirects=True) as c:
            r = c.get(url)
            r.raise_for_status()
            return r.text

    def _fetch_article(self, url: str) -> RawItem:
        html = self._http_get(url)
        return parse_article(url, html)


# --------------------------------------------------- module-level parsers
# These are stateless so tests can hit them with fixtures.


def extract_news_urls(html: str) -> Iterator[str]:
    """Yield unique ``/ru/news/...`` article URLs from a listing page in
    document order."""
    seen: set[str] = set()
    for m in RU_NEWS_PREFIX_RE.finditer(html):
        url = m.group(0)
        if url in seen:
            continue
        seen.add(url)
        yield url


def parse_article(url: str, html: str) -> RawItem:
    """Extract headline, body, published_at from one article page."""
    tree = HTMLParser(html)

    headline = _meta(html, "og:title") or ""
    if not headline:
        h1 = tree.css_first("h1")
        headline = h1.text(strip=True) if h1 else ""
    headline = _clean_headline(headline)
    if not headline:
        raise ValueError(f"no headline on {url}")

    pub_raw = _meta(html, "article:published_time")
    if not pub_raw:
        raise ValueError(f"no article:published_time meta on {url}")
    published_at = _parse_iso(pub_raw)

    body_node = tree.css_first(".content") or tree.css_first("main")
    body = body_node.text(separator="\n", strip=True) if body_node else None

    return RawItem(url=url, headline=headline, body=body, published_at=published_at)


# --------------------------------------------------------- small utilities


_TRAILING_SITE = re.compile(r"\s*-\s*X5\s*$")


def _clean_headline(s: str) -> str:
    return _TRAILING_SITE.sub("", s).strip()


def _meta(html: str, prop: str) -> str | None:
    pattern = re.compile(
        rf"<meta[^>]+(?:property|name)=[\"']{re.escape(prop)}[\"'][^>]+content=[\"']([^\"']+)[\"']",
        re.IGNORECASE,
    )
    m = pattern.search(html)
    return m.group(1) if m else None


def _parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _host(base_url: str) -> str:
    # Normalize to https://www.x5.ru regardless of investors path in config.
    from urllib.parse import urlsplit
    p = urlsplit(base_url)
    return f"{p.scheme}://{p.netloc}"
