"""Focused tests for fetcher._insert — cleanup-on-write invariant.

The contract: any RawItem that hits the DB through fetcher._insert is normalized
via src.text_cleanup.clean_text — HTML entities decoded, inline JS/CSS dumps
and press-release footers stripped. Analyzer / reporter become no-ops on
fresh data, and stay defense-in-depth for legacy rows already in the DB.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import db, fetcher
from src.config import Config, CompanyCfg, SourceCfg
from src.sources.base import RawItem


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    seed = tmp_path / "seed.csv"
    seed.write_text(
        "full_name,status,brand\nИгорь Шехтерман,CEO,X5\n", encoding="utf-8"
    )
    return Config(
        start_date="2026-05-01",
        llm_provider="openai",
        llm_model="gpt-5-mini",
        output_root=tmp_path / "out",
        db_path=tmp_path / "db.sqlite",
        auto_run=False,
        timezone="Europe/Moscow",
        companies=[CompanyCfg("X5", None, ["x5_ir"], str(seed))],
        sources={"x5_ir": SourceCfg("x5_ir", "X5 IR", "https://www.x5.ru/", "x5_ir", True)},
    )


def test_insert_cleans_html_entities_in_headline_and_body(cfg: Config) -> None:
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    cid = conn.execute("SELECT id FROM companies WHERE name='X5'").fetchone()["id"]
    sid = conn.execute("SELECT id FROM sources WHERE code='x5_ir'").fetchone()["id"]
    item = RawItem(
        url="https://www.x5.ru/ru/news/test-1/",
        headline="&quot;ИКС 5&quot; объявила результаты",
        body='Текст с &quot;цитатой&quot; внутри.',
        published_at=datetime(2026, 5, 19, 7, 0, tzinfo=timezone.utc),
    )
    assert fetcher._insert(conn, company_id=cid, source_id=sid, item=item) is True
    row = conn.execute("SELECT headline, body FROM news WHERE url=?", (item.url,)).fetchone()
    assert row["headline"] == '"ИКС 5" объявила результаты'
    assert row["body"] == 'Текст с "цитатой" внутри.'
    assert "&quot;" not in row["body"]


def test_insert_strips_x5_pdf_footer_from_body(cfg: Config) -> None:
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    cid = conn.execute("SELECT id FROM companies WHERE name='X5'").fetchone()["id"]
    sid = conn.execute("SELECT id FROM sources WHERE code='x5_ir'").fetchone()["id"]
    item = RawItem(
        url="https://www.x5.ru/ru/news/test-2/",
        headline="Х5 покупает дистрибьютора",
        body=(
            "Москва, 4 мая 2026. — Х5 объявила о приобретении компании ВКТ.\n\n"
            "По данным INFOLine, сеть стала лидером.\n\n"
            "Пресс-релиз\n\npdf, 407 КБ\n\nКатегория:\n\nРазвитие логистики"
        ),
        published_at=datetime(2026, 5, 4, 10, 0, tzinfo=timezone.utc),
    )
    fetcher._insert(conn, company_id=cid, source_id=sid, item=item)
    row = conn.execute("SELECT body FROM news WHERE url=?", (item.url,)).fetchone()
    assert "Пресс-релиз" not in row["body"]
    assert "pdf, 407" not in row["body"]
    assert "Категория" not in row["body"]
    assert "Развитие логистики" not in row["body"]
    assert "INFOLine" in row["body"]


def test_insert_strips_inline_js_css_from_body(cfg: Config) -> None:
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    cid = conn.execute("SELECT id FROM companies WHERE name='X5'").fetchone()["id"]
    sid = conn.execute("SELECT id FROM sources WHERE code='x5_ir'").fetchone()["id"]
    item = RawItem(
        url="https://www.finam.ru/publications/item/test-3-20260519-1000/",
        headline="Совет директоров рекомендовал дивиденды",
        body=(
            "Реальный текст статьи про X5. "
            ".rate:hover { color: #333; } "
            "if(window.foo){bar();}"
        ),
        published_at=datetime(2026, 5, 19, 7, 0, tzinfo=timezone.utc),
    )
    fetcher._insert(conn, company_id=cid, source_id=sid, item=item)
    row = conn.execute("SELECT body FROM news WHERE url=?", (item.url,)).fetchone()
    assert ".rate" not in row["body"]
    assert "window" not in row["body"]
    assert "Реальный текст" in row["body"]


def test_insert_preserves_none_body(cfg: Config) -> None:
    """body=None в RawItem не должен превратиться в '' (теряем сигнал)."""
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    cid = conn.execute("SELECT id FROM companies WHERE name='X5'").fetchone()["id"]
    sid = conn.execute("SELECT id FROM sources WHERE code='x5_ir'").fetchone()["id"]
    item = RawItem(
        url="https://www.x5.ru/ru/news/test-4/",
        headline="Заголовок",
        body=None,
        published_at=datetime(2026, 5, 19, 7, 0, tzinfo=timezone.utc),
    )
    fetcher._insert(conn, company_id=cid, source_id=sid, item=item)
    row = conn.execute("SELECT body FROM news WHERE url=?", (item.url,)).fetchone()
    assert row["body"] is None
