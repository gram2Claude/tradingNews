"""Tests for fetcher dispatcher по Source.item_destination."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import db, fetcher
from src.config import Config, CompanyCfg, SourceCfg
from src.sources.base import ItemDestination, RawItem


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    seed = tmp_path / "x5.csv"
    seed.write_text("full_name,status,brand\nИгорь Шехтерман,CEO,X5\n", encoding="utf-8")
    return Config(
        start_date="2026-05-01",
        llm_provider="openai",
        llm_model="gpt-5-mini",
        output_root=tmp_path / "out",
        db_path=tmp_path / "db.sqlite",
        auto_run=False,
        timezone="Europe/Moscow",
        companies=[CompanyCfg("X5", None, ["x5_ir"], str(seed))],
        sources={"x5_ir": SourceCfg("x5_ir", "X5 IR", "https://x5.ru/", "x5_ir", True)},
    )


def _make_item(url: str = "https://x.x/1", *, target: float | None = None) -> RawItem:
    return RawItem(
        url=url,
        headline="h",
        body="b",
        published_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        target_price=target,
        recommendation_action="hold" if target else None,
        potential_pct=12.5 if target else None,
        multipliers_json='{"P/E":6.8}' if target else None,
    )


def test_dispatch_news_inserts_into_news_only(cfg: Config) -> None:
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    try:
        inserted = fetcher._insert(
            conn,
            company_id=1,
            source_id=1,
            item=_make_item(),
            destination=ItemDestination.NEWS,
        )
        conn.commit()
    finally:
        conn.close()
    assert inserted is True

    conn = sqlite3.connect(cfg.db_path)
    try:
        news_cnt = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        recs_cnt = conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]
    finally:
        conn.close()
    assert news_cnt == 1
    assert recs_cnt == 0


def test_dispatch_recommendations_inserts_into_recommendations_only(cfg: Config) -> None:
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    try:
        inserted = fetcher._insert(
            conn,
            company_id=1,
            source_id=1,
            item=_make_item(target=3200.0),
            destination=ItemDestination.RECOMMENDATIONS,
        )
        conn.commit()
    finally:
        conn.close()
    assert inserted is True

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        news_cnt = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        rec = conn.execute("SELECT * FROM recommendations").fetchone()
    finally:
        conn.close()
    assert news_cnt == 0
    assert rec is not None
    assert rec["target_price"] == 3200.0
    assert rec["recommendation_action"] == "hold"
    assert rec["potential_pct"] == 12.5
    assert rec["multipliers_json"] == '{"P/E":6.8}'


def test_dispatch_unknown_destination_raises(cfg: Config) -> None:
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    try:
        with pytest.raises(ValueError, match="unknown ItemDestination"):
            fetcher._insert(
                conn,
                company_id=1,
                source_id=1,
                item=_make_item(),
                destination="news",  # type: ignore[arg-type]
            )
    finally:
        conn.close()


def test_dispatch_news_uniqueness(cfg: Config) -> None:
    """Повторный INSERT с тем же url в news → no-op (UNIQUE constraint)."""
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    try:
        assert fetcher._insert(
            conn, company_id=1, source_id=1, item=_make_item("https://x.x/dup"),
            destination=ItemDestination.NEWS,
        ) is True
        assert fetcher._insert(
            conn, company_id=1, source_id=1, item=_make_item("https://x.x/dup"),
            destination=ItemDestination.NEWS,
        ) is False
        conn.commit()
    finally:
        conn.close()


def test_dispatch_recommendations_uniqueness(cfg: Config) -> None:
    """Повторный INSERT с тем же url в recommendations → no-op (UNIQUE constraint)."""
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    try:
        assert fetcher._insert(
            conn, company_id=1, source_id=1, item=_make_item("https://x.x/dup", target=100.0),
            destination=ItemDestination.RECOMMENDATIONS,
        ) is True
        assert fetcher._insert(
            conn, company_id=1, source_id=1, item=_make_item("https://x.x/dup", target=100.0),
            destination=ItemDestination.RECOMMENDATIONS,
        ) is False
        conn.commit()
    finally:
        conn.close()


def test_dispatch_recommendations_with_null_structural_fields(cfg: Config) -> None:
    """RawItem без structural fields → recommendations row с NULL'ами."""
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    try:
        fetcher._insert(
            conn,
            company_id=1,
            source_id=1,
            item=_make_item(),  # no target etc
            destination=ItemDestination.RECOMMENDATIONS,
        )
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rec = conn.execute("SELECT * FROM recommendations").fetchone()
    finally:
        conn.close()
    assert rec["target_price"] is None
    assert rec["recommendation_action"] is None
    assert rec["potential_pct"] is None
    assert rec["multipliers_json"] is None
