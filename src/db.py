from __future__ import annotations

import csv
import logging
import sqlite3
from pathlib import Path

from src.config import Config, PROJECT_ROOT

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

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
