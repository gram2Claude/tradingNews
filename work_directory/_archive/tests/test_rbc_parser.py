"""Offline tests for the RBC RSS parser.

Uses saved RSS XML fixture (tests/fixtures/rbc_rss_sample.xml) — no network.

Covers:
* RSS parsing edge cases (malformed items, missing fields, UTC conversion)
* Token-boundary keyword matching (latin \\b, cyrillic non-word wrapper)
* Strong/weak keyword split in FetchContext.load_keywords
* RBCSource.fetch end-to-end with a mocked _http_get
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src import db
from src.config import CompanyCfg, Config, SourceCfg
from src.sources.base import FetchContext, RawItem
from src.sources.rbc import (
    FeedParseError,
    RBCSource,
    _keyword_match,
    _parse_rss,
)

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def rss_xml() -> str:
    return (FIX / "rbc_rss_sample.xml").read_text(encoding="utf-8")


# ----------------------------- _parse_rss ----------------------------------


def test_parse_rss_extracts_30_items(rss_xml: str) -> None:
    items = [it for it in _parse_rss(rss_xml) if it is not None]
    assert len(items) == 30


def test_parse_rss_fields_are_populated(rss_xml: str) -> None:
    items = [it for it in _parse_rss(rss_xml) if it is not None]
    for it in items:
        assert it.url.startswith("https://www.rbc.ru/")
        assert it.headline and len(it.headline) > 5
        assert it.published_at.tzinfo == timezone.utc
        # body may be empty for very short news, but field exists as str | None
        assert it.body is None or isinstance(it.body, str)


def test_parse_rss_pubdate_converted_to_utc(rss_xml: str) -> None:
    """RBC pubDate is RFC822 with +0300 (Moscow); we normalize to UTC."""
    items = [it for it in _parse_rss(rss_xml) if it is not None]
    # All items in the sample dump are recent → year/month should be 2026-05
    assert items[0].published_at.year == 2026
    assert items[0].published_at.month == 5
    # Each item should be offset-aware UTC
    assert all(it.published_at.utcoffset() == timedelta(0) for it in items)


def test_parse_rss_prefers_fulltext_over_description(rss_xml: str) -> None:
    """When <rbc_news:full-text> is present, it should be the body, not <description>."""
    items = [it for it in _parse_rss(rss_xml) if it is not None]
    # Body texts are typically longer than the short anons/description.
    # Find at least one item where body length > 200 (i.e. clearly full-text).
    long_bodies = [it for it in items if (it.body or "") and len(it.body) > 200]
    assert len(long_bodies) >= 1, "expected at least one item with full-text body"


def test_parse_rss_skips_malformed_item() -> None:
    """Item without <link> yields None — caller bumps malformed counter."""
    xml = """<?xml version='1.0'?>
    <rss xmlns:rbc_news="https://www.rbc.ru" version="2.0"><channel>
      <item>
        <link>https://www.rbc.ru/ok</link>
        <title><![CDATA[OK item]]></title>
        <pubDate>Thu, 21 May 2026 08:00:00 +0300</pubDate>
        <rbc_news:full-text><![CDATA[body]]></rbc_news:full-text>
      </item>
      <item>
        <!-- no link -->
        <title><![CDATA[missing link]]></title>
        <pubDate>Thu, 21 May 2026 08:00:00 +0300</pubDate>
      </item>
      <item>
        <link>https://www.rbc.ru/bad-date</link>
        <title><![CDATA[OK title]]></title>
        <pubDate>not a real date</pubDate>
      </item>
    </channel></rss>"""
    items = list(_parse_rss(xml))
    # 3 items in → 1 RawItem + 2 None
    assert len(items) == 3
    assert items[0] is not None and items[0].url == "https://www.rbc.ru/ok"
    assert items[1] is None
    assert items[2] is None


def test_parse_rss_empty_body_raises_feed_error() -> None:
    """Empty body = source effectively unavailable. Must surface as error,
    not silently masquerade as 0 items. See review-02 Codex P2.2."""
    with pytest.raises(FeedParseError, match="empty"):
        list(_parse_rss(""))
    with pytest.raises(FeedParseError, match="empty"):
        list(_parse_rss("   \n   "))


def test_parse_rss_malformed_xml_raises_feed_error() -> None:
    """Garbage XML (truly broken — unclosed tag) raises with 'cannot parse'."""
    with pytest.raises(FeedParseError, match="cannot parse"):
        list(_parse_rss("<not valid xml"))


def test_parse_rss_no_channel_raises_feed_error() -> None:
    """Valid XML but no <channel> = wrong document.

    Two real-world cases:
    * RBC returns an HTML interstitial / error page with HTTP 200 — valid XML,
      no ``<channel>``.
    * Endpoint moved and returns a different document structure.

    Both must raise — silent empty fetch hides the source going dark.
    """
    with pytest.raises(FeedParseError, match="no <channel>"):
        list(_parse_rss("<rss version='2.0'></rss>"))
    with pytest.raises(FeedParseError, match="no <channel>"):
        list(_parse_rss("<html><body>just html</body></html>"))


def test_parse_rss_empty_channel_returns_no_items() -> None:
    """Channel exists, no items — valid (e.g. between-news lull). Return []."""
    assert list(_parse_rss("<rss><channel></channel></rss>")) == []


# ----------------------------- _keyword_match ------------------------------


def test_keyword_match_latin_word_boundary() -> None:
    assert _keyword_match("X5 announces results", ["X5"])
    # NOT a substring match: "X5" should not hit "OX5" or "X50"
    assert not _keyword_match("OX50 token surge", ["X5"])
    # Hyphen IS a word boundary, so "X5-rated" SHOULD match "X5".
    assert _keyword_match("the X5-rated film", ["X5"])
    # And comma is also a boundary.
    assert _keyword_match("Today X5, Inc reported", ["X5"])


def test_keyword_match_cyrillic_surname_with_declension() -> None:
    """Russian surnames must match across all grammatical cases via pymorphy3.

    Real news rarely uses bare nominative — ``Решение Шехтермана``, ``По словам
    Шехтерману``, ``Над Шехтерманом`` are typical. The filter must catch them
    all because that's what makes it useful for RBC's news flow.
    """
    # Nominative — baseline
    assert _keyword_match("Заявил Шехтерман", ["Шехтерман"])
    # Genitive (-а)
    assert _keyword_match("Решение Шехтермана одобрено советом", ["Шехтерман"])
    # Dative (-у)
    assert _keyword_match("Шехтерману поручили доклад", ["Шехтерман"])
    # Instrumental (-ом)
    assert _keyword_match("Документ подписан Шехтерманом", ["Шехтерман"])
    # Prepositional (-е)
    assert _keyword_match("В беседе с Шехтерманом — о планах", ["Шехтерман"])
    # NOT a match if surname is a substring of a derivative adjective
    # (different lemma, not a declension).
    assert not _keyword_match("шехтермановский подход", ["Шехтерман"])


def test_keyword_match_cyrillic_brand_with_declension() -> None:
    """Russian brand names decline too — ``Пятёрочки``, ``в Пятёрочке`` etc.
    Critical for RBC fetch yield: most business mentions use indirect cases."""
    # Nominative
    assert _keyword_match("Пятёрочка открыла магазин", ["Пятёрочка"])
    # Genitive (-и)
    assert _keyword_match("Выручка Пятёрочки выросла", ["Пятёрочка"])
    # Prepositional (-е)
    assert _keyword_match("В Пятёрочке снизили цены", ["Пятёрочка"])
    # Instrumental (-ой)
    assert _keyword_match("Пятёрочкой управляет директор", ["Пятёрочка"])
    # Different brand, different declension
    assert _keyword_match("В Перекрёстке продаётся", ["Перекрёсток"])


def test_keyword_match_brand_with_yo_letter() -> None:
    assert _keyword_match("Пятёрочка открывает магазины", ["Пятёрочка"])
    # Comma boundary
    assert _keyword_match("Перекрёсток, Пятёрочка и Чижик", ["Чижик"])


def test_keyword_match_case_insensitive() -> None:
    assert _keyword_match("ШЕХТЕРМАН высказался", ["Шехтерман"])
    assert _keyword_match("x5 retail group", ["X5"])


def test_keyword_match_rejects_no_overlap() -> None:
    assert not _keyword_match("Магнит и Лента отчитались", ["X5", "Пятёрочка"])


def test_keyword_match_empty_inputs() -> None:
    assert not _keyword_match("", ["X5"])
    assert not _keyword_match("X5 hi", [])
    assert not _keyword_match("X5 hi", [""])


# ----------------- FetchContext.load_keywords strong/weak split ------------


def test_load_keywords_splits_strong_and_weak(tmp_path: Path) -> None:
    """Aliases + brands → strong. Surnames → weak. No overlap."""
    db_path = tmp_path / "kw.sqlite"
    seed = tmp_path / "seed.csv"
    seed.write_text(
        "full_name,status,brand\n"
        "Игорь Шехтерман,CEO,X5 Retail Group\n"
        "Ольга Наумова,head,Пятёрочка\n"
        "Сергей Гончаров,head,Перекрёсток\n",
        encoding="utf-8",
    )
    cfg = Config(
        start_date="2026-05-01", llm_provider="openai", llm_model="gpt-5-mini",
        output_root=tmp_path / "out", db_path=db_path,
        auto_run=False, timezone="Europe/Moscow",
        companies=[CompanyCfg("X5", None, ["rbc"], str(seed),
                              aliases=["X5", "Х5", "X5 Retail Group"])],
        sources={"rbc": SourceCfg("rbc", "RBC", "https://rssexport.rbc.ru/", "rbc", True)},
    )
    db.init_db(cfg)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cid = conn.execute("SELECT id FROM companies WHERE name='X5'").fetchone()["id"]
    sid = conn.execute("SELECT id FROM sources WHERE code='rbc'").fetchone()["id"]
    conn.close()

    ctx = FetchContext(
        company_cfg=cfg.companies[0],
        company_id=cid,
        source_id=sid,
        db_path=db_path,
    )
    kw = ctx.load_keywords()
    # Strong = aliases + brands
    assert "X5" in kw.strong
    assert "Х5" in kw.strong
    assert "X5 Retail Group" in kw.strong
    assert "Пятёрочка" in kw.strong
    assert "Перекрёсток" in kw.strong
    # Weak = surnames
    assert "Шехтерман" in kw.weak
    assert "Наумова" in kw.weak
    assert "Гончаров" in kw.weak
    # No overlap between groups
    assert set(kw.strong).isdisjoint(set(kw.weak))


def test_load_keywords_falls_back_to_company_name(tmp_path: Path) -> None:
    """When aliases is empty, strong falls back to [company.name]."""
    db_path = tmp_path / "kw.sqlite"
    seed = tmp_path / "seed.csv"
    seed.write_text("full_name,status,brand\nX,Y,\n", encoding="utf-8")
    cfg = Config(
        start_date="2026-05-01", llm_provider="openai", llm_model="gpt-5-mini",
        output_root=tmp_path / "out", db_path=db_path,
        auto_run=False, timezone="Europe/Moscow",
        companies=[CompanyCfg("X5", None, ["rbc"], str(seed), aliases=[])],
        sources={"rbc": SourceCfg("rbc", "RBC", "https://x.x/", "rbc", True)},
    )
    db.init_db(cfg)
    conn = sqlite3.connect(db_path)
    cid = conn.execute("SELECT id FROM companies WHERE name='X5'").fetchone()[0]
    sid = conn.execute("SELECT id FROM sources WHERE code='rbc'").fetchone()[0]
    conn.close()
    ctx = FetchContext(cfg.companies[0], cid, sid, db_path)
    kw = ctx.load_keywords()
    assert kw.strong == ["X5"]


def test_load_keywords_is_cached(tmp_path: Path) -> None:
    """Second call returns the same Keywords object (no DB round-trip)."""
    db_path = tmp_path / "kw.sqlite"
    seed = tmp_path / "seed.csv"
    seed.write_text("full_name,status,brand\nX,Y,\n", encoding="utf-8")
    cfg = Config(
        start_date="2026-05-01", llm_provider="openai", llm_model="gpt-5-mini",
        output_root=tmp_path / "out", db_path=db_path,
        auto_run=False, timezone="Europe/Moscow",
        companies=[CompanyCfg("X5", None, ["rbc"], str(seed), aliases=["X5"])],
        sources={"rbc": SourceCfg("rbc", "RBC", "https://x.x/", "rbc", True)},
    )
    db.init_db(cfg)
    conn = sqlite3.connect(db_path)
    cid = conn.execute("SELECT id FROM companies WHERE name='X5'").fetchone()[0]
    sid = conn.execute("SELECT id FROM sources WHERE code='rbc'").fetchone()[0]
    conn.close()
    ctx = FetchContext(cfg.companies[0], cid, sid, db_path)
    first = ctx.load_keywords()
    second = ctx.load_keywords()
    assert first is second


# ----------------------- RBCSource.fetch (with mock) -----------------------


def _make_ctx(tmp_path: Path) -> tuple[FetchContext, Path]:
    """Build a minimal Config + DB + FetchContext for fetch() tests."""
    db_path = tmp_path / "f.sqlite"
    seed = tmp_path / "seed.csv"
    seed.write_text(
        "full_name,status,brand\n"
        "Игорь Шехтерман,CEO,X5 Retail Group\n",
        encoding="utf-8",
    )
    cfg = Config(
        start_date="2026-05-01", llm_provider="openai", llm_model="gpt-5-mini",
        output_root=tmp_path / "out", db_path=db_path,
        auto_run=False, timezone="Europe/Moscow",
        companies=[CompanyCfg("X5", None, ["rbc"], str(seed),
                              aliases=["X5", "Х5", "Пятёрочка"])],
        sources={"rbc": SourceCfg("rbc", "RBC", "https://rssexport.rbc.ru/", "rbc", True)},
    )
    db.init_db(cfg)
    conn = sqlite3.connect(db_path)
    cid = conn.execute("SELECT id FROM companies WHERE name='X5'").fetchone()[0]
    sid = conn.execute("SELECT id FROM sources WHERE code='rbc'").fetchone()[0]
    conn.close()
    return FetchContext(cfg.companies[0], cid, sid, db_path), db_path


def test_rbc_fetch_raises_without_context() -> None:
    with pytest.raises(ValueError, match="FetchContext"):
        RBCSource(base_url="https://x.x/", context=None)


def test_rbc_fetch_keeps_strong_match(rss_xml: str, tmp_path: Path) -> None:
    """An RSS dump fed through fetch returns items where strong keyword matches."""
    ctx, _ = _make_ctx(tmp_path)
    response = SimpleNamespace(text=rss_xml, status_code=200)
    with RBCSource(base_url="https://rssexport.rbc.ru/", context=ctx) as src:
        with patch.object(src, "_http_get", return_value=response):
            items = list(src.fetch(since=datetime(2020, 1, 1, tzinfo=timezone.utc)))
    # We can't predict the dump content (kept count depends on real news at
    # recon time), but the shape of the result must be a list of RawItems.
    assert isinstance(items, list)
    for it in items:
        assert isinstance(it, RawItem)
        # Each kept item must contain at least one strong keyword somewhere
        kw = ctx.load_keywords()
        haystack = f"{it.headline}\n{it.body or ''}"
        assert _keyword_match(haystack, kw.strong), \
            f"kept item lacks strong match: {it.headline[:80]}"


def test_rbc_fetch_filters_by_since(tmp_path: Path) -> None:
    """Items older than `since` are dropped before keyword filter runs."""
    ctx, _ = _make_ctx(tmp_path)
    # Synthetic XML with two items: one old (2020), one recent (2026).
    xml = """<?xml version='1.0'?>
    <rss xmlns:rbc_news="https://www.rbc.ru" version="2.0"><channel>
      <item>
        <link>https://www.rbc.ru/old</link>
        <title>X5 announces something old</title>
        <pubDate>Mon, 01 Jan 2020 12:00:00 +0300</pubDate>
        <rbc_news:full-text>X5 X5 X5</rbc_news:full-text>
      </item>
      <item>
        <link>https://www.rbc.ru/new</link>
        <title>X5 announces something new</title>
        <pubDate>Thu, 21 May 2026 12:00:00 +0300</pubDate>
        <rbc_news:full-text>X5 X5 X5</rbc_news:full-text>
      </item>
    </channel></rss>"""
    response = SimpleNamespace(text=xml, status_code=200)
    with RBCSource(base_url="https://rssexport.rbc.ru/", context=ctx) as src:
        with patch.object(src, "_http_get", return_value=response):
            items = list(src.fetch(since=datetime(2025, 1, 1, tzinfo=timezone.utc)))
    assert len(items) == 1
    assert items[0].url == "https://www.rbc.ru/new"


def test_rbc_fetch_weak_only_is_rejected(tmp_path: Path) -> None:
    """Item with surname but no aliases/brands → rejected, weak_only counter increments."""
    ctx, _ = _make_ctx(tmp_path)
    xml = """<?xml version='1.0'?>
    <rss xmlns:rbc_news="https://www.rbc.ru" version="2.0"><channel>
      <item>
        <link>https://www.rbc.ru/weak</link>
        <title>Губернатор Шехтерман прокомментировал</title>
        <pubDate>Thu, 21 May 2026 12:00:00 +0300</pubDate>
        <rbc_news:full-text>News about a person named Шехтерман in another context.</rbc_news:full-text>
      </item>
      <item>
        <link>https://www.rbc.ru/none</link>
        <title>Совсем не про нас</title>
        <pubDate>Thu, 21 May 2026 12:00:00 +0300</pubDate>
        <rbc_news:full-text>Совсем не наш текст про погоду в Сибири.</rbc_news:full-text>
      </item>
    </channel></rss>"""
    response = SimpleNamespace(text=xml, status_code=200)
    with RBCSource(base_url="https://rssexport.rbc.ru/", context=ctx) as src:
        with patch.object(src, "_http_get", return_value=response):
            items = list(src.fetch(since=datetime(2025, 1, 1, tzinfo=timezone.utc)))
    # Surname-only ("Шехтерман") must be rejected — no strong term in text
    assert items == []


def test_rbc_fetch_strong_passes_even_with_unusual_position(tmp_path: Path) -> None:
    """Brand at the very start or with surrounding punctuation still passes."""
    ctx, _ = _make_ctx(tmp_path)
    xml = """<?xml version='1.0'?>
    <rss xmlns:rbc_news="https://www.rbc.ru" version="2.0"><channel>
      <item>
        <link>https://www.rbc.ru/a</link>
        <title>Пятёрочка открыла магазин в Уфе</title>
        <pubDate>Thu, 21 May 2026 12:00:00 +0300</pubDate>
        <rbc_news:full-text>Открытие нового магазина прошло успешно.</rbc_news:full-text>
      </item>
      <item>
        <link>https://www.rbc.ru/b</link>
        <title>Открытие нового магазина</title>
        <pubDate>Thu, 21 May 2026 12:00:00 +0300</pubDate>
        <rbc_news:full-text>В тексте упомянута "Пятёрочка" с кавычками вокруг.</rbc_news:full-text>
      </item>
    </channel></rss>"""
    response = SimpleNamespace(text=xml, status_code=200)
    with RBCSource(base_url="https://rssexport.rbc.ru/", context=ctx) as src:
        with patch.object(src, "_http_get", return_value=response):
            items = list(src.fetch(since=datetime(2025, 1, 1, tzinfo=timezone.utc)))
    urls = {it.url for it in items}
    assert urls == {"https://www.rbc.ru/a", "https://www.rbc.ru/b"}


def test_rbc_fetch_warns_when_strong_keywords_empty(tmp_path: Path, caplog) -> None:
    """Empty strong group → fetch logs warning and returns no items.

    Happens when CompanyCfg.aliases is empty AND no brands in seed_persons:
    fallback to ``[company.name]`` saves us, but if that's also empty (only
    surnames in seed), strong is empty and weak alone never passes.
    """
    import logging
    db_path = tmp_path / "ek.sqlite"
    seed = tmp_path / "seed.csv"
    # Persons with only surnames, no brands → weak only, no strong.
    seed.write_text(
        "full_name,status,brand\n"
        "Игорь Шехтерман,CEO,\n",
        encoding="utf-8",
    )
    cfg = Config(
        start_date="2026-05-01", llm_provider="openai", llm_model="gpt-5-mini",
        output_root=tmp_path / "out", db_path=db_path,
        auto_run=False, timezone="Europe/Moscow",
        # aliases empty AND company name is empty string → strong = [""]
        # which dedups to []. This is the pathological case the warning exists for.
        companies=[CompanyCfg("", None, ["rbc"], str(seed), aliases=[])],
        sources={"rbc": SourceCfg("rbc", "RBC", "https://x.x/", "rbc", True)},
    )
    db.init_db(cfg)
    conn = sqlite3.connect(db_path)
    cid = conn.execute("SELECT id FROM companies WHERE name=''").fetchone()[0]
    sid = conn.execute("SELECT id FROM sources WHERE code='rbc'").fetchone()[0]
    conn.close()
    ctx = FetchContext(cfg.companies[0], cid, sid, db_path)

    xml = """<?xml version='1.0'?>
    <rss xmlns:rbc_news="https://www.rbc.ru" version="2.0"><channel>
      <item>
        <link>https://www.rbc.ru/a</link>
        <title>Любая новость</title>
        <pubDate>Thu, 21 May 2026 12:00:00 +0300</pubDate>
        <rbc_news:full-text>Любой текст про что угодно.</rbc_news:full-text>
      </item>
    </channel></rss>"""
    response = SimpleNamespace(text=xml, status_code=200)
    with caplog.at_level(logging.WARNING, logger="src.sources.rbc"):
        with RBCSource(base_url="https://x.x/", context=ctx) as src:
            with patch.object(src, "_http_get", return_value=response):
                items = list(src.fetch(since=datetime(2025, 1, 1, tzinfo=timezone.utc)))
    assert items == []
    assert any("no strong keywords" in r.message for r in caplog.records)


def test_rbc_fetch_retry_on_transient(tmp_path: Path) -> None:
    """Transient HTTP 503 → tenacity retries, second call succeeds."""
    ctx, _ = _make_ctx(tmp_path)
    xml = """<?xml version='1.0'?>
    <rss xmlns:rbc_news="https://www.rbc.ru" version="2.0"><channel>
      <item>
        <link>https://www.rbc.ru/x</link>
        <title>X5 объявила</title>
        <pubDate>Thu, 21 May 2026 12:00:00 +0300</pubDate>
        <rbc_news:full-text>X5 details</rbc_news:full-text>
      </item>
    </channel></rss>"""
    ok_resp = SimpleNamespace(text=xml, status_code=200, raise_for_status=lambda: None)
    transient_resp = MagicMock()
    transient_resp.status_code = 503

    with RBCSource(base_url="https://rssexport.rbc.ru/", context=ctx) as src:
        client = MagicMock()
        # First call: 503, second: 200
        client.get.side_effect = [transient_resp, ok_resp]
        src._client = client
        with patch("src.sources.rbc.wait_exponential") as wait:
            # Tenacity uses this to compute sleep — make it instant.
            wait.return_value = lambda rs: 0
            items = list(src.fetch(since=datetime(2025, 1, 1, tzinfo=timezone.utc)))
    assert len(items) == 1
    assert items[0].url == "https://www.rbc.ru/x"
    assert client.get.call_count == 2
