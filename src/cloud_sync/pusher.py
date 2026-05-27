"""SQLite → Supabase Postgres one-way push.

Reads local SQLite read-only, UPSERTs all four tables (companies, sources,
persons, news) + the news_persons junction into the `trading_news` Postgres
schema. Single transaction; rolls back on any error.

SQLite uses integer surrogate ids that drift between machines; Postgres uses
the natural keys (company.name, source.code, etc.) so a fresh push from any
machine lands rows at the same physical location.

Connection string lives in `SUPABASE_DB_URL` (.env). Pooler URI required —
Supabase direct (port 5432) is IPv6-only. See README → "Облачное зеркало".
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import psycopg

log = logging.getLogger(__name__)

_SCHEMA_FILE = Path(__file__).parent / "schema.sql"


@dataclass
class PushStats:
    companies: int = 0
    sources: int = 0
    persons: int = 0
    news: int = 0
    recommendations: int = 0          # v3 (task 06)
    news_persons: int = 0
    recommendation_persons: int = 0   # v3 (task 06)

    def __str__(self) -> str:
        return (
            f"companies={self.companies} sources={self.sources} "
            f"persons={self.persons} news={self.news} "
            f"recommendations={self.recommendations} "
            f"news_persons={self.news_persons} "
            f"recommendation_persons={self.recommendation_persons}"
        )


def _mask_db_url(url: str) -> str:
    """postgresql://user:secret@host:port/db → postgresql://user:***@host:port/db."""
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", url)


