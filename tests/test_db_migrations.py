"""Tests for db.py schema migrations.

v1 → v2: added news.item_type column (task 04).
v2 → v3: added `recommendations` + `recommendation_persons` tables (task 06).
"""
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


def _tables(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()


def _indexes(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()


def _user_version(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


V3_TABLES = {
    "companies",
    "sources",
    "news",
    "persons",
    "news_persons",
    "recommendations",
    "recommendation_persons",
}


# ---------------------------------------------------------------------------
# v3 schema on fresh DB
# ---------------------------------------------------------------------------


def test_init_db_creates_v3_schema_on_fresh_db(cfg: Config) -> None:
    db.init_db(cfg)
    assert _user_version(cfg.db_path) == 3
    assert _tables(cfg.db_path) == V3_TABLES
    # news.item_type сохраняется (γ-legacy путь для finam-recs)
    assert "item_type" in _columns(cfg.db_path, "news")
    # recommendations имеет новые структурные колонки
    rec_cols = _columns(cfg.db_path, "recommendations")
    for col in ("target_price", "recommendation_action", "potential_pct", "multipliers_json"):
        assert col in rec_cols, f"missing recommendations.{col}"


def test_v3_indexes_created(cfg: Config) -> None:
    db.init_db(cfg)
    idx = _indexes(cfg.db_path)
    assert "idx_recommendations_company_date" in idx
    assert "idx_recommendations_status" in idx
    assert "idx_recommendation_persons_person" in idx


# ---------------------------------------------------------------------------
# Migration chain v1 → v3
# ---------------------------------------------------------------------------


def _create_v1_db(db_path: Path) -> None:
    """Manually create v1 schema (no item_type, no recommendations)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
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
    conn.execute(
        "INSERT INTO sources (code, name, base_url, enabled) VALUES ('x5_ir', 'X5 IR', 'https://x5.ru/', 1)"
    )
    conn.execute("INSERT INTO companies (name, start_date) VALUES ('X5', NULL)")
    conn.execute(
        "INSERT INTO news (company_id, source_id, url, headline, body, published_at) "
        "VALUES (1, 1, 'https://x.x/1', 'Old news', 'body', '2026-04-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()


def test_init_db_migrates_v1_to_v3(cfg: Config) -> None:
    """v1 DB → init_db: добавляет item_type (v2) и recommendations (v3)."""
    _create_v1_db(cfg.db_path)
    assert "item_type" not in _columns(cfg.db_path, "news")
    assert _user_version(cfg.db_path) == 1

    db.init_db(cfg)

    assert _user_version(cfg.db_path) == 3
    assert "item_type" in _columns(cfg.db_path, "news")
    assert "recommendations" in _tables(cfg.db_path)
    assert "recommendation_persons" in _tables(cfg.db_path)
    # Existing news row сохранилась с default item_type='news'
    conn = sqlite3.connect(cfg.db_path)
    try:
        rows = list(conn.execute("SELECT headline, item_type FROM news"))
    finally:
        conn.close()
    assert rows == [("Old news", "news")]


def test_init_db_idempotent_on_v3(cfg: Config) -> None:
    """Повторный init_db на v3 — no-op."""
    db.init_db(cfg)
    assert _user_version(cfg.db_path) == 3
    db.init_db(cfg)
    assert _user_version(cfg.db_path) == 3
    assert _tables(cfg.db_path) == V3_TABLES


def test_ensure_migrated_upgrades_v1_to_v3(cfg: Config) -> None:
    """ensure_migrated на v1 БД: цепочка v1 → v2 → v3."""
    _create_v1_db(cfg.db_path)

    db.ensure_migrated(cfg)

    assert _user_version(cfg.db_path) == 3
    assert "item_type" in _columns(cfg.db_path, "news")
    assert "recommendations" in _tables(cfg.db_path)
    assert "recommendation_persons" in _tables(cfg.db_path)


def test_ensure_migrated_upgrades_v2_to_v3(cfg: Config) -> None:
    """ensure_migrated на полноценной v2 БД: создаёт только новые v3 таблицы."""
    # Создаём v2 — сначала init_db на свежем пути, затем downgrade pragma
    db.init_db(cfg)
    conn = sqlite3.connect(cfg.db_path)
    try:
        # Имитируем v2: дропаем v3 таблицы и сбрасываем версию
        conn.execute("DROP TABLE recommendation_persons")
        conn.execute("DROP TABLE recommendations")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()

    assert _user_version(cfg.db_path) == 2
    assert "recommendations" not in _tables(cfg.db_path)

    db.ensure_migrated(cfg)

    assert _user_version(cfg.db_path) == 3
    assert "recommendations" in _tables(cfg.db_path)
    assert "recommendation_persons" in _tables(cfg.db_path)


def test_ensure_migrated_noop_on_v3(cfg: Config) -> None:
    """ensure_migrated на v3 — мгновенный no-op."""
    db.init_db(cfg)
    assert _user_version(cfg.db_path) == 3
    db.ensure_migrated(cfg)
    assert _user_version(cfg.db_path) == 3


def test_ensure_migrated_partial_v3_recovery(cfg: Config) -> None:
    """Partial-v3 scenario: только recommendations создана, recommendation_persons нет,
    user_version=2. ensure_migrated должен дочинить (CREATE IF NOT EXISTS no-op'ает
    существующую recommendations, добавляет недостающую recommendation_persons,
    выставляет version=3)."""
    db.init_db(cfg)
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.execute("DROP TABLE recommendation_persons")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()

    assert _user_version(cfg.db_path) == 2
    assert "recommendations" in _tables(cfg.db_path)
    assert "recommendation_persons" not in _tables(cfg.db_path)

    db.ensure_migrated(cfg)

    assert _user_version(cfg.db_path) == 3
    assert "recommendations" in _tables(cfg.db_path)
    assert "recommendation_persons" in _tables(cfg.db_path)


class _BoomConn:
    """Wrapper-conn для unit-теста транзакционного rollback'а _migrate_to_v3.

    Делегирует всё на реальный sqlite3.Connection, но бросает OperationalError
    на конкретной CREATE TABLE инструкции — симулирует partial-failure внутри
    транзакции, чтобы проверить что user_version не сдвинется."""

    def __init__(self, real: sqlite3.Connection, fail_on_substring: str) -> None:
        self._real = real
        self._fail = fail_on_substring

    def execute(self, sql: str, *args: object, **kwargs: object) -> sqlite3.Cursor:
        if self._fail in sql and "CREATE TABLE" in sql:
            raise sqlite3.OperationalError("simulated failure")
        return self._real.execute(sql, *args, **kwargs)

    def __enter__(self) -> sqlite3.Connection:
        return self._real.__enter__()

    def __exit__(self, *args: object) -> object:
        return self._real.__exit__(*args)

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


def test_migrate_to_v3_transactional_rollback(cfg: Config) -> None:
    """Если внутри _migrate_to_v3 операция упадёт, user_version остаётся на
    дотранзакционном уровне и таблицы не появляются.

    Симулируем сбой через _BoomConn, который бросает OperationalError на CREATE
    TABLE recommendation_persons. user_version=2 ставится ДО _migrate_to_v3 в
    ensure_migrated, поэтому проверяем что версия не поднялась до 3."""
    # Подготовим валидную v2 БД (без v3 таблиц)
    db.init_db(cfg)
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.execute("DROP TABLE recommendation_persons")
        conn.execute("DROP TABLE recommendations")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()
    assert _user_version(cfg.db_path) == 2

    real = db.connect(cfg.db_path)
    boom = _BoomConn(real, fail_on_substring="recommendation_persons")
    try:
        with pytest.raises(sqlite3.OperationalError, match="simulated failure"):
            db._migrate_to_v3(boom)  # type: ignore[arg-type]
    finally:
        real.close()

    # user_version так и остался 2; recommendations создавалась внутри
    # транзакции, но `with conn:` сделал rollback при exception.
    assert _user_version(cfg.db_path) == 2
    assert "recommendations" not in _tables(cfg.db_path)
    assert "recommendation_persons" not in _tables(cfg.db_path)


# ---------------------------------------------------------------------------
# Legacy v1 → v2 sanity (covered by chained migration above, but keep explicit)
# ---------------------------------------------------------------------------


def test_v1_to_v2_item_type_defaults_to_news(cfg: Config) -> None:
    """Существующие v1-строки получают item_type='news' после v2 миграции."""
    _create_v1_db(cfg.db_path)
    db.init_db(cfg)
    conn = sqlite3.connect(cfg.db_path)
    try:
        item_types = [row[0] for row in conn.execute("SELECT item_type FROM news")]
    finally:
        conn.close()
    assert all(it == "news" for it in item_types)
