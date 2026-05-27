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


def test_analyze_cleans_body_before_llm_call(cfg: Config) -> None:
    """В LLM уходит body без HTML-entities и без inline JS/CSS-дампа.

    Defensive layer на случай уже хранящихся в БД «грязных» строк
    (до фикса парсера). Снижает токены и убирает шум из prompt'а.
    """
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    real_article = "Реальная новость про X5: " + "длинный текст статьи. " * 30
    garbage = " .rate:hover { color: #333; } function(){...}"
    dirty_headline = '&quot;ИКС 5&quot; отчитался'
    dirty_body = real_article + garbage
    nid = _seed_news(conn, dirty_headline, dirty_body)
    conn.commit()
    conn.close()

    fake = _fake_response(json.dumps({
        "mood": "pos",
        "mood_reason": "позитив",
        "item_type": "news",
    }))
    with patch.object(analyzer, "OpenAI") as cls:
        client = MagicMock()
        client.chat.completions.create.return_value = fake
        cls.return_value = client
        analyzer.analyze_all(cfg, "X5")

    # Проверяем, что именно пошло в LLM
    call_kwargs = client.chat.completions.create.call_args.kwargs
    user_msg = call_kwargs["messages"][1]["content"]
    assert "&quot;" not in user_msg, f"raw entity leaked into LLM input: {user_msg[:200]}"
    assert ".rate:hover" not in user_msg, "inline CSS leaked into LLM input"
    assert "function" not in user_msg, "inline JS leaked into LLM input"
    assert '"ИКС 5"' in user_msg  # entity размотан в реальные кавычки
    assert "Реальная новость" in user_msg
    # Запись в БД сохранена
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT status FROM news WHERE id=?", (nid,)).fetchone()[0] == "analyzed"


def test_analyze_dispatches_recommendation_to_recs_table(cfg: Config) -> None:
    """δ-completion: LLM возвращает item_type='recommendation' → строка
    мигрирует в recommendations table; в news её больше нет."""
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    nid = _seed_news(conn, "AFK Sistema аналитика",
                     "Брокер X понизил target по акциям AFK Sistema; рекомендация — sell. " * 10)
    src_url = conn.execute("SELECT url FROM news WHERE id=?", (nid,)).fetchone()[0]
    conn.commit()
    conn.close()

    fake = _fake_response(json.dumps({
        "mood": "neg",
        "mood_reason": "негативная оценка",
        "item_type": "recommendation",
    }))
    with patch.object(analyzer, "OpenAI") as cls:
        client = MagicMock()
        client.chat.completions.create.return_value = fake
        cls.return_value = client
        analyzer.analyze_all(cfg, "X5")

    conn = db.connect(cfg.db_path)
    # news row исчезла (cross-table move + CASCADE)
    assert conn.execute("SELECT 1 FROM news WHERE id=?", (nid,)).fetchone() is None
    # Recommendation row появилась с тем же source/url, mood='neg'
    rec = conn.execute(
        "SELECT mood, status, target_price FROM recommendations WHERE url=?",
        (src_url,),
    ).fetchone()
    assert rec is not None
    assert rec["status"] == "analyzed"
    assert rec["mood"] == "neg"
    assert rec["target_price"] is None  # finam-style row без structural fields


def test_analyze_keeps_news_in_news_table(cfg: Config) -> None:
    """δ-completion: LLM возвращает item_type='news' → строка остаётся в news."""
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    nid = _seed_news(conn, "Корпоративная новость X5",
                     "Длинный текст пресс-релиза о новых планах. " * 10)
    conn.commit()
    conn.close()

    fake = _fake_response(json.dumps({
        "mood": "pos", "mood_reason": "ok", "item_type": "news",
    }))
    with patch.object(analyzer, "OpenAI") as cls:
        client = MagicMock()
        client.chat.completions.create.return_value = fake
        cls.return_value = client
        analyzer.analyze_all(cfg, "X5")

    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT status, mood FROM news WHERE id=?", (nid,)).fetchone()
    assert row is not None
    assert row["status"] == "analyzed"
    assert row["mood"] == "pos"
    # Recommendations table не получила эту строку
    cnt = conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]
    assert cnt == 0


def test_analyze_defaults_item_type_to_news_if_missing(cfg: Config) -> None:
    """Если LLM не вернул item_type — fallback to 'news' (backwards-compat).
    После δ-completion: строка просто остаётся в news (никакого dispatch'а)."""
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    nid = _seed_news(conn, "Old-style mock", "Body длиннее MIN_BODY_CHARS чтобы попасть в LLM-ветку. " * 10)
    conn.commit()
    conn.close()

    # Old-style mock — без item_type
    fake = _fake_response(json.dumps({"mood": "pos", "mood_reason": "ok"}))
    with patch.object(analyzer, "OpenAI") as cls:
        client = MagicMock()
        client.chat.completions.create.return_value = fake
        cls.return_value = client
        analyzer.analyze_all(cfg, "X5")

    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT status, mood FROM news WHERE id=?", (nid,)).fetchone()
    assert row["status"] == "analyzed"
    assert row["mood"] == "pos"
    # И не уехала в recommendations
    cnt = conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]
    assert cnt == 0