def init_schema(db_url: str) -> None:
    """Apply schema.sql to the target database. Idempotent."""
    ddl = _SCHEMA_FILE.read_text(encoding="utf-8")
    log.info("cloud_sync: applying schema to %s", _mask_db_url(db_url))
    with psycopg.connect(db_url, sslmode="require", connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
    log.info("cloud_sync: schema applied")


def push_all(
    sqlite_path: Path,
    db_url: str,
    company: str | None = None,
) -> PushStats:
    """Upsert local SQLite rows into Postgres mirror.

    company: optional company.name filter. If None, pushes data for every company.
    """
    log.info(
        "cloud_sync: push start (sqlite=%s, target=%s, company=%s)",
        sqlite_path, _mask_db_url(db_url), company or "<all>",
    )
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        with psycopg.connect(db_url, sslmode="require", connect_timeout=15) as dst:
            try:
                stats = _push_inner(src, dst, company)
                dst.commit()
            except Exception:
                dst.rollback()
                raise
    finally:
        src.close()
    log.info("cloud_sync: push ok — %s", stats)
    return stats


def _push_inner(
    src: sqlite3.Connection,
    dst: psycopg.Connection,
    company: str | None,
) -> PushStats:
    """Explicit push order — junctions последними чтобы их FK были satisfied
    (codex 06 P1.4). Одна транзакция; rollback на любой ошибке.

    Order:
      1. companies
      2. sources
      3. persons
      4. news
      5. recommendations
      6. news_persons (FK → news + persons)
      7. recommendation_persons (FK → recommendations + persons)
    """
    stats = PushStats()

    with dst.cursor() as cur:
        stats.companies = _push_companies(src, cur, company)
        stats.sources = _push_sources(src, cur)
        stats.persons = _push_persons(src, cur, company)
        stats.news = _push_news(src, cur, company)
        stats.recommendations = _push_recommendations(src, cur, company)
        stats.news_persons = _push_news_persons(src, cur, company)
        stats.recommendation_persons = _push_recommendation_persons(src, cur, company)

    return stats


def _push_companies(
    src: sqlite3.Connection,
    cur: psycopg.Cursor,
    company: str | None,
) -> int:
    sql = "SELECT name, start_date FROM companies"
    params: tuple = ()
    if company:
        sql += " WHERE name = ?"
        params = (company,)
    rows = [(r["name"], r["start_date"]) for r in src.execute(sql, params)]
    if not rows:
        return 0
    cur.executemany(
        "INSERT INTO trading_news.companies (name, start_date) VALUES (%s, %s) "
        "ON CONFLICT (name) DO UPDATE SET start_date = EXCLUDED.start_date",
        rows,
    )
    return len(rows)


def _push_sources(src: sqlite3.Connection, cur: psycopg.Cursor) -> int:
    # Sources are not company-scoped; we always push the full list.
    rows = [
        (r["code"], r["name"], r["base_url"], bool(r["enabled"]))
        for r in src.execute("SELECT code, name, base_url, enabled FROM sources")
    ]
    if not rows:
        return 0
    cur.executemany(
        "INSERT INTO trading_news.sources (code, name, base_url, enabled) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (code) DO UPDATE SET "
        "name = EXCLUDED.name, base_url = EXCLUDED.base_url, enabled = EXCLUDED.enabled",
        rows,
    )
    return len(rows)


def _push_persons(
    src: sqlite3.Connection,
    cur: psycopg.Cursor,
    company: str | None,
) -> int:
    sql = (
        "SELECT c.name AS company_name, p.full_name, p.status, p.brand, p.from_seed "
        "FROM persons p JOIN companies c ON c.id = p.company_id"
    )
    params: tuple = ()
    if company:
        sql += " WHERE c.name = ?"
        params = (company,)
    rows = [
        (r["company_name"], r["full_name"], r["status"], r["brand"], bool(r["from_seed"]))
        for r in src.execute(sql, params)
    ]
    if not rows:
        return 0
    cur.executemany(
        "INSERT INTO trading_news.persons "
        "(company_name, full_name, status, brand, from_seed) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (company_name, full_name) DO UPDATE SET "
        "status = EXCLUDED.status, brand = EXCLUDED.brand, from_seed = EXCLUDED.from_seed",
        rows,
    )
    return len(rows)


def _push_news(
    src: sqlite3.Connection,
    cur: psycopg.Cursor,
    company: str | None,
) -> int:
    # δ-completion (task 08): item_type из news удалён.
    sql = (
        "SELECT s.code AS source_code, n.url, c.name AS company_name, "
        "n.headline, n.body, n.published_at, n.fetched_at, "
        "n.mood, n.mood_reason, n.status, "
        "n.error_msg, n.retry_count, n.tokens_used "
        "FROM news n "
        "JOIN companies c ON c.id = n.company_id "
        "JOIN sources s ON s.id = n.source_id"
    )
    params: tuple = ()
    if company:
        sql += " WHERE c.name = ?"
        params = (company,)
    rows = [
        (
            r["source_code"], r["url"], r["company_name"], r["headline"], r["body"],
            r["published_at"], r["fetched_at"], r["mood"], r["mood_reason"],
            r["status"], r["error_msg"], r["retry_count"],
            r["tokens_used"],
        )
        for r in src.execute(sql, params)
    ]
    if not rows:
        return 0
    cur.executemany(
        "INSERT INTO trading_news.news "
        "(source_code, url, company_name, headline, body, published_at, fetched_at, "
        " mood, mood_reason, status, error_msg, retry_count, tokens_used) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (source_code, url) DO UPDATE SET "
        "company_name = EXCLUDED.company_name, "
        "headline = EXCLUDED.headline, body = EXCLUDED.body, "
        "published_at = EXCLUDED.published_at, fetched_at = EXCLUDED.fetched_at, "
        "mood = EXCLUDED.mood, mood_reason = EXCLUDED.mood_reason, "
        "status = EXCLUDED.status, "
        "error_msg = EXCLUDED.error_msg, retry_count = EXCLUDED.retry_count, "
        "tokens_used = EXCLUDED.tokens_used",
        rows,
    )
    return len(rows)


def _push_news_persons(
    src: sqlite3.Connection,
    cur: psycopg.Cursor,
    company: str | None,
) -> int:
    sql = (
        "SELECT s.code AS source_code, n.url, c.name AS company_name, "
        "p.full_name AS person_full_name "
        "FROM news_persons np "
        "JOIN news n ON n.id = np.news_id "
        "JOIN persons p ON p.id = np.person_id "
        "JOIN companies c ON c.id = n.company_id "
        "JOIN sources s ON s.id = n.source_id"
    )
    params: tuple = ()
    if company:
        sql += " WHERE c.name = ?"
        params = (company,)
    rows = [
        (r["source_code"], r["url"], r["company_name"], r["person_full_name"])
        for r in src.execute(sql, params)
    ]
    if not rows:
        return 0
    cur.executemany(
        "INSERT INTO trading_news.news_persons "
        "(source_code, url, company_name, person_full_name) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (source_code, url, company_name, person_full_name) DO NOTHING",
        rows,
    )
    return len(rows)


def _push_recommendations(
    src: sqlite3.Connection,
    cur: psycopg.Cursor,
    company: str | None,
) -> int:
    sql = (
        "SELECT s.code AS source_code, r.url, c.name AS company_name, "
        "r.headline, r.body, r.published_at, r.fetched_at, "
        "r.mood, r.mood_reason, r.target_price, r.recommendation_action, "
        "r.potential_pct, r.multipliers_json, r.status, "
        "r.error_msg, r.retry_count, r.tokens_used "
        "FROM recommendations r "
        "JOIN companies c ON c.id = r.company_id "
        "JOIN sources s ON s.id = r.source_id"
    )
    params: tuple = ()
    if company:
        sql += " WHERE c.name = ?"
        params = (company,)
    rows = [
        (
            r["source_code"], r["url"], r["company_name"], r["headline"], r["body"],
            r["published_at"], r["fetched_at"], r["mood"], r["mood_reason"],
            r["target_price"], r["recommendation_action"], r["potential_pct"],
            r["multipliers_json"], r["status"], r["error_msg"], r["retry_count"],
            r["tokens_used"],
        )
        for r in src.execute(sql, params)
    ]
    if not rows:
        return 0
    cur.executemany(
        "INSERT INTO trading_news.recommendations "
        "(source_code, url, company_name, headline, body, published_at, fetched_at, "
        " mood, mood_reason, target_price, recommendation_action, potential_pct, "
        " multipliers_json, status, error_msg, retry_count, tokens_used) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (source_code, url) DO UPDATE SET "
        "company_name = EXCLUDED.company_name, "
        "headline = EXCLUDED.headline, body = EXCLUDED.body, "
        "published_at = EXCLUDED.published_at, fetched_at = EXCLUDED.fetched_at, "
        "mood = EXCLUDED.mood, mood_reason = EXCLUDED.mood_reason, "
        "target_price = EXCLUDED.target_price, "
        "recommendation_action = EXCLUDED.recommendation_action, "
        "potential_pct = EXCLUDED.potential_pct, "
        "multipliers_json = EXCLUDED.multipliers_json, "
        "status = EXCLUDED.status, error_msg = EXCLUDED.error_msg, "
        "retry_count = EXCLUDED.retry_count, tokens_used = EXCLUDED.tokens_used",
        rows,
    )
    return len(rows)


def _push_recommendation_persons(
    src: sqlite3.Connection,
    cur: psycopg.Cursor,
    company: str | None,
) -> int:
    sql = (
        "SELECT s.code AS source_code, r.url, c.name AS company_name, "
        "p.full_name AS person_full_name "
        "FROM recommendation_persons rp "
        "JOIN recommendations r ON r.id = rp.recommendation_id "
        "JOIN persons p ON p.id = rp.person_id "
        "JOIN companies c ON c.id = r.company_id "
        "JOIN sources s ON s.id = r.source_id"
    )
    params: tuple = ()
    if company:
        sql += " WHERE c.name = ?"
        params = (company,)
    rows = [
        (r["source_code"], r["url"], r["company_name"], r["person_full_name"])
        for r in src.execute(sql, params)
    ]
    if not rows:
        return 0
    cur.executemany(
        "INSERT INTO trading_news.recommendation_persons "
        "(source_code, url, company_name, person_full_name) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (source_code, url, company_name, person_full_name) DO NOTHING",
        rows,
    )
    return len(rows)
