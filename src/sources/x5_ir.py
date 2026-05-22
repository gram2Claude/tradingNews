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

Transient HTTP errors (5xx, connect/read timeouts) are retried up to three
times with exponential backoff via tenacity. 404s on pagination remain a
hard stop (used to detect the end of the listing).
"""

from __future__ import annotations

import html as html_module
import logging
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Iterable, Iterator
from urllib.parse import urljoin, urlsplit

import httpx
from selectolax.parser import HTMLParser
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.sources.base import RawItem, Source

log = logging.getLogger(__name__)

LISTING_PATH = "/ru/press-center/press-releases/"
RU_NEWS_PREFIX_RE = re.compile(r"https://www\.x5\.ru/ru/news/[a-z0-9\-]+/")
MAX_PAGES = 50  # ~600 articles ceiling per fetch cycle
PAGE_DELAY_S = 0.4
ARTICLE_DELAY_S = 0.4
HTTP_TIMEOUT_S = 20.0


def _is_transient_http(exc: BaseException) -> bool:
    """True iff the error looks transient and worth retrying.

    Retries apply to: connect errors, read/write/pool timeouts, generic
    protocol errors, and 5xx responses. 4xx (including 404) is NOT retried —
    the listing pagination uses 404 as a hard "end of pages" signal.
    """
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


class X5IRSource(Source):
    code = "x5_ir"

    # If x5.ru ever serves redirects, only follow them when the target stays
    # on the canonical host. This blocks an SSRF path where a compromised
    # origin (or a future mistake) could 302 us to 169.254.169.254 or
    # http://localhost:N. See security/01_*.md → §3.
    _ALLOWED_REDIRECT_HOSTS = frozenset({"www.x5.ru", "x5.ru"})
    _MAX_REDIRECTS = 3

    def __init__(self, base_url: str = "https://www.x5.ru/", **kw) -> None:
        super().__init__(base_url=base_url, **kw)
        self._host = _host(base_url)
        # Persistent client → reused TLS connection across all requests in
        # this fetch cycle. Saves ~200ms per article on TLS handshake.
        # follow_redirects=False so we can validate each Location header
        # before chasing it.
        self._client = httpx.Client(
            headers={"User-Agent": self.user_agent},
            timeout=HTTP_TIMEOUT_S,
            follow_redirects=False,
        )

    # ---------------------------------------------------------------- public

    def close(self) -> None:
        self._client.close()

    def fetch(self, since: datetime) -> Iterable[RawItem]:
        since_utc = _to_utc(since)
        seen: set[str] = set()
        stop = False
        last_page = 0

        for page in range(1, MAX_PAGES + 1):
            last_page = page
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

        if last_page >= MAX_PAGES and not stop:
            log.warning(
                "x5_ir: reached MAX_PAGES=%d without hitting since=%s — "
                "older news may have been skipped. Bump MAX_PAGES or run "
                "with a more recent start_date if backfilling.",
                MAX_PAGES, since_utc.isoformat(),
            )

    # ------------------------------------------------------------- internals

    def _page_url(self, n: int) -> str:
        if n == 1:
            return urljoin(self._host, LISTING_PATH)
        return urljoin(self._host, f"{LISTING_PATH}page/{n}/")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception(_is_transient_http),
        reraise=True,
    )
    def _http_get(self, url: str) -> str:
        # Manual redirect-following with host validation. httpx with
        # follow_redirects=True would chase any Location header — that's
        # the SSRF vector. We follow at most _MAX_REDIRECTS hops and bail
        # the moment a Location points outside the x5.ru host set.
        current = url
        for _ in range(self._MAX_REDIRECTS + 1):
            r = self._client.get(current)
            if r.is_redirect:
                loc = r.headers.get("location", "")
                if not loc:
                    r.raise_for_status()  # malformed redirect → error out
                target = httpx.URL(loc, base=current)
                if target.host not in self._ALLOWED_REDIRECT_HOSTS:
                    raise httpx.HTTPStatusError(
                        f"refused redirect to non-x5 host: {target.host}",
                        request=r.request, response=r,
                    )
                current = str(target)
                continue
            r.raise_for_status()
            return r.text
        raise httpx.HTTPStatusError(
            f"too many redirects (>{self._MAX_REDIRECTS}) starting from {url}",
            request=r.request, response=r,
        )

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

    headline = _meta(tree, "og:title") or ""
    if not headline:
        h1 = tree.css_first("h1")
        headline = h1.text(strip=True) if h1 else ""
    headline = _clean_headline(headline)
    if not headline:
        raise ValueError(f"no headline on {url}")

    pub_raw = _meta(tree, "article:published_time")
    if not pub_raw:
        raise ValueError(f"no article:published_time meta on {url}")
    published_at = _parse_iso(pub_raw)

    body_node = tree.css_first(".content") or tree.css_first("main")
    body = body_node.text(separator="\n", strip=True) if body_node else None
    body = _clean_text(body) if body else None

    return RawItem(url=url, headline=headline, body=body, published_at=published_at)


# --------------------------------------------------------- text cleaning
# Same _clean_text как в finam.py — нормализует NBSP, HTML-сущности и whitespace,
# чтобы body в БД был стабильным plain-text'ом для LLM.
_WS_LINE_RE = re.compile(r"[ \t ]+")
_WS_BLANK_RE = re.compile(r"\n{3,}")


def _clean_text(s: str) -> str:
    if not s:
        return ""
    s = html_module.unescape(s)
    s = s.replace(" ", " ").replace(" ", " ").replace(" ", " ")
    s = "".join(
        ch for ch in s if ch == "\n" or ch == "\t" or unicodedata.category(ch)[0] != "C"
    )
    lines = [_WS_LINE_RE.sub(" ", line).strip() for line in s.split("\n")]
    s = "\n".join(lines)
    s = _WS_BLANK_RE.sub("\n\n", s)
    return s.strip()


# --------------------------------------------------------- small utilities


_TRAILING_SITE = re.compile(r"\s*-\s*X5\s*$")


def _clean_headline(s: str) -> str:
    return _clean_text(_TRAILING_SITE.sub("", s))


def _meta(tree: HTMLParser, prop: str) -> str | None:
    """Robust meta-tag lookup: walks all <meta> elements and reads attributes
    independently. Survives any attribute order, unlike regex matching."""
    for node in tree.css("meta"):
        attrs = node.attributes
        if attrs.get("property") == prop or attrs.get("name") == prop:
            return attrs.get("content")
    return None


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
    p = urlsplit(base_url)
    return f"{p.scheme}://{p.netloc}"
