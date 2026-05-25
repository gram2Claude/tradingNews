"""Tests for src/sources/lmsic.py — LMSIC trading recommendations parser.

Phase 4 implementation tracker:
- T1: scaffold + guard (done)
- T2: listing parser + HTTP layer (this revision)
- T3: _extract_fields (regex) — pending
- T4: body cleaning + header — pending
- T5: synthetic URL — pending
- T6: integration fetch() — pending
- T7: full coverage (~25 tests target) — pending
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from selectolax.parser import HTMLParser

from src import db
from src.config import CompanyCfg, Config, SourceCfg
from src.sources.base import FetchContext, ItemDestination
from src.sources.lmsic import LmsicSource

FIX = Path(__file__).parent / "fixtures"


# ---- fixtures ----------------------------------------------------------------


@pytest.fixture
def listing_html() -> str:
    return (FIX / "lmsic_listing.html").read_text(encoding="utf-8")


@pytest.fixture
def x5_ctx(tmp_path: Path) -> FetchContext:
    """Minimal FetchContext с X5-aliases (mirrors plan v2 T2 acceptance)."""
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
        companies=[CompanyCfg(
            "X5", None, ["lmsic"], str(seed),
            aliases=["X5", "ИКС 5", "Корпоративный центр Икс 5"],
        )],
        sources={"lmsic": SourceCfg(
            "lmsic", "LMS Invest", "https://www.lmsic.com/", "lmsic", True
        )},
    )
    db.init_db(cfg)
    conn = sqlite3.connect(cfg.db_path)
    cid = conn.execute("SELECT id FROM companies WHERE name='X5'").fetchone()[0]
    sid = conn.execute("SELECT id FROM sources WHERE code='lmsic'").fetchone()[0]
    conn.close()
    return FetchContext(cfg.companies[0], cid, sid, cfg.db_path)


def _src_with_transport(ctx: FetchContext, transport: httpx.MockTransport) -> LmsicSource:
    """LmsicSource с моковым httpx.MockTransport вместо реальной сети."""
    src = LmsicSource(base_url="https://www.lmsic.com/", context=ctx)
    src._client.close()
    src._client = httpx.Client(
        transport=transport,
        headers={"User-Agent": src.user_agent},
        timeout=5.0,
        follow_redirects=False,
    )
    return src


# ---- T1 tests ----------------------------------------------------------------


def test_item_destination_is_recommendations():
    assert LmsicSource.item_destination is ItemDestination.RECOMMENDATIONS


def test_init_without_context_raises():
    with pytest.raises(ValueError, match="FetchContext"):
        LmsicSource(base_url="https://www.lmsic.com/", context=None)


def test_code_attribute():
    assert LmsicSource.code == "lmsic"


# ---- T2: parser primitives ---------------------------------------------------


def test_parse_listing_yields_10_items(listing_html: str) -> None:
    """Recon §8: первая страница содержит ровно 10 ideas."""
    tree = HTMLParser(listing_html)
    nodes = tree.css("li.ideas-page__list-item")
    assert len(nodes) == 10


def test_parse_card_x5_top_item(listing_html: str) -> None:
    """Top item должен быть X5 от 19.05.2026 (recon snapshot §8)."""
    tree = HTMLParser(listing_html)
    nodes = tree.css("li.ideas-page__list-item")
    card = LmsicSource._parse_card(nodes[0])
    assert card["date"] == "19.05.2026"
    assert card["issuer"] == "X5 Retail group"
    assert "EV/EBITDA = 3.31" in card["body_html"]
    assert "Подтверждаем нашу рекомендацию" in card["body_html"] or "подтверждаем" in card["body_html"].lower()


def test_parse_card_missing_date_raises() -> None:
    """Defensive: malformed card без даты → ValueError, не silent skip."""
    html = '<li class="ideas-page__list-item"><div class="ideas-card"><div class="ideas-card__title">X</div></div></li>'
    node = HTMLParser(html).css_first("li.ideas-page__list-item")
    assert node is not None
    with pytest.raises(ValueError, match="ideas-card__date"):
        LmsicSource._parse_card(node)


def test_parse_card_missing_title_raises() -> None:
    html = '<li class="ideas-page__list-item"><div class="ideas-card"><span class="ideas-card__date">01.01.2026</span></div></li>'
    node = HTMLParser(html).css_first("li.ideas-page__list-item")
    assert node is not None
    with pytest.raises(ValueError, match="ideas-card__title"):
        LmsicSource._parse_card(node)


def test_published_at_moscow_23_59_to_utc() -> None:
    """`DD.MM.YYYY` → 23:59 Europe/Moscow → 20:59 UTC same day (recon §12)."""
    got = LmsicSource._published_at("19.05.2026")
    expected = datetime(2026, 5, 19, 20, 59, tzinfo=timezone.utc)
    assert got == expected


def test_published_at_year_boundary() -> None:
    """31.12 23:59 Moscow → 31.12 20:59 UTC — invariant из CLAUDE.md
    (trading day follows Moscow, not UTC). Файл должен лечь в `2025_12/31`."""
    got = LmsicSource._published_at("31.12.2025")
    assert got == datetime(2025, 12, 31, 20, 59, tzinfo=timezone.utc)


def test_published_at_invalid_format_raises() -> None:
    with pytest.raises(ValueError):
        LmsicSource._published_at("2026-05-19")


def test_match_company_substring_x5(x5_ctx: FetchContext) -> None:
    """`"X5 Retail group"` содержит alias `"X5"` (case-insensitive substring)."""
    src = LmsicSource(base_url="https://www.lmsic.com/", context=x5_ctx)
    try:
        assert src._match_company("X5 Retail group") is True
        assert src._match_company("Корпоративный центр Икс 5") is True
        assert src._match_company("x5 retail")  # case-insensitive
    finally:
        src.close()


def test_match_company_rejects_unrelated(x5_ctx: FetchContext) -> None:
    src = LmsicSource(base_url="https://www.lmsic.com/", context=x5_ctx)
    try:
        assert src._match_company("ФосАгро") is False
        assert src._match_company("ММК") is False
        assert src._match_company("Сегежа") is False
        assert src._match_company("") is False
    finally:
        src.close()


def test_match_company_real_fixture_only_one_x5(listing_html: str, x5_ctx: FetchContext) -> None:
    """Recon snapshot §8: ровно 1 из 10 issuer'ов матчит X5."""
    src = LmsicSource(base_url="https://www.lmsic.com/", context=x5_ctx)
    try:
        tree = HTMLParser(listing_html)
        nodes = tree.css("li.ideas-page__list-item")
        matched = [
            LmsicSource._parse_card(n)["issuer"]
            for n in nodes
            if src._match_company(LmsicSource._parse_card(n)["issuer"])
        ]
        assert matched == ["X5 Retail group"]
    finally:
        src.close()


