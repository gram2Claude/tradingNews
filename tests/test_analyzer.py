"""Tests for the LLM analyzer with a mocked OpenAI client.

Covers:
* Happy path — JSON parsed, mood persisted, persons linked.
* Malformed JSON — status='error', no retry.
* Rate limit, then success — retry counter bumps.
* Three rate limits — status='error', retry_count=3.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import httpx

from src import analyzer, db
from src.config import Config, CompanyCfg, SourceCfg


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    seed = tmp_path / "seed.csv"
    seed.write_text(
        "full_name,status,brand\n"
        "Игорь Шехтерман,CEO,X5 Retail Group\n",
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
        sources={"x5_ir": SourceCfg("x5_ir", "X5 IR", "https://www.x5.ru/", "x5_ir", True)},
        openai_api_key="sk-test",
    )


def _seed_news(conn: sqlite3.Connection, headline: str, body: str) -> int:
    cid = conn.execute("SELECT id FROM companies WHERE name='X5'").fetchone()["id"]
    sid = conn.execute("SELECT id FROM sources WHERE code='x5_ir'").fetchone()["id"]
    cur = conn.execute(
        "INSERT INTO news (company_id, source_id, url, headline, body, published_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (cid, sid, f"https://x.x/{headline[:20]}", headline, body,
         datetime(2026, 5, 10, tzinfo=timezone.utc).isoformat()),
    )
    return cur.lastrowid


def _fake_response(content: str, prompt_tokens: int = 100, completion_tokens: int = 30):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


def test_happy_path(cfg: Config) -> None:
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    news_id = _seed_news(conn, "Шехтерман объявил план развития", "Подробный пресс-релиз о новых планах компании, описывающий направления развития на ближайшие годы.")
    conn.commit()
    conn.close()

    fake = _fake_response(json.dumps({"mood": "pos", "mood_reason": "позитивная новость"}))
    with patch.object(analyzer, "OpenAI") as cls:
        client = MagicMock()
        client.chat.completions.create.return_value = fake
        cls.return_value = client

        r = analyzer.analyze_all(cfg, "X5")

    assert r.analyzed == 1
    assert r.errored == 0
    assert r.tokens_total == 130

    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT * FROM news WHERE id=?", (news_id,)).fetchone()
    assert row["status"] == "analyzed"
    assert row["mood"] == "pos"
    assert row["mood_reason"] == "позитивная новость"
    assert row["tokens_used"] == 130
    # Person linked
    links = conn.execute(
        "SELECT p.full_name FROM news_persons np JOIN persons p ON p.id=np.person_id "
        "WHERE np.news_id=?", (news_id,)
    ).fetchall()
    assert [r["full_name"] for r in links] == ["Игорь Шехтерман"]


def test_malformed_json_marks_error(cfg: Config) -> None:
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    nid = _seed_news(conn, "Заголовок", "Полный текст пресс-релиза достаточной длины чтобы пройти порог MIN_BODY_CHARS и попасть в LLM-ветку анализа.")
    conn.commit()
    conn.close()

    fake = _fake_response("not json at all")
    with patch.object(analyzer, "OpenAI") as cls:
        client = MagicMock()
        client.chat.completions.create.return_value = fake
        cls.return_value = client
        r = analyzer.analyze_all(cfg, "X5")

    assert r.analyzed == 0
    assert r.errored == 1
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT * FROM news WHERE id=?", (nid,)).fetchone()
    assert row["status"] == "error"
    assert "parse" in row["error_msg"]
    assert row["retry_count"] == 1  # parse errors are not retried


def test_rate_limit_then_success(cfg: Config) -> None:
    from openai import RateLimitError
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    nid = _seed_news(conn, "Заголовок", "Полный текст пресс-релиза достаточной длины чтобы пройти порог MIN_BODY_CHARS и попасть в LLM-ветку анализа.")
    conn.commit()
    conn.close()

    fake = _fake_response(json.dumps({"mood": "neutral", "mood_reason": "ровно"}))
    # First call raises, second succeeds.
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    resp = httpx.Response(429, request=req)
    err = RateLimitError("rate limited", response=resp, body=None)
    with patch.object(analyzer, "OpenAI") as cls, \
         patch("src.analyzer.wait_exponential") as wait:
        wait.return_value = lambda rs: 0  # no sleep in tests
        client = MagicMock()
        client.chat.completions.create.side_effect = [err, fake]
        cls.return_value = client
        r = analyzer.analyze_all(cfg, "X5")

    assert r.analyzed == 1
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT * FROM news WHERE id=?", (nid,)).fetchone()
    assert row["status"] == "analyzed"
    assert row["mood"] == "neutral"
    assert row["retry_count"] == 2


def test_short_body_skipped_without_llm_call(cfg: Config) -> None:
    """Body below MIN_BODY_CHARS → status='analyzed', mood='neutral', no LLM call."""
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    nid = _seed_news(conn, "Шехтерман уволен", "Коротко.")  # well below MIN_BODY_CHARS
    conn.commit()
    conn.close()

    with patch.object(analyzer, "OpenAI") as cls:
        client = MagicMock()
        client.chat.completions.create.side_effect = AssertionError("LLM must not be called")
        cls.return_value = client
        r = analyzer.analyze_all(cfg, "X5")

    assert r.analyzed == 1
    assert r.tokens_total == 0
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT * FROM news WHERE id=?", (nid,)).fetchone()
    assert row["status"] == "analyzed"
    assert row["mood"] == "neutral"
    # Surname match should still run on the headline.
    links = conn.execute(
        "SELECT p.full_name FROM news_persons np JOIN persons p ON p.id=np.person_id "
        "WHERE np.news_id=?", (nid,)
    ).fetchall()
    assert [r["full_name"] for r in links] == ["Игорь Шехтерман"]


def test_three_rate_limits_marks_error(cfg: Config) -> None:
    from openai import RateLimitError
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    nid = _seed_news(conn, "Заголовок", "Полный текст пресс-релиза достаточной длины чтобы пройти порог MIN_BODY_CHARS и попасть в LLM-ветку анализа.")
    conn.commit()
    conn.close()

    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    resp = httpx.Response(429, request=req)
    err = RateLimitError("rate limited", response=resp, body=None)
    with patch.object(analyzer, "OpenAI") as cls, \
         patch("src.analyzer.wait_exponential") as wait:
        wait.return_value = lambda rs: 0
        client = MagicMock()
        client.chat.completions.create.side_effect = [err, err, err]
        cls.return_value = client
        r = analyzer.analyze_all(cfg, "X5")

    assert r.errored == 1
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT * FROM news WHERE id=?", (nid,)).fetchone()
    assert row["status"] == "error"
    assert row["retry_count"] == 3
    assert "network" in row["error_msg"]
