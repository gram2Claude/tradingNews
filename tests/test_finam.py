"""Offline tests для FinamSource. Используем сохранённые HTML фикстуры из T8.2."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import db
from src.config import CompanyCfg, Config, SourceCfg
from src.sources.base import FetchContext
from src.sources.finam import (
    FinamSource,
    ListingHit,
    _ParseError,
    _parse_article,
    _parse_listing,
    _slug_relevant,
)

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def listing_html() -> str:
    return (FIX / "finam_x5_publications.html").read_text(encoding="utf-8")


@pytest.fixture
def article_news_html() -> str:
    return (FIX / "finam_article_news.html").read_text(encoding="utf-8")


@pytest.fixture
def article_rec_html() -> str:
    return (FIX / "finam_article_recommendation.html").read_text(encoding="utf-8")


# ---------- _parse_listing ----------


def test_parse_listing_extracts_many_items(listing_html: str) -> None:
    hits = list(_parse_listing(listing_html, base_url="https://www.finam.ru"))
    # Реальная фикстура — около 69 уникальных items
    assert len(hits) >= 50


def test_parse_listing_url_date_to_utc(listing_html: str) -> None:
    """URL даты YYYYMMDD-HHMM Moscow → UTC (-3h)."""
    hits = list(_parse_listing(listing_html, base_url="https://www.finam.ru"))
    for h in hits:
        assert h.published_at.tzinfo == timezone.utc
        assert h.published_at.year == 2026
        # Moscow time всегда +3, UTC должна быть < Moscow
        assert 4 <= h.published_at.month <= 5  # период с recon (16 апр - 21 мая)


def test_parse_listing_dedups(listing_html: str) -> None:
    hits = list(_parse_listing(listing_html, base_url="https://www.finam.ru"))
    urls = [h.url for h in hits]
    assert len(urls) == len(set(urls))


def test_parse_listing_skips_invalid_date_in_url() -> None:
    """URL без YYYYMMDD-HHMM суффикса не должен включаться."""
    html = """
    <a href="/publications/item/x5-news-20260429-0524/">good</a>
    <a href="/publications/item/no-date-at-all/">bad</a>
    <a href="/publications/item/some-99999999-9999/">bad — invalid date</a>
    """
    hits = list(_parse_listing(html, base_url="https://www.finam.ru"))
    # Только первый match (с валидной датой)
    assert len(hits) == 1
    assert hits[0].url == "https://www.finam.ru/publications/item/x5-news-20260429-0524/"


# ---------- _slug_relevant ----------


def test_slug_relevant_matches_x5() -> None:
    assert _slug_relevant("x5-otchitalsya-za-kvartal")
    assert _slug_relevant("29-aprelya-x5-predstavit-finansovye-rezultaty")
    assert _slug_relevant("iks-5-novosti")


def test_slug_relevant_matches_x5_brands() -> None:
    assert _slug_relevant("pyaterochka-otkryla-magazin")
    assert _slug_relevant("perekrestok-nowiy-format")
    assert _slug_relevant("chizhik-rastet")


def test_slug_relevant_matches_competitors() -> None:
    assert _slug_relevant("magnit-otchitalsya")
    assert _slug_relevant("lenta-novosti")
    assert _slug_relevant("fix-price-otkrytie")


def test_slug_relevant_matches_sistema_holding() -> None:
    """AFK Sistema — X5 ownership context (recommendation kase из codex)."""
    assert _slug_relevant("aktsii-afk-sistema-ne-vyglyadyat-privlekatelnym")
    assert _slug_relevant("sistema-otchitalas")


def test_slug_relevant_rejects_broad_market_noise() -> None:
    """SpaceX, Ethereum, золото — не релевант X5."""
    assert not _slug_relevant("spacex-zapustil-raketu")
    assert not _slug_relevant("ethereum-vyros-na-5-protsent")
    assert not _slug_relevant("zoloto-pobilo-rekord")
    assert not _slug_relevant("amazon-otchet-za-kvartal")
    assert not _slug_relevant("bill-ackman-protiv-shorts")


def test_slug_relevant_word_boundary() -> None:
    """Слово 'lenta' не должно матчить случайные 'kalenta' или 'lentochka'."""
    assert not _slug_relevant("kalentar-na-may")  # "lent" внутри другого слова — но мы fixed "lenta", есть `(?:^|-)lenta(?:-|$)`
    assert _slug_relevant("lenta-otchitalas")
    assert _slug_relevant("nashlas-lenta-novostey-pro-lenta")  # есть hyphen-bounded "lenta"


# ---------- _parse_article ----------


def test_parse_article_news_extracts_fields(article_news_html: str) -> None:
    hit = ListingHit(
        url="https://www.finam.ru/publications/item/29-aprelya-x5-predstavit-finansovye-rezultaty-za-1-kvartal-po-msfo-20260429-0524/",
        slug="29-aprelya-x5-predstavit-finansovye-rezultaty-za-1-kvartal-po-msfo",
        published_at=datetime(2026, 4, 29, 2, 24, tzinfo=timezone.utc),
    )
    item = _parse_article(hit.url, article_news_html, listing_meta=hit)
    assert item.url == hit.url
    assert "X5" in item.headline or "x5" in item.headline.lower()
    assert item.body
    assert len(item.body) > 50
    assert item.published_at == hit.published_at


def test_parse_article_recommendation_extracts_fields(article_rec_html: str) -> None:
    hit = ListingHit(
        url="https://www.finam.ru/publications/item/aktsii-afk-sistema-ne-vyglyadyat-privlekatelnym-obektom-dlya-portfelnykh-investitsiy-20260417-0950/",
        slug="aktsii-afk-sistema-ne-vyglyadyat-privlekatelnym-obektom-dlya-portfelnykh-investitsiy",
        published_at=datetime(2026, 4, 17, 6, 50, tzinfo=timezone.utc),
    )
    item = _parse_article(hit.url, article_rec_html, listing_meta=hit)
    assert "Систем" in item.headline
    assert item.body and len(item.body) > 100


def test_parse_article_missing_body_raises() -> None:
    hit = ListingHit(url="https://x.x/", slug="x", published_at=datetime.now(timezone.utc))
    with pytest.raises(_ParseError):
        _parse_article("https://x.x/", "<html><meta property='og:title' content='t'></html>", listing_meta=hit)


# ---------- FinamSource validation ----------


@pytest.fixture
def fetch_context(tmp_path: Path) -> FetchContext:
    seed = tmp_path / "seed.csv"
    seed.write_text(
        "full_name,status,brand\nИгорь Шехтерман,CEO,X5\n",
        encoding="utf-8",
    )
    cfg = Config(
        start_date="2026-05-01",
        llm_provider="openai", llm_model="gpt-5-mini",
        output_root=tmp_path / "out",
        db_path=tmp_path / "db.sqlite",
        auto_run=False, timezone="Europe/Moscow",
        companies=[CompanyCfg("X5", None, ["finam"], str(seed),
                              aliases=["X5"], finam_ticker="x5")],
        sources={"finam": SourceCfg("finam", "Finam", "https://www.finam.ru/", "finam", True)},
    )
    db.init_db(cfg)
    conn = sqlite3.connect(cfg.db_path)
    cid = conn.execute("SELECT id FROM companies WHERE name='X5'").fetchone()[0]
    sid = conn.execute("SELECT id FROM sources WHERE code='finam'").fetchone()[0]
    conn.close()
    return FetchContext(cfg.companies[0], cid, sid, cfg.db_path)


def test_finam_raises_without_context() -> None:
    with pytest.raises(ValueError, match="FetchContext"):
        FinamSource(base_url="https://www.finam.ru/", context=None)


def test_finam_raises_without_finam_ticker(tmp_path: Path) -> None:
    seed = tmp_path / "seed.csv"
    seed.write_text("full_name,status,brand\nX,Y,\n", encoding="utf-8")
    cfg = Config(
        start_date="2026-05-01",
        llm_provider="openai", llm_model="gpt-5-mini",
        output_root=tmp_path / "out", db_path=tmp_path / "db.sqlite",
        auto_run=False, timezone="Europe/Moscow",
        companies=[CompanyCfg("X5", None, ["finam"], str(seed), finam_ticker=None)],
        sources={"finam": SourceCfg("finam", "F", "https://x.x/", "finam", True)},
    )
    db.init_db(cfg)
    conn = sqlite3.connect(cfg.db_path)
    cid = conn.execute("SELECT id FROM companies WHERE name='X5'").fetchone()[0]
    sid = conn.execute("SELECT id FROM sources WHERE code='finam'").fetchone()[0]
    conn.close()
    ctx = FetchContext(cfg.companies[0], cid, sid, cfg.db_path)
    with pytest.raises(ValueError, match="finam_ticker"):
        FinamSource(base_url="https://x.x/", context=ctx)


def test_finam_normalizes_ticker_to_lower(tmp_path: Path) -> None:
    seed = tmp_path / "seed.csv"
    seed.write_text("full_name,status,brand\nX,Y,\n", encoding="utf-8")
    cfg = Config(
        start_date="2026-05-01",
        llm_provider="openai", llm_model="gpt-5-mini",
        output_root=tmp_path / "out", db_path=tmp_path / "db.sqlite",
        auto_run=False, timezone="Europe/Moscow",
        companies=[CompanyCfg("X5", None, ["finam"], str(seed), finam_ticker="  X5  ")],
        sources={"finam": SourceCfg("finam", "F", "https://x.x/", "finam", True)},
    )
    db.init_db(cfg)
    conn = sqlite3.connect(cfg.db_path)
    cid = conn.execute("SELECT id FROM companies WHERE name='X5'").fetchone()[0]
    sid = conn.execute("SELECT id FROM sources WHERE code='finam'").fetchone()[0]
    conn.close()
    ctx = FetchContext(cfg.companies[0], cid, sid, cfg.db_path)
    src = FinamSource(base_url="https://x.x/", context=ctx)
    assert src._ticker == "x5"


def test_finam_raises_on_invalid_ticker(tmp_path: Path) -> None:
    seed = tmp_path / "seed.csv"
    seed.write_text("full_name,status,brand\nX,Y,\n", encoding="utf-8")
    cfg = Config(
        start_date="2026-05-01",
        llm_provider="openai", llm_model="gpt-5-mini",
        output_root=tmp_path / "out", db_path=tmp_path / "db.sqlite",
        auto_run=False, timezone="Europe/Moscow",
        companies=[CompanyCfg("X5", None, ["finam"], str(seed), finam_ticker="RTKM-P")],
        sources={"finam": SourceCfg("finam", "F", "https://x.x/", "finam", True)},
    )
    db.init_db(cfg)
    conn = sqlite3.connect(cfg.db_path)
    cid = conn.execute("SELECT id FROM companies WHERE name='X5'").fetchone()[0]
    sid = conn.execute("SELECT id FROM sources WHERE code='finam'").fetchone()[0]
    conn.close()
    ctx = FetchContext(cfg.companies[0], cid, sid, cfg.db_path)
    with pytest.raises(ValueError, match=r"\[a-z0-9\]\+"):
        FinamSource(base_url="https://x.x/", context=ctx)
