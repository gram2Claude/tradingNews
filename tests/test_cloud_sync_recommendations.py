"""Tests for cloud_sync расширение (task 06 T6).

Covers:
* `_push_inner` order — companies → sources → persons → news → recommendations →
  news_persons → recommendation_persons (junctions последними)
* recommendations push'ит структурные поля
* recommendation_persons push'ится после recommendations (FK satisfied)
* schema.sql включает trading_news.recommendations + recommendation_persons
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from src import cloud_sync
from src.cloud_sync import pusher


class FakeCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> "FakeCursor":
        self.calls.append(("execute", sql, params))
        return self

    def executemany(self, sql: str, rows: list[tuple]) -> "FakeCursor":
        self.calls.append(("executemany", sql, rows))
        return self

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *a: object) -> None:
        pass


class FakeConn:
    def __init__(self) -> None:
        self._cursor = FakeCursor()
        self.committed = False
        self.rolled_back = False

    def cursor(self) -> FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        pass

    def __enter__(self) -> "FakeConn":
        return self

    def __exit__(self, *a: object) -> None:
        self.close()


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> FakeConn:
    conn = FakeConn()
    monkeypatch.setattr(pusher.psycopg, "connect", lambda *a, **k: conn)
    return conn


@pytest.fixture
def sqlite_with_recs(tmp_path: Path) -> Path:
    """Seed SQLite v3 с recommendations + recommendation_persons."""
    db_path = tmp_path / "db.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT UNIQUE,
                                start_date TEXT, created_at TEXT);
        CREATE TABLE sources (id INTEGER PRIMARY KEY, code TEXT UNIQUE,
                              name TEXT, base_url TEXT, enabled INTEGER);
        CREATE TABLE persons (id INTEGER PRIMARY KEY, company_id INTEGER,
                              full_name TEXT, status TEXT, brand TEXT, from_seed INTEGER,
                              UNIQUE (company_id, full_name));
        CREATE TABLE news (id INTEGER PRIMARY KEY, company_id INTEGER, source_id INTEGER,
                           url TEXT, headline TEXT, body TEXT, published_at TEXT,
                           fetched_at TEXT, mood TEXT, mood_reason TEXT, item_type TEXT,
                           status TEXT, error_msg TEXT, retry_count INTEGER, tokens_used INTEGER,
                           UNIQUE(source_id, url));
        CREATE TABLE news_persons (news_id INTEGER, person_id INTEGER,
                                   PRIMARY KEY (news_id, person_id));
        CREATE TABLE recommendations (id INTEGER PRIMARY KEY, company_id INTEGER,
                           source_id INTEGER, url TEXT, headline TEXT, body TEXT,
                           published_at TEXT, fetched_at TEXT, mood TEXT, mood_reason TEXT,
                           target_price REAL, recommendation_action TEXT,
                           potential_pct REAL, multipliers_json TEXT,
                           status TEXT, error_msg TEXT, retry_count INTEGER, tokens_used INTEGER,
                           UNIQUE(source_id, url));
        CREATE TABLE recommendation_persons (recommendation_id INTEGER, person_id INTEGER,
                                             PRIMARY KEY (recommendation_id, person_id));
    """)
    conn.execute("INSERT INTO companies (id, name) VALUES (1, 'X5')")
    conn.execute(
        "INSERT INTO sources (id, code, name, base_url, enabled) "
        "VALUES (1, 'lmsic', 'LMS Invest', 'https://lmsic.com/', 1)"
    )
    conn.execute(
        "INSERT INTO persons (id, company_id, full_name, status, brand, from_seed) "
        "VALUES (1, 1, 'Игорь Шехтерман', 'CEO', 'X5', 1)"
    )
    # Two recommendations с заполненными структурными полями
    conn.execute(
        "INSERT INTO recommendations (id, company_id, source_id, url, headline, body, "
        "published_at, fetched_at, mood, mood_reason, target_price, recommendation_action, "
        "potential_pct, multipliers_json, status, error_msg, retry_count, tokens_used) VALUES "
        "(1, 1, 1, 'https://lmsic.com/x5/1', 'X5 hold', 'Body1', "
        "'2026-05-01T10:00:00+00:00', '2026-05-02T10:00:00+00:00', 'neutral', 'ok', "
        "3200.0, 'hold', 12.5, '{\"P/E\":6.8}', 'analyzed', NULL, 0, 1500)"
    )
    conn.execute(
        "INSERT INTO recommendations (id, company_id, source_id, url, headline, body, "
        "published_at, fetched_at, mood, mood_reason, target_price, recommendation_action, "
        "potential_pct, multipliers_json, status, error_msg, retry_count, tokens_used) VALUES "
        "(2, 1, 1, 'https://lmsic.com/x5/2', 'X5 buy', 'Body2', "
        "'2026-05-02T10:00:00+00:00', '2026-05-03T10:00:00+00:00', 'pos', 'upside', "
        "4000.0, 'buy', 20.0, NULL, 'analyzed', NULL, 0, 1200)"
    )
    conn.execute(
        "INSERT INTO recommendation_persons (recommendation_id, person_id) VALUES (1, 1)"
    )
    conn.commit()
    conn.close()
    return db_path


