"""LLM analysis pass: PR-mood classification + person extraction.

For each ``news`` row with ``status='new' AND retry_count < 3`` we:

1. Ask GPT-5 mini for ``mood`` and ``mood_reason`` (single JSON response).
2. Match seed persons against ``headline + body`` via :mod:`src.name_matcher`.
3. Persist results: ``news.mood``, ``news.mood_reason``, ``news.status='analyzed'``,
   ``news.tokens_used`` plus rows in ``news_persons``.

Network failures are retried (up to 3 attempts, exponential backoff) via
``tenacity``. Persistent failures flip ``status='error'`` with ``error_msg``
captured. JSON-shape failures are not retried (the model returned but
malformed — retrying gets us nothing).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)
from tenacity import (
    RetryError,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src import db
from src.config import Config
from src.name_matcher import NameMatcher

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
MAX_BODY_CHARS = 8000   # truncate runaway bodies before sending to LLM
MIN_BODY_CHARS = 50     # below this, skip LLM and mark as 'neutral' (not worth tokens)
LLM_TIMEOUT_S = 60.0    # ceiling per request; SDK default is 10 min — too long
VALID_MOODS = {"pos", "neutral", "neg"}

SYSTEM_PROMPT = (
    "Ты аналитик PR-тональности новостей о российских публичных компаниях. "
    "Для каждой новости определи PR-репутационную тональность для компании, о которой идёт речь. "
    "Это НЕ торговая оценка — нас интересует именно репутация бренда. "
    "Верни строго JSON с двумя полями:\n"
    "  - mood: одно из 'pos', 'neutral', 'neg'\n"
    "  - mood_reason: одно короткое предложение по-русски, почему такая тональность.\n"
    "Никакого Markdown, никаких ```. Только валидный JSON."
)


@dataclass
class AnalyzeResult:
    analyzed: int
    errored: int
    skipped_maxed_out: int
    tokens_total: int


def analyze_all(cfg: Config, company_filter: str | None = None) -> AnalyzeResult:
    if not cfg.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set in .env")

    client = OpenAI(api_key=cfg.openai_api_key, timeout=LLM_TIMEOUT_S)
    conn = db.connect(cfg.db_path)
    try:
        rows = _select_pending(conn, company_filter)
        log.info("analyze: %d rows pending", len(rows))

        matchers: dict[int, NameMatcher] = {}  # company_id -> matcher
        result = AnalyzeResult(0, 0, 0, 0)

        for row in rows:
            if row["retry_count"] >= MAX_ATTEMPTS:
                result.skipped_maxed_out += 1
                continue
            cid = row["company_id"]
            if cid not in matchers:
                matchers[cid] = NameMatcher.from_db(conn, cid)
            try:
                tokens = _analyze_one(conn, client, cfg.llm_model, row, matchers[cid])
                result.analyzed += 1
                result.tokens_total += tokens
            except _Errored as exc:
                result.errored += 1
                log.warning("analyze: news_id=%s ERROR after %d attempts: %s",
                            row["id"], exc.attempts, exc.msg)
            conn.commit()  # commit per-row so progress survives crashes
        return result
    finally:
        conn.close()


# ----------------------------------------------------------- internals


def _select_pending(conn: sqlite3.Connection, company_name: str | None) -> list[sqlite3.Row]:
    sql = (
        "SELECT n.id, n.company_id, n.headline, n.body, n.retry_count "
        "FROM news n JOIN companies c ON c.id = n.company_id "
        "WHERE n.status = 'new' "
    )
    params: tuple = ()
    if company_name:
        sql += "AND c.name = ? "
        params = (company_name,)
    sql += "ORDER BY n.published_at ASC"
    return list(conn.execute(sql, params))


class _Errored(Exception):
    def __init__(self, msg: str, attempts: int) -> None:
        super().__init__(msg)
        self.msg = msg
        self.attempts = attempts


def _analyze_one(
    conn: sqlite3.Connection,
    client: OpenAI,
    model: str,
    row: sqlite3.Row,
    matcher: NameMatcher,
) -> int:
    headline = row["headline"]
    body = (row["body"] or "")[:MAX_BODY_CHARS]
    news_id = row["id"]
    start_attempt = int(row["retry_count"] or 0)

    # If body is too short to extract signal, skip the LLM call entirely.
    # Wasted tokens on a one-line press release don't produce useful mood.
    if len(body.strip()) < MIN_BODY_CHARS:
        log.info("analyze: news_id=%d body<%d chars — marking neutral without LLM",
                 news_id, MIN_BODY_CHARS)
        matches = matcher.match(headline)
        conn.execute(
            "UPDATE news SET status='analyzed', mood='neutral', "
            "mood_reason='body too short for LLM analysis', "
            "retry_count=?, tokens_used=0, error_msg=NULL WHERE id=?",
            (start_attempt, news_id),
        )
        for person in matches:
            conn.execute(
                "INSERT OR IGNORE INTO news_persons (news_id, person_id) VALUES (?, ?)",
                (news_id, person.id),
            )
        return 0

    attempts_used = 0
    try:
        for attempt in Retrying(
            stop=stop_after_attempt(MAX_ATTEMPTS - start_attempt),
            wait=wait_exponential(multiplier=2, min=2, max=30),
            retry=retry_if_exception_type(
                (RateLimitError, APIConnectionError, APITimeoutError)
            ),
            reraise=True,
        ):
            with attempt:
                attempts_used = attempt.retry_state.attempt_number
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": f"Заголовок: {headline}\n\nТекст:\n{body}"},
                    ],
                    response_format={"type": "json_object"},
                )
    except (RateLimitError, APIConnectionError, APITimeoutError) as exc:
        total_attempts = start_attempt + attempts_used
        # Network errors are retriable: only flip to 'error' once we hit MAX_ATTEMPTS.
        terminal = total_attempts >= MAX_ATTEMPTS
        _mark_error(conn, news_id, total_attempts,
                    f"network: {type(exc).__name__}", terminal=terminal)
        raise _Errored(str(exc), total_attempts) from exc
    except APIError as exc:
        # Other API errors (e.g. auth, bad request) are not transient — terminate.
        total_attempts = start_attempt + attempts_used
        log.warning("analyze: news_id=%d API error: %s", news_id, exc)
        _mark_error(conn, news_id, total_attempts,
                    f"api: {type(exc).__name__}", terminal=True)
        raise _Errored(str(exc), total_attempts) from exc

    # Parse the LLM response
    try:
        content = response.choices[0].message.content or ""
        parsed = json.loads(content)
        mood = parsed["mood"]
        if mood not in VALID_MOODS:
            raise ValueError(f"invalid mood: {mood!r}")
        mood_reason = str(parsed.get("mood_reason") or "")[:500]
    except Exception as exc:
        # Parse errors are terminal: retrying won't make the LLM return a
        # different shape for the same prompt.
        total_attempts = start_attempt + attempts_used
        log.warning("analyze: news_id=%d parse error: %s", news_id, exc)
        _mark_error(conn, news_id, total_attempts,
                    f"parse: {type(exc).__name__}", terminal=True)
        raise _Errored(str(exc), total_attempts) from exc

    tokens = (response.usage.prompt_tokens + response.usage.completion_tokens) if response.usage else 0

    # Match seed persons against the article text.
    matches = matcher.match(f"{headline}\n{body}")

    conn.execute(
        "UPDATE news SET status='analyzed', mood=?, mood_reason=?, "
        "retry_count=?, tokens_used=?, error_msg=NULL WHERE id=?",
        (mood, mood_reason, start_attempt + attempts_used, tokens, news_id),
    )
    for person in matches:
        conn.execute(
            "INSERT OR IGNORE INTO news_persons (news_id, person_id) VALUES (?, ?)",
            (news_id, person.id),
        )
    log.info(
        "analyze: news_id=%d mood=%s tokens=%d persons=%d",
        news_id, mood, tokens, len(matches),
    )
    return tokens


def _mark_error(
    conn: sqlite3.Connection, news_id: int, attempts: int, msg: str,
    *, terminal: bool,
) -> None:
    """Persist an error. ``terminal=True`` flips status to 'error' (won't retry).
    Otherwise stays 'new' and a future ``analyze`` run may pick it up again
    provided ``retry_count < MAX_ATTEMPTS``."""
    final_status = "error" if terminal else "new"
    conn.execute(
        "UPDATE news SET status=?, retry_count=?, error_msg=? WHERE id=?",
        (final_status, attempts, msg[:1000], news_id),
    )