def test_analyze_rejects_invalid_item_type(cfg: Config) -> None:
    """LLM возвращает невалидное item_type — row → status='error'."""
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    nid = _seed_news(conn, "Bad classifier", "Body длиннее MIN_BODY_CHARS чтобы попасть в LLM-ветку анализа. " * 10)
    conn.commit()
    conn.close()

    fake = _fake_response(json.dumps({
        "mood": "neutral", "mood_reason": "ok",
        "item_type": "garbage_value",
    }))
    with patch.object(analyzer, "OpenAI") as cls:
        client = MagicMock()
        client.chat.completions.create.return_value = fake
        cls.return_value = client
        analyzer.analyze_all(cfg, "X5")

    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT * FROM news WHERE id=?", (nid,)).fetchone()
    assert row["status"] == "error"
    assert "parse" in (row["error_msg"] or "")


def test_short_body_skip_path_stays_in_news(cfg: Config) -> None:
    """Short-body skip path: LLM не вызван, строка остаётся в news (без
    классификации). После δ-completion: news table не имеет item_type."""
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    nid = _seed_news(conn, "Короткая", "Коротко.")  # < MIN_BODY_CHARS
    conn.commit()
    conn.close()

    with patch.object(analyzer, "OpenAI") as cls:
        client = MagicMock()
        client.chat.completions.create.side_effect = AssertionError("LLM must not be called")
        cls.return_value = client
        analyzer.analyze_all(cfg, "X5")

    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT status, mood FROM news WHERE id=?", (nid,)).fetchone()
    assert row["status"] == "analyzed"
    assert row["mood"] == "neutral"
    # И не уехала в recommendations
    cnt = conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]
    assert cnt == 0


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


def test_three_rate_limits_keeps_row_new(cfg: Config) -> None:
    """Transient errors (rate limit / 5xx / timeout) never permanently kill a row.
    After 3 failed attempts in this session, status stays 'new' and the in-loop
    retry_count check skips it on future runs until reset."""
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
    assert r.aborted is False
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT * FROM news WHERE id=?", (nid,)).fetchone()
    # Transient: still 'new' — row can recover next session if user resets retry_count.
    assert row["status"] == "new"
    assert row["retry_count"] == 3
    assert "transient" in row["error_msg"]


def test_auth_error_aborts_batch_without_poisoning_rows(cfg: Config) -> None:
    """A global config error (wrong API key) must NOT mark any row 'error'.
    Otherwise fixing the key and rerunning would leave the rows stuck."""
    from openai import AuthenticationError
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    nid1 = _seed_news(conn, "Первая новость", "Тело первого пресс-релиза достаточной длины чтобы пройти порог MIN_BODY_CHARS точно.")
    nid2 = _seed_news(conn, "Вторая новость", "Тело второго пресс-релиза достаточной длины чтобы пройти порог MIN_BODY_CHARS точно.")
    conn.commit()
    conn.close()

    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    resp = httpx.Response(401, request=req)
    err = AuthenticationError("invalid key", response=resp, body=None)
    with patch.object(analyzer, "OpenAI") as cls:
        client = MagicMock()
        client.chat.completions.create.side_effect = err
        cls.return_value = client
        r = analyzer.analyze_all(cfg, "X5")

    assert r.aborted is True
    assert "Authentication" in (r.abort_reason or "")
    assert r.analyzed == 0
    assert r.errored == 0
    conn = db.connect(cfg.db_path)
    for nid in (nid1, nid2):
        row = conn.execute("SELECT * FROM news WHERE id=?", (nid,)).fetchone()
        assert row["status"] == "new", f"row {nid} got poisoned to {row['status']}"


def test_short_body_matches_persons_against_body(cfg: Config) -> None:
    """Short-body skip must still find persons mentioned only in body."""
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    # Headline doesn't mention any seed person; body does (short body though).
    nid = _seed_news(conn, "Новость дня", "Шехтерман.")
    conn.commit()
    conn.close()

    with patch.object(analyzer, "OpenAI") as cls:
        client = MagicMock()
        client.chat.completions.create.side_effect = AssertionError("LLM must not be called")
        cls.return_value = client
        r = analyzer.analyze_all(cfg, "X5")

    assert r.analyzed == 1
    conn = db.connect(cfg.db_path)
    links = conn.execute(
        "SELECT p.full_name FROM news_persons np JOIN persons p ON p.id=np.person_id "
        "WHERE np.news_id=?", (nid,)
    ).fetchall()
    assert [r["full_name"] for r in links] == ["Игорь Шехтерман"]