# ----------------------------------------------------------- order


def test_push_inner_order_junctions_last(fake_pg: FakeConn, sqlite_with_recs: Path) -> None:
    """Порядок executemany'ев в push_all: companies → sources → persons → news →
    recommendations → news_persons → recommendation_persons. Junctions последними."""
    cloud_sync.push_all(sqlite_with_recs, "postgresql://fake")

    em_calls = [c for c in fake_pg.cursor().calls if c[0] == "executemany"]
    table_order = []
    expected = [
        "trading_news.companies",
        "trading_news.sources",
        "trading_news.persons",
        "trading_news.news",
        "trading_news.recommendations",
        "trading_news.news_persons",
        "trading_news.recommendation_persons",
    ]
    for _, sql, _ in em_calls:
        for table in expected:
            if f"INTO {table} " in sql:
                table_order.append(table)
                break

    # news и news_persons пусты в фикстуре → не попадут в em_calls.
    # Проверяем что present-таблицы идут в правильном относительном порядке.
    assert table_order == [
        "trading_news.companies",
        "trading_news.sources",
        "trading_news.persons",
        "trading_news.recommendations",
        "trading_news.recommendation_persons",
    ]


def test_push_recommendation_persons_after_recommendations(
    fake_pg: FakeConn, sqlite_with_recs: Path,
) -> None:
    """Junction trading_news.recommendation_persons пушится строго ПОСЛЕ
    trading_news.recommendations (FK satisfied)."""
    cloud_sync.push_all(sqlite_with_recs, "postgresql://fake")
    em_calls = [c for c in fake_pg.cursor().calls if c[0] == "executemany"]

    rec_idx = next(i for i, c in enumerate(em_calls)
                   if "INTO trading_news.recommendations " in c[1])
    rec_p_idx = next(i for i, c in enumerate(em_calls)
                     if "INTO trading_news.recommendation_persons " in c[1])
    assert rec_p_idx > rec_idx


# ----------------------------------------------------------- structural fields


def test_push_recommendations_carries_structural_fields(
    fake_pg: FakeConn, sqlite_with_recs: Path,
) -> None:
    """target_price, recommendation_action, potential_pct, multipliers_json
    долетают до executemany как значения, не теряются."""
    cloud_sync.push_all(sqlite_with_recs, "postgresql://fake")
    em_calls = [c for c in fake_pg.cursor().calls if c[0] == "executemany"]
    rec_call = next(c for c in em_calls if "INTO trading_news.recommendations " in c[1])
    rows = rec_call[2]
    assert len(rows) == 2

    # row layout (см. pusher._push_recommendations):
    # (source_code, url, company_name, headline, body, published_at, fetched_at,
    #  mood, mood_reason, target_price, recommendation_action, potential_pct,
    #  multipliers_json, status, error_msg, retry_count, tokens_used)
    by_url = {r[1]: r for r in rows}
    r1 = by_url["https://lmsic.com/x5/1"]
    assert r1[0] == "lmsic"  # source_code
    assert r1[2] == "X5"  # company_name
    assert r1[9] == 3200.0  # target_price
    assert r1[10] == "hold"  # recommendation_action
    assert r1[11] == 12.5  # potential_pct
    assert r1[12] == '{"P/E":6.8}'  # multipliers_json

    r2 = by_url["https://lmsic.com/x5/2"]
    assert r2[9] == 4000.0
    assert r2[10] == "buy"
    assert r2[12] is None  # multipliers_json NULL


