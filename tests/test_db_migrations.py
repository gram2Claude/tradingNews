"""Tests for db.py schema migration v1 → v2 (added news.item_type)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src import db
from src.config import Config, CompanyCfg, SourceCfg


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    seed = tmp_path / "x5.csv"
    seed.write_text(
        "full_name,status,brand\nИгорь Шехтерман,CEO,X5\n",
        encoding="utf-8",
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
        sources={"x5_ir": SourceCfg("x5_ir", "X5 IR", "https://x5.ru/", "x5_ir", True)},
    )


def _columns(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _user_version(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def test_init_db_creates_v2_schema_on_fresh_db(cfg: Config) -> None:
    db.init_db(cfg)
    assert "item_type" in _columns(cfg.db_path, "news")
    assert _user_version(cfg.db_path) == 2


def test_init_db_migrates_v1_to_v2(cfg: Config) -> None:
    """Создаём v1 DB вручную, проверяем что init_db добавляет item_type."""
    # Manually create v1 schema (without item_type column)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path)
    conn.executescript("""
        CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                                start_date TEXT, created_at TEXT);
        CREATE TABLE sources (id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL,
                              name TEXT NOT NULL, base_url TEXT NOT NULL, enabled INTEGER DEFAULT 1);
        CREATE TABLE news (id INTEGER PRIMARY KEY, company_id INTEGER, source_id INTEGER,
                           url TEXT, headline TEXT, body TEXT, published_at TEXT,
                           fetched_at TEXT, mood TEXT, mood_reason TEXT,
                           status TEXT, error_msg TEXT, retry_count INTEGER, tokens_used INTEGER,
                           UNIQUE(source_id, url));
        CREATE TABLE persons (id INTEGER PRIMARY KEY, company_id INTEGER, full_name TEXT,
                              status TEXT, brand TEXT, from_seed INTEGER,
                              UNIQUE (company_id, full_name));
        CREATE TABLE news_persons (news_id INTEGER, person_id INTEGER, PRIMARY KEY (news_id, person_id));
        PRAGMA user_version = 1;
    """)
    # Insert a row that will get default 'news' after migration
    conn.execute(
        "INSERT INTO sources (code, name, base_url, enabled) VALUES ('x5_ir', 'X5 IR', 'https://x5.ru/', 1)"
    )
    conn.execute(
        "INSERT INTO companies (name, start_date) VALUES ('X5', NULL)"
    )
    conn.execute(
        "INSERT INTO news (company_id, source_id, url, headline, body, published_at) "
        "VALUES (1, 1, 'https://x.x/1', 'Old news', 'body', '2026-04-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    # Pre-condition: no item_type column
    assert "item_type" not in _columns(cfg.db_path, "news")
    assert _user_version(cfg.db_path) == 1

    # Run init_db — should migrate to v2
    db.init_db(cfg)

    # Post-condition: column exists, version bumped, old row got default 'news'
    assert "item_type" in _columns(cfg.db_path, "news")
    assert _user_version(cfg.db_path) == 2
    conn = sqlite3.connect(cfg.db_path)
    try:
        item_types = [row[0] for row in conn.execute("SELECT item_type FROM news")]
    finally:
        conn.close()
    assert all(it == "news" for it in item_types)


def test_init_db_idempotent_on_v2(cfg: Config) -> None:
    """Повторный init_db на v2 БД — no-op (не падает, не дублирует ALTER)."""
    db.init_db(cfg)
    assert _user_version(cfg.db_path) == 2
    # Second run
    db.init_db(cfg)
    assert _user_version(cfg.db_path) == 2
    assert "item_type" in _columns(cfg.db_path, "news")


def test_ensure_migrated_upgrades_v1_db(cfg: Config) -> None:
    """ensure_migrated на v1 БД добавляет item_type без полного init_db (без seed)."""
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path)
    conn.executescript("""
        CREATE TABLE news (id INTEGER PRIMARY KEY, item_int INTEGER);
        PRAGMA user_version = 1;
    """)
    conn.commit()
    conn.close()

    db.ensure_migrated(cfg)

    assert _user_version(cfg.db_path) == 2
    assert "item_type" in _columns(cfg.db_path, "news")


def test_ensure_migrated_noop_on_v2(cfg: Config) -> None:
    """ensure_migrated на v2 БД — мгновенный no-op."""
    db.init_db(cfg)
    assert _user_version(cfg.db_path) == 2
    db.ensure_migrated(cfg)
    assert _user_version(cfg.db_path) == 2


def test_ensure_migrated_fresh_db_creates_nothing(cfg: Config) -> None:
    """ensure_migrated на пустом пути — НЕ создаёт схему (это работа init_db).
    Просто bump'ит user_version (если БД пустая, news таблицы нет, _migrate_to_v2
    выходит no-op, и pragma поднимается до v2). Это безопасно: первый init_db
    создаст схему позже, его migration check тоже будет no-op.
    """
    db.ensure_migrated(cfg)
    # БД создалась но без таблиц
    assert _user_version(cfg.db_path) == 2
    assert _columns(cfg.db_path, "news") == set()  # news таблицы нет
