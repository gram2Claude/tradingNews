"""Offline tests for the X5 IR parser using saved HTML fixtures."""
from __future__ import annotations

from datetime import timezone
from pathlib import Path

import pytest

from src.sources.x5_ir import extract_news_urls, parse_article

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def listing_html() -> str:
    return (FIX / "x5_listing.html").read_text(encoding="utf-8")


@pytest.fixture
def article_html() -> str:
    return (FIX / "x5_article.html").read_text(encoding="utf-8")


def test_extract_news_urls_finds_articles(listing_html: str) -> None:
    urls = list(extract_news_urls(listing_html))
    assert len(urls) >= 10
    for u in urls:
        assert u.startswith("https://www.x5.ru/ru/news/")
        assert u.endswith("/")


def test_extract_news_urls_unique_order_preserved(listing_html: str) -> None:
    urls = list(extract_news_urls(listing_html))
    assert len(urls) == len(set(urls))


def test_parse_article_extracts_fields(article_html: str) -> None:
    url = "https://www.x5.ru/ru/news/x5-test/"
    item = parse_article(url, article_html)

    assert item.url == url
    assert len(item.headline) > 10
    assert not item.headline.endswith(" - X5")
    assert item.published_at.tzinfo == timezone.utc
    assert item.published_at.year == 2026
    assert item.body is not None
    assert len(item.body) > 100


def test_parse_article_missing_published_time_raises() -> None:
    html = '<html><head><meta property="og:title" content="X"></head></html>'
    with pytest.raises(ValueError, match="article:published_time"):
        parse_article("https://x.x/", html)


def test_parse_article_missing_headline_raises() -> None:
    with pytest.raises(ValueError, match="no headline"):
        parse_article("https://x.x/", "<html></html>")


def test_parse_article_strips_x5_suffix() -> None:
    html = """<html><head>
        <meta property="og:title" content="Заголовок - X5">
        <meta property="article:published_time" content="2026-04-29T10:00:00+03:00">
        </head><body><div class="content">Тело текста длиннее ста символов
        нужно чтобы тесты могли проверить минимальную длину тела статьи и оно
        реально содержательно.</div></body></html>"""
    item = parse_article("https://x.x/", html)
    assert item.headline == "Заголовок"
