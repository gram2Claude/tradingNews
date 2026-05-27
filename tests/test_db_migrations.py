"""Tests for db.py schema migrations.

v1 → v2: added news.item_type column (task 04).
v2 → v3: added `recommendations` + `recommendation_persons` tables (task 06).
v3 → v4: dropped news.item_type column; moved finam-recs to recommendations
         (task 08, δ-completion).
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


V4_TABLES = {
    "companies",
    "sources",
    "news",
    "persons",
    "news_persons",
    "recommendations",
    "recommendation_persons",
}


# ---------------------------------------------------------------------------
# v4 schema on fresh DB
# ---------------------------------------------------------------------------


def test_init_db_creates_v4_schema_on_fresh_db(cfg: Config) -> None:
    db.init_db(cfg)
    assert _user_version(cfg.db_path) == 4
    assert _tables(cfg.db_path) == V4_TABLES
    # δ-completion: news.item_type удалён
    assert "item_type" not in _columns(cfg.db_path, "news")
    # recommendations имеет структурные колонки (task 06)
    rec_cols = _columns(cfg.db_path, "recommendations")
    for col in ("target_price", "recommendation_action", "potential_pct", "multipliers_json"):
        assert col in rec_cols, f"missing recommendations.{col}"


def test_v4_indexes_created(cfg: Config) -> None:
    db.init_db(cfg)
    idx = _indexes(cfg.db_path)
    assert "idx_recommendations_company_date" in idx
    assert "idx_recommendations_status" in idx
    assert "idx_recommendation_persons_person" in idx
    assert "idx_news_company_date" in idx
    assert "idx_news_status" in idx


# ---------------------------------------------------------------------------
# Migration chain v1 → v4
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


def test_init_db_migrates_v1_to_v4(cfg: Config) -> None:
    """v1 DB → init_db: цепочка v1→v2 (item_type added) → v3 (recs tables) →
    v4 (item_type dropped, finam-recs migrated). Existing news row сохраняется."""
    _create_v1_db(cfg.db_path)
    assert "item_type" not in _columns(cfg.db_path, "news")
    assert _user_version(cfg.db_path) == 1

    db.init_db(cfg)

    assert _user_version(cfg.db_path) == 4
    # После v4: item_type удалён
    assert "item_type" not in _columns(cfg.db_path, "news")
    assert "recommendations" in _tables(cfg.db_path)
    assert "recommendation_persons" in _tables(cfg.db_path)
    # Existing news row сохранилась — v2 повесил item_type='news', v4 убрал колонку
    # но строка осталась (item_type='news' rows не мигрируют, остаются в news)
    conn = sqlite3.connect(cfg.db_path)
    try:
        rows = list(conn.execute("SELECT headline FROM news"))
    finally:
        conn.close()
    assert rows == [("Old news",)]


def test_init_db_idempotent_on_v4(cfg: Config) -> None:
    """Повторный init_db на v4 — no-op."""
    db.init_db(cfg)
    assert _user_version(cfg.db_path) == 4
    db.init_db(cfg)
    assert _user_version(cfg.db_path) == 4
    assert _tables(cfg.db_path) == V4_TABLES


def test_ensure_migrated_upgrades_v1_to_v4(cfg: Config) -> None:
    """ensure_migrated на v1 БД: цепочка v1 → v2 → v3 → v4."""
    _create_v1_db(cfg.db_path)

    db.ensure_migrated(cfg)

    assert _user_version(cfg.db_path) == 4
    assert "item_type" not in _columns(cfg.db_path, "news")
    assert "recommendations" in _tables(cfg.db_path)
    assert "recommendation_persons" in _tables(cfg.db_path)


def test_ensure_migrated_upgrades_v3_to_v4_moves_finam_recs(cfg: Config) -> None:
    """Ключевой δ-completion тест: v3 БД с finam-rec строками →
    после ensure_migrated всё переехало в recommendations table."""
    # Создаём v3 БД (init_db уже мигрирует до v4; downgrade обратно к v3)
    db.init_db(cfg)
    conn = sqlite3.connect(cfg.db_path)
    try:
        # Имитируем v3: добавляем item_type column обратно (v4 миграция её удалила),
        # сбрасываем version
        conn.execute(
            "ALTER TABLE news ADD COLUMN item_type TEXT NOT NULL DEFAULT 'news'"
        )
        # Вставляем тестовые строки: одна news, одна recommendation
        conn.execute(
            "INSERT INTO news (company_id, source_id, url, headline, body, "
            "published_at, mood, mood_reason, item_type, status) "
            "VALUES (1, 1, 'https://x.x/news1', 'Plain news', 'b', "
            "'2026-05-01T00:00:00+00:00', 'pos', 'r', 'news', 'analyzed')"
        )
        conn.execute(
            "INSERT INTO news (company_id, source_id, url, headline, body, "
            "published_at, mood, mood_reason, item_type, status) "
            "VALUES (1, 1, 'https://x.x/rec1', 'Finam rec', 'b', "
            "'2026-05-02T00:00:00+00:00', 'pos', 'r', 'recommendation', 'analyzed')"
        )
        # Person-junction для recommendation row
        nid = conn.execute(
            "SELECT id FROM news WHERE url='https://x.x/rec1'"
        ).fetchone()[0]
        pid = conn.execute(
            "SELECT id FROM persons WHERE full_name='Игорь Шехтерман'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO news_persons (news_id, person_id) VALUES (?, ?)",
            (nid, pid),
        )
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    finally:
        conn.close()

    assert _user_version(cfg.db_path) == 3
    assert "item_type" in _columns(cfg.db_path, "news")

    db.ensure_migrated(cfg)

    assert _user_version(cfg.db_path) == 4
    assert "item_type" not in _columns(cfg.db_path, "news")

    # news осталась только plain-news строка
    conn = sqlite3.connect(cfg.db_path)
    try:
        news_rows = list(conn.execute("SELECT url, headline FROM news"))
        assert news_rows == [("https://x.x/news1", "Plain news")]

        # recommendations получили finam-rec строку с NULL structural fields
        rec_rows = list(conn.execute(
            "SELECT url, headline, mood, target_price, recommendation_action "
            "FROM recommendations"
        ))
        assert rec_rows == [("https://x.x/rec1", "Finam rec", "pos", None, None)]

        # junction news_persons → recommendation_persons
        rp = list(conn.execute(
            "SELECT person_id FROM recommendation_persons rp "
            "JOIN recommendations r ON r.id = rp.recommendation_id "
            "WHERE r.url = 'https://x.x/rec1'"
        ))
        assert len(rp) == 1
        # И news_persons для удалённой rec-строки очищены CASCADE'ом
        np = list(conn.execute("SELECT COUNT(*) FROM news_persons"))
        assert np == [(0,)]
    finally:
        conn.close()


def test_ensure_migrated_v3_to_v4_preserves_plain_news_persons(cfg: Config) -> None:
    """Regression: при rebuild'е news таблицы (DROP + RENAME) FK CASCADE
    не должен снести news_persons для plain-news строк. PRAGMA foreign_keys
    нужно выключить ДО открытия SAVEPOINT (внутри транзакции это no-op).
    """
    db.init_db(cfg)
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.execute(
            "ALTER TABLE news ADD COLUMN item_type TEXT NOT NULL DEFAULT 'news'"
        )
        conn.execute(
            "INSERT INTO news (company_id, source_id, url, headline, body, "
            "published_at, mood, mood_reason, item_type, status) "
            "VALUES (1, 1, 'https://x.x/news_with_person', 'Plain news', 'b', "
            "'2026-05-01T00:00:00+00:00', 'pos', 'r', 'news', 'analyzed')"
        )
        nid = conn.execute(
            "SELECT id FROM news WHERE url='https://x.x/news_with_person'"
        ).fetchone()[0]
        pid = conn.execute(
            "SELECT id FROM persons WHERE full_name='Игорь Шехтерман'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO news_persons (news_id, person_id) VALUES (?, ?)",
            (nid, pid),
        )
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    finally:
        conn.close()

    db.ensure_migrated(cfg)

    conn = sqlite3.connect(cfg.db_path)
    try:
        # news-строка выжила
        survived = list(conn.execute(
            "SELECT url FROM news WHERE url='https://x.x/news_with_person'"
        ))
        assert survived == [("https://x.x/news_with_person",)]
        # junction news_persons тоже выжил (НЕ скаскадился при DROP TABLE)
        np = list(conn.execute(
            "SELECT np.person_id FROM news_persons np "
            "JOIN news n ON n.id = np.news_id "
            "WHERE n.url='https://x.x/news_with_person'"
        ))
        assert len(np) == 1, f"news_persons wiped by FK cascade on DROP TABLE; got {np}"
    finally:
        conn.close()


def test_ensure_migrated_noop_on_v4(cfg: Config) -> None:
    """ensure_migrated на v4 — мгновенный no-op."""
    db.init_db(cfg)
    assert _user_version(cfg.db_path) == 4
    db.ensure_migrated(cfg)
    assert _user_version(cfg.db_path) == 4


def test_ensure_migrated_partial_v3_recovery(cfg: Config) -> None:
    """Partial-v3 scenario: только recommendations создана, recommendation_persons нет,
    user_version=2. ensure_migrated должен дочинить (CREATE IF NOT EXISTS no-op'ает
    существующую recommendations, добавляет недостающую recommendation_persons,
    затем v4 миграция чистит item_type если есть)."""
    db.init_db(cfg)
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.execute("DROP TABLE recommendation_persons")
        # Имитируем v2: добавляем item_type column обратно, сбрасываем version
        conn.execute(
            "ALTER TABLE news ADD COLUMN item_type TEXT NOT NULL DEFAULT 'news'"
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()

    assert _user_version(cfg.db_path) == 2
    assert "recommendations" in _tables(cfg.db_path)
    assert "recommendation_persons" not in _tables(cfg.db_path)
    assert "item_type" in _columns(cfg.db_path, "news")

    db.ensure_migrated(cfg)

    assert _user_version(cfg.db_path) == 4
    assert "recommendations" in _tables(cfg.db_path)
    assert "recommendation_persons" in _tables(cfg.db_path)
    assert "item_type" not in _columns(cfg.db_path, "news")


class _BoomConn:
    """Wrapper-conn для unit-теста транзакционного rollback'а _migrate_to_v3."""

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
    дотранзакционном уровне и таблицы не появляются."""
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

    assert _user_version(cfg.db_path) == 2
    assert "recommendations" not in _tables(cfg.db_path)
    assert "recommendation_persons" not in _tables(cfg.db_path)


# ---------------------------------------------------------------------------
# Legacy v1 → v2 sanity (covered by chained migration, kept explicit)
# ---------------------------------------------------------------------------


def test_v1_to_v2_then_v4_no_item_type_column(cfg: Config) -> None:
    """v1 строки выживают через v1→v2 (item_type='news' default) → v4
    (item_type column удалена; rows со старым item_type='news' остаются в news,
    rows с item_type='recommendation' уехали бы в recommendations — в данном
    fixture'е таких нет)."""
    _create_v1_db(cfg.db_path)
    db.init_db(cfg)
    assert _user_version(cfg.db_path) == 4
    assert "item_type" not in _columns(cfg.db_path, "news")
    conn = sqlite3.connect(cfg.db_path)
    try:
        headlines = [row[0] for row in conn.execute("SELECT headline FROM news")]
    finally:
        conn.close()
    assert headlines == ["Old news"]
