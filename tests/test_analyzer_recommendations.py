"""Tests for analyzer.recommendations-path (task 06 T4).

Pattern зеркалит test_analyzer.py — mocked OpenAI, sqlite на диске,
проверяем UPDATE recommendations + INSERT recommendation_persons, два
SYSTEM_PROMPT'а, два error-marker'а.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from openai import APIConnectionError, AuthenticationError

from src import analyzer, db
from src.config import Config, CompanyCfg, SourceCfg


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    seed = tmp_path / "seed.csv"
    seed.write_text(
        "full_name,status,brand\nИгорь Шехтерман,CEO,X5 Retail Group\n",
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


def _seed_recommendation(conn: sqlite3.Connection, headline: str, body: str) -> int:
    cid = conn.execute("SELECT id FROM companies WHERE name='X5'").fetchone()["id"]
    sid = conn.execute("SELECT id FROM sources WHERE code='x5_ir'").fetchone()["id"]
    cur = conn.execute(
        "INSERT INTO recommendations (company_id, source_id, url, headline, body, "
        "published_at, target_price, recommendation_action, potential_pct) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            cid, sid, f"https://x.x/{headline[:20]}", headline, body,
            datetime(2026, 5, 10, tzinfo=timezone.utc).isoformat(),
            3200.0, "hold", 12.5,
        ),
    )
    return cur.lastrowid


def _fake_response(content: str, prompt_tokens: int = 100, completion_tokens: int = 30):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


# ----------------------------------------------------------- happy path


def test_analyze_recommendation_happy_path(cfg: Config) -> None:
    """LLM возвращает mood/mood_reason → UPDATE recommendations + tokens учтены."""
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    body = "X5 показывает падение трафика. " * 30
    rec_id = _seed_recommendation(conn, "X5 hold", body)
    conn.commit()
    conn.close()

    fake = _fake_response(json.dumps({"mood": "neg", "mood_reason": "падение трафика"}))
    with patch.object(analyzer, "OpenAI") as cls:
        client = MagicMock()
        client.chat.completions.create.return_value = fake
        cls.return_value = client
        result = analyzer.analyze_all(cfg)

    assert result.recommendations_analyzed == 1
    assert result.recommendations_errored == 0
    assert result.news_analyzed == 0
    assert result.tokens_total == 130

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "analyzed"
    assert row["mood"] == "neg"
    assert row["mood_reason"] == "падение трафика"
    # Структурные поля сохранились
    assert row["target_price"] == 3200.0
    assert row["recommendation_action"] == "hold"


def test_analyze_recommendation_uses_recommendation_prompt(cfg: Config) -> None:
    """Recommendation-path шлёт SYSTEM_PROMPT_RECOMMENDATION (без item_type)."""
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    _seed_recommendation(conn, "X5 hold", "X5 в торговой идее. " * 30)
    conn.commit()
    conn.close()

    fake = _fake_response(json.dumps({"mood": "neutral", "mood_reason": "ok"}))
    captured_prompts: list[str] = []

    with patch.object(analyzer, "OpenAI") as cls:
        client = MagicMock()

        def _create(*, model, messages, response_format):  # type: ignore[no-untyped-def]
            captured_prompts.append(messages[0]["content"])
            return fake

        client.chat.completions.create.side_effect = _create
        cls.return_value = client
        analyzer.analyze_all(cfg)

    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    # Не должен содержать инструкций про item_type классификацию
    assert "item_type" not in prompt
    # Должен содержать ключевое recommendation-специфическое описание
    assert "торговых рекомендаций" in prompt or "торговая рекомендация" in prompt.lower()


def test_analyze_recommendation_tolerates_extra_item_type_in_response(cfg: Config) -> None:
    """Если LLM в recs-проходе всё-таки вернул item_type — игнорируем,
    парсинг не падает (тоlerant parse, P1.1 ack)."""
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    _seed_recommendation(conn, "X5", "X5 в торговой идее. " * 30)
    conn.commit()
    conn.close()

    fake = _fake_response(json.dumps({
        "mood": "pos",
        "mood_reason": "ok",
        "item_type": "garbage_value",  # лишнее поле игнорируется
    }))
    with patch.object(analyzer, "OpenAI") as cls:
        client = MagicMock()
        client.chat.completions.create.return_value = fake
        cls.return_value = client
        result = analyzer.analyze_all(cfg)

    assert result.recommendations_analyzed == 1
    assert result.recommendations_errored == 0


# ----------------------------------------------------------- persons link


def test_analyze_recommendation_links_persons(cfg: Config) -> None:
    """Person из seed CSV найден в body → recommendation_persons получает строку."""
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    body = "Игорь Шехтерман прокомментировал ситуацию X5. " * 20
    rec_id = _seed_recommendation(conn, "X5 CEO", body)
    conn.commit()
    conn.close()

    fake = _fake_response(json.dumps({"mood": "pos", "mood_reason": "ok"}))
    with patch.object(analyzer, "OpenAI") as cls:
        client = MagicMock()
        client.chat.completions.create.return_value = fake
        cls.return_value = client
        analyzer.analyze_all(cfg)

    conn = sqlite3.connect(cfg.db_path)
    try:
        links = list(conn.execute(
            "SELECT person_id FROM recommendation_persons WHERE recommendation_id=?",
            (rec_id,),
        ))
        # news_persons НЕ должен получить строку — это путь recommendations
        news_links = list(conn.execute(
            "SELECT * FROM news_persons WHERE news_id=?", (rec_id,)
        ))
    finally:
        conn.close()
    assert len(links) >= 1
    assert news_links == []


# ----------------------------------------------------------- error semantics


def test_analyze_recommendation_global_config_error_aborts_after_news(cfg: Config) -> None:
    """Global config error на recs-проходе: news уже закоммичены, batch прерывается
    с aborted_during='recommendations'."""
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    # Одна news (успешно проанализируется), одна recs (упадёт)
    cid = conn.execute("SELECT id FROM companies WHERE name='X5'").fetchone()["id"]
    sid = conn.execute("SELECT id FROM sources WHERE code='x5_ir'").fetchone()["id"]
    conn.execute(
        "INSERT INTO news (company_id, source_id, url, headline, body, published_at) "
        "VALUES (?, ?, 'https://x.x/n1', 'h', ?, ?)",
        (cid, sid, "X5 новость. " * 20, datetime(2026, 5, 10, tzinfo=timezone.utc).isoformat()),
    )
    _seed_recommendation(conn, "X5 rec", "X5 идея. " * 20)
    conn.commit()
    conn.close()

    fake_news = _fake_response(json.dumps({"mood": "pos", "mood_reason": "ok", "item_type": "news"}))
    auth_exc = AuthenticationError(
        message="bad key",
        response=httpx.Response(401, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")),
        body=None,
    )

    with patch.object(analyzer, "OpenAI") as cls:
        client = MagicMock()
        # news call → success; recs call → AuthenticationError
        client.chat.completions.create.side_effect = [fake_news, auth_exc]
        cls.return_value = client
        result = analyzer.analyze_all(cfg)

    assert result.news_analyzed == 1
    assert result.recommendations_analyzed == 0
    assert result.aborted is True
    assert result.aborted_during == "recommendations"

    # news закоммичена, rec осталась 'new'
    conn = sqlite3.connect(cfg.db_path)
    try:
        news_status = conn.execute("SELECT status FROM news LIMIT 1").fetchone()[0]
        rec_status = conn.execute("SELECT status FROM recommendations LIMIT 1").fetchone()[0]
    finally:
        conn.close()
    assert news_status == "analyzed"
    assert rec_status == "new"


def test_analyze_recommendation_transient_keeps_status_new(cfg: Config) -> None:
    """APIConnectionError на recs → retry exhausted → status='new', retry_count бампается."""
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    rec_id = _seed_recommendation(conn, "X5 rec", "X5 идея. " * 20)
    conn.commit()
    conn.close()

    transient = APIConnectionError(
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
    )

    with patch.object(analyzer, "OpenAI") as cls:
        client = MagicMock()
        client.chat.completions.create.side_effect = transient
        cls.return_value = client
        # Speed up retries — patch wait to be near-zero
        with patch.object(analyzer, "wait_exponential", lambda **kwargs: lambda r: 0):
            result = analyzer.analyze_all(cfg)

    assert result.recommendations_analyzed == 0
    assert result.recommendations_errored == 1

    conn = sqlite3.connect(cfg.db_path)
    try:
        row = conn.execute("SELECT status, retry_count FROM recommendations WHERE id=?", (rec_id,)).fetchone()
    finally:
        conn.close()
    assert row[0] == "new"  # transient — status стайт 'new'
    assert row[1] >= 1  # retry_count бампнут


def test_analyze_recommendation_parse_error_terminal(cfg: Config) -> None:
    """Malformed JSON → status='error' (terminal — повтор не поможет)."""
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    rec_id = _seed_recommendation(conn, "X5 rec", "X5 идея. " * 20)
    conn.commit()
    conn.close()

    fake = _fake_response("not a json {{ broken")
    with patch.object(analyzer, "OpenAI") as cls:
        client = MagicMock()
        client.chat.completions.create.return_value = fake
        cls.return_value = client
        result = analyzer.analyze_all(cfg)

    assert result.recommendations_errored == 1

    conn = sqlite3.connect(cfg.db_path)
    try:
        row = conn.execute("SELECT status, error_msg FROM recommendations WHERE id=?", (rec_id,)).fetchone()
    finally:
        conn.close()
    assert row[0] == "error"
    assert "parse" in (row[1] or "")


# ----------------------------------------------------------- empty pass


def test_analyze_empty_recommendations_table(cfg: Config) -> None:
    """analyze_all не падает если recommendations таблица пустая (0 iterations)."""
    db.init_db(cfg)
    # Нет вставок — обе таблицы пусты

    with patch.object(analyzer, "OpenAI") as cls:
        client = MagicMock()
        cls.return_value = client
        result = analyzer.analyze_all(cfg)

    assert result.news_analyzed == 0
    assert result.recommendations_analyzed == 0
    assert result.aborted is False
    # LLM не дёргался ни разу
    client.chat.completions.create.assert_not_called()