# ---- T2: HTTP layer (explicit status handling, codex P2.5) -------------------


def test_http_get_200_returns_body(x5_ctx: FetchContext) -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="OK-BODY"))
    src = _src_with_transport(x5_ctx, transport)
    try:
        assert src._http_get("https://www.lmsic.com/analytics/ideas/") == "OK-BODY"
    finally:
        src.close()


def test_http_get_3xx_redirect_raises_runtime(x5_ctx: FetchContext) -> None:
    """Listing endpoint не должен редиректить — это аномалия / SSRF surface.
    Plan v2 codex P2.5: НЕ skip, raise."""
    transport = httpx.MockTransport(
        lambda req: httpx.Response(302, headers={"location": "https://evil.example/"})
    )
    src = _src_with_transport(x5_ctx, transport)
    try:
        with pytest.raises(RuntimeError, match="redirect"):
            src._http_get("https://www.lmsic.com/analytics/ideas/")
    finally:
        src.close()


def test_http_get_4xx_no_retry(x5_ctx: FetchContext) -> None:
    """4xx terminal — tenacity не retry'ит. Один вызов, raise сразу."""
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    src = _src_with_transport(x5_ctx, transport)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            src._http_get("https://www.lmsic.com/analytics/ideas/")
        assert calls["n"] == 1
    finally:
        src.close()


def test_http_get_5xx_retries_3_times(x5_ctx: FetchContext) -> None:
    """5xx transient — tenacity retry до 3 попыток, потом reraise."""
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)
    src = _src_with_transport(x5_ctx, transport)
    # Speed up tenacity wait for this test
    src._http_get.retry.wait = lambda *_a, **_kw: 0  # type: ignore[attr-defined]
    try:
        with pytest.raises(httpx.HTTPStatusError):
            src._http_get("https://www.lmsic.com/analytics/ideas/")
        assert calls["n"] == 3
    finally:
        src.close()


def test_fetch_listing_uses_correct_url(x5_ctx: FetchContext) -> None:
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        return httpx.Response(200, text="<html></html>")

    transport = httpx.MockTransport(handler)
    src = _src_with_transport(x5_ctx, transport)
    try:
        src._fetch_listing()
        assert captured["url"] == "https://www.lmsic.com/analytics/ideas/"
    finally:
        src.close()


def test_context_manager_closes_httpx_client(x5_ctx: FetchContext) -> None:
    src = LmsicSource(base_url="https://www.lmsic.com/", context=x5_ctx)
    assert src._client.is_closed is False
    with src:
        pass
    assert src._client.is_closed is True
