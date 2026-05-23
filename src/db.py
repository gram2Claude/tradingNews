from __future__ import annotations

import csv
import logging
import sqlite3
from pathlib import Path

from src.config import Config, PROJECT_ROOT

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS companies (
    id          INTEGER PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    start_date  TEXT,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sources (
    id        INTEGER PRIMARY KEY,
    code      TEXT UNIQUE NOT NULL,
    name      TEXT NOT NULL,
    base_url  TEXT NOT NULL,
    enabled   INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS news (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES companies(id),
    source_id     INTEGER NOT NULL REFERENCES sources(id),
    url           TEXT NOT NULL,
    headline      TEXT NOT NULL,
    body          TEXT,
    published_at  TEXT NOT NULL,
    fetched_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    mood          TEXT,
    mood_reason   TEXT,
    item_type     TEXT NOT NULL DEFAULT 'news',  -- 'news' | 'recommendation' (v2)
    status        TEXT DEFAULT 'new',
    error_msg     TEXT,
    retry_count   INTEGER DEFAULT 0,
    tokens_used   INTEGER,
    UNIQUE (source_id, url)
);

CREATE INDEX IF NOT EXISTS idx_news_company_date ON news(company_id, published_at);
CREATE INDEX IF NOT EXISTS idx_news_status ON news(status);

CREATE TABLE IF NOT EXISTS persons (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES companies(id),
    full_name   TEXT NOT NULL,
    status      TEXT,
    brand       TEXT,
    from_seed   INTEGER DEFAULT 0,
    UNIQUE (company_id, full_name)
);

CREATE TABLE IF NOT EXISTS news_persons (
    news_id    INTEGER NOT NULL REFERENCES news(id) ON DELETE CASCADE,
    person_id  INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    PRIMARY KEY (news_id, person_id)
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(cfg: Config) -> dict[str, int]:
    """Create schema, seed companies/sources/persons. Idempotent.

    Returns counts: {'companies': N, 'sources': N, 'persons': N}.
    """
    conn = connect(cfg.db_path)
    try:
        # Column-presence-aware migration (codex 04 P1.4):
        # `CREATE TABLE IF NOT EXISTS` не добавляет колонки на существующих таблицах.
        # Поэтому если v1 БД уже есть, нужен ALTER. Делаем перед executescript чтобы
        # порядок был детерминированный.
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if user_version < 2:
            _migrate_to_v2(conn)

        conn.executescript(SCHEMA_SQL)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        # Sources from config
        for code, src in cfg.sources.items():
            conn.execute(
                "INSERT INTO sources (code, name, base_url, enabled) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name, base_url=excluded.base_url, "
                "enabled=excluded.enabled",
                (code, src.name, src.base_url, int(src.enabled)),
            )

        # Companies + seed persons
        for company in cfg.companies:
            conn.execute(
                "INSERT INTO companies (name, start_date) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET start_date=excluded.start_date",
                (company.name, company.start_date),
            )
            cid = conn.execute(
                "SELECT id FROM companies WHERE name = ?", (company.name,)
            ).fetchone()["id"]

            if company.seed_persons:
                seed_file = (PROJECT_ROOT / company.seed_persons).resolve()
                if seed_file.exists():
                    _load_seed_persons(conn, cid, seed_file)
                else:
                    log.warning("seed file missing for %s: %s", company.name, seed_file)

        conn.commit()

        counts = {
            "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
            "sources": conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            "persons": conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0],
        }
        return counts
    finally:
        conn.close()


def ensure_migrated(cfg: Config) -> None:
    """Lightweight migration check. Called at start of cmd_fetch/analyze/report/cycle
    so a v1 DB picked up after `git pull` migrates without explicit `init-db`.

    Idempotent: PRAGMA user_version check skips no-op cases in ~1ms. Does NOT
    re-seed sources/persons (that's `init_db`'s job).
    """
    conn = connect(cfg.db_path)
    try:
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if user_version >= SCHEMA_VERSION:
            return
        _migrate_to_v2(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2: add news.item_type if column doesn't exist. Idempotent.

    Если таблица `news` ещё не создана (fresh DB) — выходим, последующий
    `executescript(SCHEMA_SQL)` создаст её сразу с колонкой item_type.
    """
    # Check existence of news table
    has_news = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='news'"
    ).fetchone()
    if not has_news:
        return  # fresh DB; SCHEMA_SQL создаст с item_type сразу
    # Check existence of item_type column
    columns = {row[1] for row in conn.execute("PRAGMA table_info(news)")}
    if "item_type" not in columns:
        conn.execute(
            "ALTER TABLE news ADD COLUMN item_type TEXT NOT NULL DEFAULT 'news'"
        )
        log.info("db: migrated v1 → v2 (added news.item_type)")


def _load_seed_persons(conn: sqlite3.Connection, company_id: int, csv_path: Path) -> None:
    with csv_path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            conn.execute(
                "INSERT INTO persons (company_id, full_name, status, brand, from_seed) "
                "VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT(company_id, full_name) DO UPDATE SET "
                "status=excluded.status, brand=excluded.brand, from_seed=1",
                (company_id, row["full_name"], row.get("status"), row.get("brand")),
            )


def status_counts(cfg: Config, company: str | None = None) -> list[sqlite3.Row]:
    """Return counts of news by status, optionally filtered by company name."""
    conn = connect(cfg.db_path)
    try:
        sql = (
            "SELECT c.name AS company, n.status, COUNT(*) AS cnt "
            "FROM news n JOIN companies c ON c.id = n.company_id "
        )
        params: tuple = ()
        if company:
            sql += "WHERE c.name = ? "
            params = (company,)
        sql += "GROUP BY c.name, n.status ORDER BY c.name, n.status"
        return list(conn.execute(sql, params))
    finally:
        conn.close()