def test_push_recommendation_persons_carries_denormalized_keys(
    fake_pg: FakeConn, sqlite_with_recs: Path,
) -> None:
    """recommendation_persons пушится с натуральными ключами
    (source_code, url, company_name, person_full_name)."""
    cloud_sync.push_all(sqlite_with_recs, "postgresql://fake")
    em_calls = [c for c in fake_pg.cursor().calls if c[0] == "executemany"]
    rp_call = next(c for c in em_calls if "INTO trading_news.recommendation_persons " in c[1])
    rows = rp_call[2]
    assert len(rows) == 1
    # (source_code, url, company_name, person_full_name)
    assert rows[0] == ("lmsic", "https://lmsic.com/x5/1", "X5", "Игорь Шехтерман")


def test_push_stats_includes_recommendations(
    fake_pg: FakeConn, sqlite_with_recs: Path,
) -> None:
    """PushStats содержит расширенные поля recommendations + recommendation_persons."""
    stats = cloud_sync.push_all(sqlite_with_recs, "postgresql://fake")
    assert stats.recommendations == 2
    assert stats.recommendation_persons == 1
    # __str__ упоминает оба
    s = str(stats)
    assert "recommendations=2" in s
    assert "recommendation_persons=1" in s


def test_push_stats_zero_when_no_recommendations(
    fake_pg: FakeConn, tmp_path: Path,
) -> None:
    """На v3 БД без recommendations rows: stats.recommendations == 0, no executemany."""
    db_path = tmp_path / "empty.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT UNIQUE,
                                start_date TEXT, created_at TEXT);
        CREATE TABLE sources (id INTEGER PRIMARY KEY, code TEXT UNIQUE,
                              name TEXT, base_url TEXT, enabled INTEGER);
        CREATE TABLE persons (id INTEGER PRIMARY KEY, company_id INTEGER,
                              full_name TEXT, status TEXT, brand TEXT, from_seed INTEGER,
                              UNIQUE (company_id, full_name));
        CREATE TABLE news (id INTEGER PRIMARY KEY, company_id INTEGER, source_id INTEGER,
                           url TEXT, headline TEXT, body TEXT, published_at TEXT,
                           fetched_at TEXT, mood TEXT, mood_reason TEXT, item_type TEXT,
                           status TEXT, error_msg TEXT, retry_count INTEGER, tokens_used INTEGER,
                           UNIQUE(source_id, url));
        CREATE TABLE news_persons (news_id INTEGER, person_id INTEGER,
                                   PRIMARY KEY (news_id, person_id));
        CREATE TABLE recommendations (id INTEGER PRIMARY KEY, company_id INTEGER,
                           source_id INTEGER, url TEXT, headline TEXT, body TEXT,
                           published_at TEXT, fetched_at TEXT, mood TEXT, mood_reason TEXT,
                           target_price REAL, recommendation_action TEXT,
                           potential_pct REAL, multipliers_json TEXT,
                           status TEXT, error_msg TEXT, retry_count INTEGER, tokens_used INTEGER,
                           UNIQUE(source_id, url));
        CREATE TABLE recommendation_persons (recommendation_id INTEGER, person_id INTEGER,
                                             PRIMARY KEY (recommendation_id, person_id));
    """)
    conn.execute("INSERT INTO companies (id, name) VALUES (1, 'X5')")
    conn.commit()
    conn.close()

    stats = cloud_sync.push_all(db_path, "postgresql://fake")
    assert stats.recommendations == 0
    assert stats.recommendation_persons == 0
    em_calls = [c for c in fake_pg.cursor().calls if c[0] == "executemany"]
    assert not any("recommendations" in c[1] for c in em_calls)


# ----------------------------------------------------------- schema


def test_init_schema_includes_recommendations_tables(fake_pg: FakeConn) -> None:
    """schema.sql DDL включает обе новые таблицы + индексы."""
    cloud_sync.init_schema("postgresql://fake")
    cur = fake_pg.cursor()
    assert len(cur.calls) == 1
    _, sql, _ = cur.calls[0]
    assert "CREATE TABLE IF NOT EXISTS trading_news.recommendations" in sql
    assert "CREATE TABLE IF NOT EXISTS trading_news.recommendation_persons" in sql
    assert "target_price" in sql
    assert "recommendation_action" in sql
    assert "potential_pct" in sql
    assert "multipliers_json" in sql
    assert "idx_tn_recs_company_date" in sql
    assert "idx_tn_recs_status" in sql
