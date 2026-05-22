# Review 02 (Codex) — Pre-Landing Review: RBC source

Дата: 2026-05-21
Ветка: `rbc_news` (staged tree, ~+1000 / -130 vs master)
Модель: Codex CLI 0.130.0, `model_reasoning_effort="high"`
База сравнения: `master` (полный diff проекта после `git add -A`)
Связанная спека: `specs/02_rbc_news_spec.md`
Связанный план: `plans/02_claude_rbc_news_plan.md` (v3, RSS-only)
Связанная оценка: `estimates/02_codex_rbc_news_est.md`
Параллельное review (Claude): `reviews/02_claude_rbc_news_rew.md`

Итог: **2 issues (0 critical, 2 important). GATE: PASS** (оба не блокирующие, но stoit fix'ить до merge).

---

## Codex summary (verbatim)

> The RBC source mostly fits the existing pipeline, but it fails to retry a
> common transient timeout and can silently treat a broken whole RSS feed as
> a successful empty fetch. These issues can cause missed news without
> appropriate retrying or error reporting.

---

## P2 — Important

### 1. ConnectTimeout не ретраится

**Файл:** `src/sources/rbc.py:67-75` (`_is_transient_exc`)

**Codex (verbatim):**
> When the RSS request fails with a transient connect timeout, `_is_transient_exc`
> returns false because `httpx.ConnectTimeout` is not a `ConnectError`. In that case
> `_http_get` does not use the configured tenacity retries and the whole RBC fetch
> fails after a single attempt, even though this is one of the transient failures
> the source is meant to tolerate.

**Причина:** httpx иерархия исключений:
```
TimeoutException (родитель)
├── ConnectTimeout      ← мы НЕ ловим
├── ReadTimeout         ← ловим
├── WriteTimeout
└── PoolTimeout
NetworkError
├── ConnectError        ← ловим
├── ReadError           ← ловим
└── WriteError
```

`ConnectTimeout` это `TimeoutException`, не `ConnectError`. Мы перечислили `ReadTimeout`, но забыли `ConnectTimeout`. Это **тот самый класс**, который ловится в реальной сети — мы видели его в логах T6.4 при тестах с throttled RBC.

**Fix:** заменить кортеж в `_is_transient_exc` на:
```python
(_TransientHTTPError, httpx.TimeoutException, httpx.NetworkError)
```
Это покрывает все таймауты и все network-уровни ошибок одной строкой.

### 2. Whole-feed parse failure возвращает empty silently

**Файл:** `src/sources/rbc.py:175-179` (`_parse_rss`)

**Codex (verbatim):**
> If the RSS endpoint returns a 200 HTML error/interstitial page or a truncated
> XML response, this catches the feed-level parse failure and returns an empty
> iterator, so `fetch()` reports `errors=0` and silently misses all RBC news
> for that cycle. Malformed individual items can be skipped, but a malformed
> feed should propagate as a source fetch error so the pipeline alerts
> instead of looking successful.

**Причина:** текущий код:
```python
try:
    root = ET.fromstring(xml_text)
except ET.ParseError as exc:
    log.error("rbc: cannot parse RSS XML: %s", exc)
    return  # ← генератор завершается, fetch видит "ok 0 items"
```

Это смешивает два кейса:
- **Malformed item** (один `<item>` без link/pubDate) — корректно пропустить, отметить counter.
- **Malformed feed** (XML битый, или эндпоинт отдал HTML / interstitial) — это **отказ источника**, его надо пробрасывать наверх как exception.

Сейчас оба кейса выглядят как `fetched=0, errors=0`. Если RBC завтра поменяет RSS-формат — мы не заметим до ручной проверки.

**Fix:**
1. Добавить класс `_FeedParseError(Exception)` в `rbc.py`.
2. `_parse_rss` raise'нет на `ET.ParseError` или отсутствие `<channel>`, не log+return.
3. `fetch()` пропускает исключение наверх (или ловит/логирует и пере-raise) — fetcher `_fetch_one` уже имеет `except Exception` который инкрементирует `errors`.

---

## Что Claude (review-02) и Codex увидели **одинаково**

- **P2 #2 (Codex)** vs **#1 P1 (Claude — already fixed)** — оба про парсинг и фильтр-этап качества. Claude фокусировался на text matching (declensions), Codex — на error propagation. Дополняют друг друга.
- Оба не нашли SQL injection, race conditions, secrets leakage.
- Оба подтвердили: strong/weak split — принципиально правильный дизайн.

## Что Claude (review-02) увидел, Codex упустил

- P1: Russian declensions filter (Codex review был после моего фикса — соответственно проблема уже отсутствовала в diff).
- P2: defusedxml (Claude — defense-in-depth; Codex это не флагнул — возможно, считает не критичным для trusted host).
- P2: misleading test name.
- P3: init-db enable sync, .gitattributes, empty-keywords test.

## Что Codex увидел, Claude упустил

- **P2 #1 (ConnectTimeout)** — Claude этого не нашёл. Concrete bug: классы httpx exception часто путают, особенно `ConnectError` vs `ConnectTimeout`.
- **P2 #2 (whole-feed failure silent)** — Claude видел `_parse_rss` ловит ParseError и логирует, но не сделал шаг дальше: «это пропадает в logs, не в errors counter». Codex поднял на уровень pipeline semantics.

---

## Resolution

| # | Тема | Статус | Что сделано |
|---|---|---|---|
| 1 | ConnectTimeout не ретраится | **FIXED** | `_is_transient_exc` теперь использует `httpx.TimeoutException` + `httpx.NetworkError` как базовые классы — покрывает ConnectTimeout/ReadTimeout/WriteTimeout/PoolTimeout + ConnectError/ReadError/WriteError одной строкой. |
| 2 | Whole-feed parse failure silent | **FIXED** | Новый класс `FeedParseError` в `rbc.py`. `_parse_rss` raise'нет на: пустой body, broken XML (unclosed tags), отсутствие `<channel>` (валидный XML, но не RSS — типичный interstitial). Generator пробрасывает в `fetch()` → `_fetch_one` ловит `except Exception` → `errors += 1`. Pipeline теперь алертит вместо silent zero. |

**Тесты:** 59/59 passed (было 56, добавлено 3 новых на feed-level failure modes; раздробил один `test_parse_rss_handles_empty_xml` на 4 более прицельных).

- `test_parse_rss_empty_body_raises_feed_error`
- `test_parse_rss_malformed_xml_raises_feed_error`
- `test_parse_rss_no_channel_raises_feed_error` (HTML interstitial кейс)
- `test_parse_rss_empty_channel_returns_no_items` (валидный пустой канал → [])

**Lint:** ruff All checks passed. **Types:** mypy 14 files clean.
