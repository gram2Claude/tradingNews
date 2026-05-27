"""Tests for db.status_counts — теперь UNION над news и recommendations."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src import db
from src.config import Config, CompanyCfg, SourceCfg


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


def _insert_sample(db_path: Path) -> None:
    """Вставить тестовые строки в news и recommendations."""
    conn = sqlite3.connect(db_path)
    try:
        # news: 2 analyzed, 1 new
        conn.executemany(
            "INSERT INTO news (company_id, source_id, url, headline, body, published_at, status) "
            "VALUES (1, 1, ?, 'h', 'b', '2026-05-01T00:00:00+00:00', ?)",
            [
                ("https://x.x/n1", "analyzed"),
                ("https://x.x/n2", "analyzed"),
                ("https://x.x/n3", "new"),
            ],
        )
        # recommendations: 1 analyzed
        conn.execute(
            "INSERT INTO recommendations (company_id, source_id, url, headline, body, published_at, status) "
            "VALUES (1, 1, 'https://x.x/r1', 'h', 'b', '2026-05-01T00:00:00+00:00', 'analyzed')"
        )
        conn.commit()
    finally:
        conn.close()


def test_status_counts_empty_db(cfg: Config) -> None:
    db.init_db(cfg)
    assert db.status_counts(cfg) == []


def test_status_counts_news_only(cfg: Config) -> None:
    db.init_db(cfg)
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.execute(
            "INSERT INTO news (company_id, source_id, url, headline, body, published_at, status) "
            "VALUES (1, 1, 'https://x.x/1', 'h', 'b', '2026-05-01T00:00:00+00:00', 'new')"
        )
        conn.commit()
    finally:
        conn.close()

    rows = db.status_counts(cfg)
    assert len(rows) == 1
    r = rows[0]
    assert r["company"] == "X5"
    assert r["kind"] == "news"
    assert r["status"] == "new"
    assert r["cnt"] == 1


def test_status_counts_both_tables(cfg: Config) -> None:
    """news и recommendations выдаются отдельными строками с колонкой kind."""
    db.init_db(cfg)
    _insert_sample(cfg.db_path)

    rows = db.status_counts(cfg)
    by_key = {(r["kind"], r["status"]): r["cnt"] for r in rows}
    assert by_key == {
        ("news", "analyzed"): 2,
        ("news", "new"): 1,
        ("recommendations", "analyzed"): 1,
    }
    # Ordering: company, kind, status
    assert [(r["kind"], r["status"]) for r in rows] == [
        ("news", "analyzed"),
        ("news", "new"),
        ("recommendations", "analyzed"),
    ]


def test_cmd_status_migrates_v2_db_before_querying(cfg: Config) -> None:
    """`cmd_status` на v2 БД должна вызвать ensure_migrated перед status_counts —
    иначе SELECT FROM recommendations упадёт с 'no such table' (codex 06 review P1)."""
    import argparse
    from src import cli

    # Создаём v2 БД руками (без таблиц recommendations + recommendation_persons)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path)
    conn.executescript("""
        CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                                start_date TEXT, created_at TEXT);
        CREATE TABLE sources (id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL,
                              name TEXT NOT NULL, base_url TEXT NOT NULL, enabled INTEGER DEFAULT 1);
        CREATE TABLE news (id INTEGER PRIMARY KEY, company_id INTEGER, source_id INTEGER,
                           url TEXT, headline TEXT, body TEXT, published_at TEXT,
                           fetched_at TEXT, mood TEXT, mood_reason TEXT, item_type TEXT,
                           status TEXT, error_msg TEXT, retry_count INTEGER, tokens_used INTEGER,
                           UNIQUE(source_id, url));
        CREATE TABLE persons (id INTEGER PRIMARY KEY, company_id INTEGER, full_name TEXT,
                              status TEXT, brand TEXT, from_seed INTEGER,
                              UNIQUE (company_id, full_name));
        CREATE TABLE news_persons (news_id INTEGER, person_id INTEGER,
                                   PRIMARY KEY (news_id, person_id));
        PRAGMA user_version = 2;
    """)
    conn.commit()
    conn.close()

    # Pre-condition: v2, no recommendations table
    conn = sqlite3.connect(cfg.db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "recommendations" not in tables
    finally:
        conn.close()

    # Подсунем cfg через load_config — патчим чтобы получить нашу cfg
    from unittest.mock import patch
    with patch.object(cli, "load_config", return_value=cfg):
        args = argparse.Namespace(config="config.yaml", company=None, verbose=False)
        rc = cli.cmd_status(args)
    assert rc == 0

    # После cmd_status БД должна быть v4 (migration v2→v3→v4 сработала)
    conn = sqlite3.connect(cfg.db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "recommendations" in tables
        assert "recommendation_persons" in tables
    finally:
        conn.close()


def test_status_counts_company_filter(cfg: Config) -> None:
    db.init_db(cfg)
    _insert_sample(cfg.db_path)

    rows_x5 = db.status_counts(cfg, "X5")
    assert len(rows_x5) == 3  # 2 news + 1 recs kind/status combinations

    rows_nope = db.status_counts(cfg, "NoSuchCompany")
    assert rows_nope == []
