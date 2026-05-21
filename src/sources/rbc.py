"""RBC news source. RSS-only.

The main rbc.ru site is gated by Qrator JS-challenge (HTTP 401 + /__qrator/qauth.js
on every request — see tests/fixtures/RBC_RECON.md), so an httpx-based parser of
the HTML site is not feasible without a headless browser. The RSS endpoint
``https://rssexport.rbc.ru/rbcnews/news/30/full.rss`` is served from a separate
subdomain that is NOT behind Qrator and contains the full article text in the
``<rbc_news:full-text>`` element — no per-article HTTP needed.

Hard limit: only ``/30/full.rss`` works (other counts return 404 or 302 to /30).
30 items cover ~7 hours; backfill of older news is impossible via RSS. For a
trading-news tool the freshness matters more than historical reach, so this is
acceptable. Backfill via Playwright or web.archive.org is a separate spec if
ever needed.

Filter strategy:
* RSS returns 30 latest items across all topics (politics, business, sport, ...).
* We keep only items where ``headline + full-text`` substring-matches any of the
  company's keywords (aliases ∪ brands ∪ surnames from seed), using token
  boundaries so ``X5`` does not match ``OX5`` and ``Шехтерман`` matches all
  case-forms (the surname stem is shared across declensions).
* ``since`` filter is applied as a safety net; in practice all 30 items are
  newer than any reasonable ``since``.

Transient HTTP errors (5xx, 429, connect/read timeouts) are retried up to three
times with exponential backoff via tenacity. The RSS endpoint is a single GET
per fetch cycle, so total fetch wall-time stays well under 30 seconds even in
the worst case.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from typing import Iterable, Iterator

import defusedxml.ElementTree as ET   # XXE / billion-laughs safe — see reviews/02
import httpx
import pymorphy3
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.sources.base import FetchContext, RawItem, Source

log = logging.getLogger(__name__)

RSS_URL = "https://rssexport.rbc.ru/rbcnews/news/30/full.rss"
RBC_NS = "{https://www.rbc.ru}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT_S = 30


class _TransientHTTPError(Exception):
    """HTTP status worth retrying (rate limit / 5xx)."""


class FeedParseError(Exception):
    """Whole-feed parse failure (broken XML, HTML interstitial, no <channel>).

    Distinct from malformed *individual* items (those are skipped silently and
    counted in stats['malformed']). A feed-level failure means the source is
    effectively unavailable for this cycle — surface it as an error so the
    pipeline doesn't treat it as a successful empty fetch. See review-02 Codex P2.2.
    """


def _is_transient_exc(exc: BaseException) -> bool:
    # httpx.TimeoutException covers ConnectTimeout / ReadTimeout / WriteTimeout
    # / PoolTimeout in one. NetworkError covers ConnectError / ReadError / WriteError.
    # We previously listed only ConnectError + ReadTimeout, missing ConnectTimeout
    # (which fired in T6.4 throttled-RBC tests). See review-02 Codex P2.1.
    return isinstance(
        exc,
        (_TransientHTTPError, httpx.TimeoutException, httpx.NetworkError),
    )


class RBCSource(Source):
    code = "rbc"

    def __init__(
        self,
        base_url: str,
        user_agent: str = USER_AGENT,
        context: FetchContext | None = None,
    ) -> None:
        if context is None:
            raise ValueError("RBCSource requires FetchContext (for keyword filter)")
        super().__init__(base_url=base_url, user_agent=user_agent, context=context)
        self._client: httpx.Client | None = None

    def __enter__(self) -> "RBCSource":
        # rssexport.rbc.ru is a single endpoint; no redirects expected.
        # follow_redirects=False keeps SSRF surface zero — see RBC_RECON.md §3.
        self._client = httpx.Client(
            timeout=TIMEOUT_S,
            follow_redirects=False,
            headers={"User-Agent": self.user_agent, "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8"},
        )
        return self

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def fetch(self, since: datetime) -> Iterable[RawItem]:
        assert self.context is not None  # checked in __init__
        keywords = self.context.load_keywords()
        if not keywords.strong:
            log.warning(
                "rbc: no strong keywords (aliases/brands) for company %s — "
                "fetch will return 0 items (weak surnames alone do not pass)",
                self.context.company_cfg.name,
            )

        response = self._http_get(RSS_URL)
        items = list(_parse_rss(response.text))

        stats = {"fetched": len(items), "older_than_since": 0,
                 "keyword_rejects": 0, "weak_only_rejects": 0,
                 "kept": 0, "malformed": 0}
        kept: list[RawItem] = []
        for item in items:
            if item is None:
                stats["malformed"] += 1
                continue
            if item.published_at < since:
                stats["older_than_since"] += 1
                continue
            haystack = f"{item.headline}\n{item.body or ''}"
            if _keyword_match(haystack, keywords.strong):
                kept.append(item)
                stats["kept"] += 1
                continue
            # No strong hit. Check weak for diagnostics, but reject either way.
            if _keyword_match(haystack, keywords.weak):
                stats["weak_only_rejects"] += 1
                log.debug(
                    "rbc: rejected weak-only match in headline=%r",
                    item.headline[:80],
                )
            else:
                stats["keyword_rejects"] += 1

        log.info("rbc fetch summary: %s", stats)
        return kept

    @retry(
        retry=retry_if_exception(_is_transient_exc),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    )
    def _http_get(self, url: str) -> httpx.Response:
        assert self._client is not None
        log.debug("rbc: GET %s", url)
        response = self._client.get(url)
        if response.status_code in {429, 500, 502, 503, 504}:
            raise _TransientHTTPError(f"transient HTTP {response.status_code}")
        response.raise_for_status()
        return response


# --------------------------- module-level pure helpers --------------------------
# Kept at module level so tests can hit them with fixtures, no network needed.


def _parse_rss(xml_text: str) -> Iterator[RawItem | None]:
    """Yield RawItem per <item>; yields None on a malformed *item* (caller counts).

    Feed-level failures (unparseable XML, missing <channel>, empty body) raise
    :class:`FeedParseError` so the fetcher surfaces them as errors instead of
    pretending the source returned zero news. See review-02 Codex P2.2.

    Schema: see tests/fixtures/RBC_RECON.md §2 for the verified field layout.
    """
    if not xml_text or not xml_text.strip():
        raise FeedParseError("empty RSS body")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FeedParseError(f"cannot parse RSS XML: {exc}") from exc
    channel = root.find("channel")
    if channel is None:
        raise FeedParseError("RSS has no <channel> — likely an interstitial / HTML error page")
    for item_el in channel.findall("item"):
        link = (item_el.findtext("link") or "").strip()
        title = (item_el.findtext("title") or "").strip()
        pub_date = (item_el.findtext("pubDate") or "").strip()
        if not (link and title and pub_date):
            log.debug("rbc: skipping malformed item link=%r title=%r pubDate=%r",
                      link[:60], title[:60], pub_date[:60])
            yield None
            continue
        try:
            published_at = parsedate_to_datetime(pub_date).astimezone(timezone.utc)
        except (TypeError, ValueError) as exc:
            log.warning("rbc: cannot parse pubDate %r: %s", pub_date, exc)
            yield None
            continue
        body = (item_el.findtext(f"{RBC_NS}full-text") or "").strip()
        if not body:
            # short_news without full-text → use <description> as fallback
            body = (item_el.findtext("description") or "").strip()
        yield RawItem(
            url=link,
            headline=title,
            body=body or None,
            published_at=published_at,
        )


# Cyrillic-aware token boundary for the regex fallback path (used for Latin /
# multi-word keywords where lemmatization doesn't apply).
_CYRILLIC = r"а-яёА-ЯЁ"
_BOUNDARY = r"(?:^|(?<=[^\w" + _CYRILLIC + r"]))"
_BOUNDARY_END = r"(?=$|[^\w" + _CYRILLIC + r"])"

# Tokenizer for the lemmatization path: extracts contiguous Cyrillic / Latin
# letter runs. Punctuation and digits act as separators. Mirrors the regex used
# in src/name_matcher.py to keep behaviour consistent across the project.
_WORD_RE = re.compile(r"[А-Яа-яЁёA-Za-z]+")

# Below this length we treat tokens as noise (initials, conjunctions, articles).
_MIN_TOKEN_LEN = 3


@lru_cache(maxsize=1)
def _morph() -> pymorphy3.MorphAnalyzer:
    return pymorphy3.MorphAnalyzer()


@lru_cache(maxsize=8192)
def _lemma(word: str) -> str:
    """Lowercased normal form of a Russian word, via pymorphy3.

    Cached because RSS vocabulary repeats heavily across items; the cache
    bounds memory and pays for itself within the first article of a cycle.
    """
    return _morph().parse(word)[0].normal_form.lower()


def _is_pure_ascii_word(s: str) -> bool:
    return all(c.isascii() and (c.isalnum() or c == "_") for c in s)


def _is_cyrillic_single_word(s: str) -> bool:
    """True for ``Пятёрочка``, ``Шехтерман``. False for ``X5``, ``X5 Retail Group``."""
    if not s or " " in s:
        return False
    return all(c.isalpha() and not c.isascii() for c in s)


def _keyword_match(text: str, keywords: list[str]) -> bool:
    """Case-insensitive token match, declension-aware for Russian words.

    Strategy (per review-02 #1, variant A — pymorphy3 lemmatization):

    * **Cyrillic single-word keywords** (``Пятёрочка``, ``Шехтерман``):
      tokenize the text, lemmatize each token, exact-match against
      lemmatized keywords. Catches all declensions (``Пятёрочки``,
      ``Пятёрочке``, ``Шехтермана``, ``Шехтерману``...).
    * **Latin tokens** (``X5``, ``Group``): use ``\\b`` word boundary. ``X5``
      does not match ``OX50``; ``Group`` does not match ``Groups``.
    * **Multi-word / mixed keywords** (``X5 Retail Group``): explicit token
      boundary on both sides of the literal string.

    Latin and multi-word keywords skip lemmatization because pymorphy3 is
    tuned for Russian and produces garbage normal forms for ASCII tokens.
    """
    if not text or not keywords:
        return False

    # Split keywords by which path they take. Cleaning + dedup happens here
    # so the hot loop below never sees empties.
    cyr_lemmas: set[str] = set()
    others: list[str] = []
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        if _is_cyrillic_single_word(kw):
            cyr_lemmas.add(_lemma(kw))
        else:
            others.append(kw)

    # 1. Lemma match for cyrillic single-word keywords.
    if cyr_lemmas:
        for tok in _WORD_RE.findall(text):
            if len(tok) < _MIN_TOKEN_LEN:
                continue
            if _lemma(tok) in cyr_lemmas:
                return True

    # 2. Regex token-boundary match for Latin / multi-word keywords.
    for kw in others:
        if _is_pure_ascii_word(kw):
            pattern = r"\b" + re.escape(kw) + r"\b"
        else:
            pattern = _BOUNDARY + re.escape(kw) + _BOUNDARY_END
        if re.search(pattern, text, re.IGNORECASE):
            return True

    return False
