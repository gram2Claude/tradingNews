"""Tests for src/sources/lmsic.py — LMSIC trading recommendations parser.

Phase 4 implementation tracker:
- T1 scaffold + guard
- T2 listing parser + HTTP layer
- T3 _extract_fields (regex)
- T4 body cleaning + header
- T5 synthetic URL
- T6 integration fetch()
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from selectolax.parser import HTMLParser

from src import db
from src.config import CompanyCfg, Config, SourceCfg
from src.sources.base import FetchContext, ItemDestination
from src.sources.lmsic import (
    LmsicSource,
    _build_body,
    _build_headline,
    _build_url,
    _clean_body,
    _clean_text,
    _extract_action,
    _extract_fields,
    _extract_thesis,
    _parse_float,
    _slugify,
)

FIX = Path(__file__).parent / "fixtures"


# ---- fixtures ----------------------------------------------------------------


@pytest.fixture
def listing_html() -> str:
    return (FIX / "lmsic_listing.html").read_text(encoding="utf-8")


@pytest.fixture
def card_bodies(listing_html: str) -> dict[str, str]:
    """Map issuer → cleaned plain-text body для всех 10 идей на странице.

    Используется в T3/T4 тестах для проверки regex extraction на реальном тексте.
    """
    tree = HTMLParser(listing_html)
    out: dict[str, str] = {}
    for node in tree.css("li.ideas-page__list-item"):
        card = LmsicSource._parse_card(node)
        out[card["issuer"]] = _clean_body(card["body_html"])
    return out


@pytest.fixture
def x5_ctx(tmp_path: Path) -> FetchContext:
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
    src = LmsicSource(base_url="https://www.lmsic.com/", context=ctx)
    src._client.close()
    src._client = httpx.Client(
        transport=transport,
        headers={"User-Agent": src.user_agent},
        timeout=5.0,
        follow_redirects=False,
    )
    return src


# =============================================================================
# T1 — scaffold sanity
# =============================================================================


def test_item_destination_is_recommendations():
    assert LmsicSource.item_destination is ItemDestination.RECOMMENDATIONS


def test_init_without_context_raises():
    with pytest.raises(ValueError, match="FetchContext"):
        LmsicSource(base_url="https://www.lmsic.com/", context=None)


def test_code_attribute():
    assert LmsicSource.code == "lmsic"


# =============================================================================
# T2 — parser primitives + HTTP layer
# =============================================================================


def test_parse_listing_yields_10_items(listing_html: str) -> None:
    nodes = HTMLParser(listing_html).css("li.ideas-page__list-item")
    assert len(nodes) == 10


def test_parse_card_x5_top_item(listing_html: str) -> None:
    nodes = HTMLParser(listing_html).css("li.ideas-page__list-item")
    card = LmsicSource._parse_card(nodes[0])
    assert card["date"] == "19.05.2026"
    assert card["issuer"] == "X5 Retail group"
    assert "EV/EBITDA = 3.31" in card["body_html"]


def test_parse_card_missing_date_raises() -> None:
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
    got = LmsicSource._published_at("19.05.2026")
    assert got == datetime(2026, 5, 19, 20, 59, tzinfo=timezone.utc)


def test_published_at_year_boundary() -> None:
    got = LmsicSource._published_at("31.12.2025")
    assert got == datetime(2025, 12, 31, 20, 59, tzinfo=timezone.utc)


def test_published_at_invalid_format_raises() -> None:
    with pytest.raises(ValueError):
        LmsicSource._published_at("2026-05-19")


def test_match_company_substring_x5(x5_ctx: FetchContext) -> None:
    with LmsicSource(base_url="https://www.lmsic.com/", context=x5_ctx) as src:
        assert src._match_company("X5 Retail group") is True
        assert src._match_company("Корпоративный центр Икс 5") is True
        assert src._match_company("x5 retail")


def test_match_company_rejects_unrelated(x5_ctx: FetchContext) -> None:
    with LmsicSource(base_url="https://www.lmsic.com/", context=x5_ctx) as src:
        assert src._match_company("ФосАгро") is False
        assert src._match_company("ММК") is False
        assert src._match_company("") is False


def test_match_company_real_fixture_only_one_x5(listing_html: str, x5_ctx: FetchContext) -> None:
    with LmsicSource(base_url="https://www.lmsic.com/", context=x5_ctx) as src:
        tree = HTMLParser(listing_html)
        matched = [
            LmsicSource._parse_card(n)["issuer"]
            for n in tree.css("li.ideas-page__list-item")
            if src._match_company(LmsicSource._parse_card(n)["issuer"])
        ]
        assert matched == ["X5 Retail group"]


def test_http_get_200_returns_body(x5_ctx: FetchContext) -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="OK-BODY"))
    with _src_with_transport(x5_ctx, transport) as src:
        assert src._http_get("https://www.lmsic.com/analytics/ideas/") == "OK-BODY"


def test_http_get_3xx_redirect_raises_runtime(x5_ctx: FetchContext) -> None:
    transport = httpx.MockTransport(
        lambda req: httpx.Response(302, headers={"location": "https://evil.example/"})
    )
    with _src_with_transport(x5_ctx, transport) as src:
        with pytest.raises(RuntimeError, match="redirect"):
            src._http_get("https://www.lmsic.com/analytics/ideas/")


def test_http_get_4xx_no_retry(x5_ctx: FetchContext) -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    with _src_with_transport(x5_ctx, httpx.MockTransport(handler)) as src:
        with pytest.raises(httpx.HTTPStatusError):
            src._http_get("https://www.lmsic.com/analytics/ideas/")
        assert calls["n"] == 1


def test_http_get_5xx_retries_3_times(x5_ctx: FetchContext) -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    with _src_with_transport(x5_ctx, httpx.MockTransport(handler)) as src:
        src._http_get.retry.wait = lambda *_a, **_kw: 0  # type: ignore[attr-defined]
        with pytest.raises(httpx.HTTPStatusError):
            src._http_get("https://www.lmsic.com/analytics/ideas/")
        assert calls["n"] == 3


def test_fetch_listing_uses_correct_url(x5_ctx: FetchContext) -> None:
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        return httpx.Response(200, text="<html></html>")

    with _src_with_transport(x5_ctx, httpx.MockTransport(handler)) as src:
        src._fetch_listing()
    assert captured["url"] == "https://www.lmsic.com/analytics/ideas/"


def test_context_manager_closes_httpx_client(x5_ctx: FetchContext) -> None:
    src = LmsicSource(base_url="https://www.lmsic.com/", context=x5_ctx)
    assert src._client.is_closed is False
    with src:
        pass
    assert src._client.is_closed is True


# =============================================================================
# T3 — _extract_fields regex extraction
# =============================================================================


def test_parse_float_unicode_spaces() -> None:
    # regular space, NBSP, narrow-NBSP, figure-space + запятая
    assert _parse_float("2 800,5") == 2800.5
    assert _parse_float("2 800,5") == 2800.5
    assert _parse_float("2 800,5") == 2800.5
    assert _parse_float("2 800,5") == 2800.5
    assert _parse_float("6,75") == 6.75
    assert _parse_float("garbage") is None
    assert _parse_float("") is None


def test_extract_action_positive_hold() -> None:
    assert _extract_action('Мы подтверждаем нашу рекомендацию «держать»') == "hold"


def test_extract_action_positive_buy() -> None:
    assert _extract_action('подтверждаем рекомендацию «покупать»') == "buy"


def test_extract_action_positive_sell() -> None:
    assert _extract_action('рекомендацию «продавать»') == "sell"


def test_extract_action_negation_recommend_quoted() -> None:
    """Сегежа: 'Мы не рекомендуем «покупать»' → sell."""
    assert _extract_action('Мы не рекомендуем «покупать» ценные бумаги') == "sell"


def test_extract_action_negation_recommend_no_quotes() -> None:
    """ММК: 'Мы не рекомендуем приобретать' → sell (без кавычек)."""
    assert _extract_action('Мы не рекомендуем приобретать ценные бумаги ММК') == "sell"


def test_extract_action_negation_advise_quoted() -> None:
    """Северсталь: 'не советуем «покупать»' → sell."""
    assert _extract_action('не советуем «покупать» ценные бумаги Северстали') == "sell"


def test_extract_action_negation_wins_over_positive() -> None:
    """Plan v2 P1.1: при коэкзистенции negation побеждает positive,
    иначе positive regex заматчит 'покупать' внутри 'не рекомендуем покупать'."""
    body = 'Мы не рекомендуем покупать. Подтверждаем нашу рекомендацию «держать».'
    assert _extract_action(body) == "sell"


def test_extract_action_none_when_no_match() -> None:
    assert _extract_action("Просто текст без рекомендации.") is None


def test_extract_action_only_russian_lexicon() -> None:
    """Plan v2 P2.2: _ACTION_MAP содержит только русские формы.
    Английское 'BUY' не должно матчиться."""
    assert _extract_action("We recommend BUY") is None


# Real fixture extract checks (T3 acceptance)


def test_extract_fields_x5(card_bodies: dict[str, str]) -> None:
    f = _extract_fields(card_bodies["X5 Retail group"])
    assert f["recommendation_action"] == "hold"
    assert f["target_price"] == 2800.0
    assert f["potential_pct"] == 14.0
    m = json.loads(f["multipliers_json"])
    assert m == {"ev_ebitda": 3.31, "p_e": 7.43, "nd_ebitda": 1.12}


def test_extract_fields_fosagro(card_bodies: dict[str, str]) -> None:
    """ФосАгро использует запятую в P/E ('P/E = 7,66') и 'Net Debt' (capital D)."""
    f = _extract_fields(card_bodies["ФосАгро"])
    assert f["recommendation_action"] == "hold"
    assert f["target_price"] == 8000.0
    assert f["potential_pct"] == 19.0
    m = json.loads(f["multipliers_json"])
    assert m["ev_ebitda"] == 6.75
    assert m["p_e"] == 7.66
    assert m["nd_ebitda"] == 1.78


def test_extract_fields_mmk_negation(card_bodies: dict[str, str]) -> None:
    """Plan v2 P1.1: 'Мы не рекомендуем приобретать' → sell.
    Нет target, multipliers partial (нет P/E)."""
    f = _extract_fields(card_bodies["ММК"])
    assert f["recommendation_action"] == "sell"
    assert f["target_price"] is None
    assert f["potential_pct"] is None
    m = json.loads(f["multipliers_json"])
    assert m == {"ev_ebitda": 2.97, "nd_ebitda": -0.99}


def test_extract_fields_severstal_negation(card_bodies: dict[str, str]) -> None:
    """Plan v2 P1.1: 'не советуем «покупать»' → sell."""
    f = _extract_fields(card_bodies["Северсталь"])
    assert f["recommendation_action"] == "sell"
    assert f["target_price"] is None
    m = json.loads(f["multipliers_json"])
    assert m == {"ev_ebitda": 5.93, "p_e": 58.30, "nd_ebitda": 0.41}


def test_extract_fields_rosseti_target_v(card_bodies: dict[str, str]) -> None:
    """Plan v2 P1.2: 'целевой цены в 400 руб' с optional `в`."""
    f = _extract_fields(card_bodies["Россети Ленэнерго"])
    assert f["recommendation_action"] == "hold"
    assert f["target_price"] == 400.0
    assert f["potential_pct"] == 19.0


def test_extract_fields_segezha_negation_no_target(card_bodies: dict[str, str]) -> None:
    """Сегежа: 'Мы не рекомендуем «покупать»' → sell, без target и multipliers."""
    f = _extract_fields(card_bodies["Сегежа"])
    assert f["recommendation_action"] == "sell"
    assert f["target_price"] is None
    assert f["multipliers_json"] is None


# =============================================================================
# T4 — body cleaning + preformatted header
# =============================================================================


def test_clean_text_normalizes_nbsp() -> None:
    assert _clean_text("foo bar") == "foo bar"
    assert _clean_text("foo bar") == "foo bar"
    assert _clean_text("&mdash;") == "—"


def test_clean_text_collapses_multi_newline() -> None:
    assert _clean_text("a\n\n\n\nb") == "a\n\nb"


def test_clean_text_empty_string() -> None:
    assert _clean_text("") == ""
    assert _clean_text(None) == ""  # type: ignore[arg-type]


def test_clean_body_preserves_paragraphs() -> None:
    """Plan v2 P2.6: `<br>` → `\\n` ДО selectolax, иначе abzac flattened."""
    html = "первая строка<br><br>вторая строка<br><br>третья"
    out = _clean_body(html)
    assert "первая строка\n\nвторая строка\n\nтретья" == out


def test_clean_body_strips_telegram_footer() -> None:
    html = "Идея текста<br><br>Подробности.<br><br>Телеграмм-канал: https://t.me/lmsstock"
    out = _clean_body(html)
    assert "Телеграмм" not in out
    assert "t.me" not in out
    assert out.endswith("Подробности.")


def test_clean_body_strips_telegram_footer_typos() -> None:
    """Telegram-footer regex покрывает опечатки автора (телеграм/телеграмм)."""
    for variant in (
        "Телеграмм-канал",
        "Телеграм-канал",
        "Телеграм канал",
    ):
        html = f"Текст<br><br>{variant}: https://t.me/lmsstock"
        out = _clean_body(html)
        assert "t.me" not in out, f"failed for variant: {variant!r}"


def test_clean_body_real_x5_fixture(card_bodies: dict[str, str]) -> None:
    """Sanity на реальном теле X5: содержит ≥4 abzac-разделителя (P2.6)
    и НЕ содержит Telegram-footer."""
    body = card_bodies["X5 Retail group"]
    assert body.count("\n\n") >= 4
    assert "t.me/lmsstock" not in body
    assert body.startswith("КЦ Икс 5: падение трафика")


def test_extract_thesis_first_paragraph() -> None:
    body = "Первая строка тезис.\n\nВторой абзац.\n\nТретий."
    assert _extract_thesis(body) == "Первая строка тезис."


def test_extract_thesis_empty() -> None:
    assert _extract_thesis("") == ""
    assert _extract_thesis("\n\n\n") == ""


def test_build_headline_prefixes_issuer() -> None:
    assert _build_headline("X5 Retail group", "падение трафика") == "X5 Retail group: падение трафика"


def test_build_headline_no_duplication() -> None:
    """Если thesis уже содержит issuer — не дублируем."""
    assert _build_headline("X5", "X5 покупает дистрибьютора") == "X5 покупает дистрибьютора"


def test_build_body_full_header() -> None:
    fields = {
        "recommendation_action": "hold",
        "target_price": 2800.0,
        "potential_pct": 14.0,
        "multipliers_json": json.dumps({"ev_ebitda": 3.31, "p_e": 7.43, "nd_ebitda": 1.12}),
    }
    out = _build_body(fields, "Тезис идеи.\n\nПодробности.")
    assert "Рекомендация: hold" in out
    assert "Целевая цена: 2800 ₽ (+14%)" in out
    assert "Мультипликаторы: EV/EBITDA=3.31 · P/E=7.43 · Net debt/EBITDA=1.12" in out
    # body после blank line, thesis НЕ продублирован (P2.1)
    assert "Тезис идеи." in out
    assert out.count("Тезис идеи.") == 1


def test_build_body_no_thesis_duplication_real_x5(card_bodies: dict[str, str]) -> None:
    body = card_bodies["X5 Retail group"]
    fields = _extract_fields(body)
    out = _build_body(fields, body)
    assert out.count("КЦ Икс 5: падение трафика во всех форматах сети") == 1


def test_build_body_partial_fields() -> None:
    """ММК: action=sell, нет target, partial multipliers (без P/E)."""
    fields = {
        "recommendation_action": "sell",
        "target_price": None,
        "potential_pct": None,
        "multipliers_json": json.dumps({"ev_ebitda": 2.97, "nd_ebitda": -0.99}),
    }
    out = _build_body(fields, "ММК body.")
    assert "Рекомендация: sell" in out
    assert "Целевая цена" not in out
    assert "EV/EBITDA=2.97" in out
    assert "P/E" not in out
    assert "Net debt/EBITDA=-0.99" in out


def test_build_body_no_fields_returns_body_unchanged() -> None:
    fields = {"recommendation_action": None, "target_price": None,
              "potential_pct": None, "multipliers_json": None}
    assert _build_body(fields, "Просто текст.") == "Просто текст."


# =============================================================================
# T5 — synthetic URL
# =============================================================================


def test_slugify_basic() -> None:
    assert _slugify("X5 Retail group") == "x5-retail-group"


def test_slugify_cyrillic() -> None:
    assert _slugify("ФосАгро") == "fosagro"
    assert _slugify("Россети Ленэнерго") == "rosseti-lenenergo"


def test_slugify_el5_energo() -> None:
    """Эл5-Энерго содержит цифру и дефис — должны сохраниться."""
    assert _slugify("Эл5-Энерго") == "el5-energo"


def test_slugify_strips_punctuation() -> None:
    assert _slugify("«держать!»") == "derzhat"


def test_slugify_empty() -> None:
    assert _slugify("") == ""
    assert _slugify("   ") == ""


def test_build_url_includes_thesis_slug() -> None:
    dt = datetime(2026, 5, 19, 20, 59, tzinfo=timezone.utc)
    url = _build_url(dt, "X5 Retail group", "КЦ Икс 5: падение трафика во всех форматах сети")
    assert url.startswith("https://www.lmsic.com/analytics/ideas/#20260519-x5-retail-group-")
    # 3 thesis words после issuer-slug
    fragment = url.split("#", 1)[1]
    after_issuer = fragment.split("x5-retail-group-", 1)[1]
    assert len(after_issuer.split("-")) == 3


def test_build_url_dedup_same_idea_twice() -> None:
    """Same (date, issuer, thesis) → same URL (INSERT OR IGNORE дедупит)."""
    dt = datetime(2026, 5, 19, 20, 59, tzinfo=timezone.utc)
    u1 = _build_url(dt, "X5 Retail group", "падение трафика")
    u2 = _build_url(dt, "X5 Retail group", "падение трафика")
    assert u1 == u2


def test_build_url_disambiguates_different_thesis() -> None:
    """Same (date, issuer) + different thesis → different URLs (P2.4)."""
    dt = datetime(2026, 5, 19, 20, 59, tzinfo=timezone.utc)
    u1 = _build_url(dt, "X5 Retail group", "падение трафика во всех форматах")
    u2 = _build_url(dt, "X5 Retail group", "рост дивидендов одобрен")
    assert u1 != u2


def test_build_url_no_thesis_still_valid() -> None:
    dt = datetime(2026, 5, 19, 20, 59, tzinfo=timezone.utc)
    url = _build_url(dt, "X5 Retail group", "")
    assert url == "https://www.lmsic.com/analytics/ideas/#20260519-x5-retail-group"


# =============================================================================
# T6 — fetch() integration
# =============================================================================


def _mock_fetch_src(ctx: FetchContext, html: str) -> LmsicSource:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=html))
    return _src_with_transport(ctx, transport)


def test_fetch_filters_by_date_first(listing_html: str, x5_ctx: FetchContext) -> None:
    """Plan v2 P1.5: date-filter применяется ПЕРВЫМ. Если since > newest_date,
    все 10 уходят в filtered_date, ни одного в filtered_issuer."""
    with _mock_fetch_src(x5_ctx, listing_html) as src:
        items = list(src.fetch(datetime(2026, 5, 20, tzinfo=timezone.utc)))
    assert items == []


def test_fetch_yields_x5_only(listing_html: str, x5_ctx: FetchContext, caplog) -> None:
    """since=2026-05-01: на странице 7 items проходят дату (>=01.05), 1 матчит X5."""
    import logging
    caplog.set_level(logging.INFO, logger="src.sources.lmsic")
    with _mock_fetch_src(x5_ctx, listing_html) as src:
        items = list(src.fetch(datetime(2026, 5, 1, tzinfo=timezone.utc)))
    assert len(items) == 1
    item = items[0]
    assert "x5-retail-group" in item.url
    assert item.recommendation_action == "hold"
    assert item.target_price == 2800.0
    assert item.potential_pct == 14.0
    assert item.body and "Рекомендация: hold" in item.body
    assert item.headline.startswith("X5 Retail group")
    # лог содержит реальные числа (для T8 live smoke acceptance)
    assert any("lmsic fetch" in r.message and "kept=1" in r.message for r in caplog.records)


def test_fetch_since_before_all_keeps_x5(listing_html: str, x5_ctx: FetchContext) -> None:
    """since=2025-01-01: дата пропускает все 10, issuer матчит только X5."""
    with _mock_fetch_src(x5_ctx, listing_html) as src:
        items = list(src.fetch(datetime(2025, 1, 1, tzinfo=timezone.utc)))
    assert len(items) == 1
    assert items[0].headline.startswith("X5 Retail group")


def test_fetch_returns_iterable_rawitem(listing_html: str, x5_ctx: FetchContext) -> None:
    """Sanity: fetch() возвращает iterable, items имеют все RawItem поля."""
    with _mock_fetch_src(x5_ctx, listing_html) as src:
        items = list(src.fetch(datetime(2026, 5, 1, tzinfo=timezone.utc)))
    it = items[0]
    assert it.url and it.headline and it.body
    assert it.published_at.tzinfo is not None
    assert it.multipliers_json is not None
    parsed = json.loads(it.multipliers_json)
    assert "ev_ebitda" in parsed
