# 05 · supabase_sync · pre-ship review (Claude)

**Ветка:** `news_refactoring`
**Дата:** 2026-05-23 (задним числом — код уехал в `392a158` до review)
**Diff scope:** `git diff b8f6fa0..392a158` — 11 файлов, +1132 −3
**Новый код в `src/`:** `cloud_sync/__init__.py` (13), `cloud_sync/pusher.py` (255), `cloud_sync/schema.sql` (67), правки `cli.py` (+65), новые тесты `test_cloud_sync.py` (268).
**Scope check:** CLEAN — соответствует `specs/05_supabase_sync_spec.md` + `plans/05_claude_supabase_sync_plan.md`. Изоляция в `trading_news.*` схему, natural keys, one-way push, silent skip без `SUPABASE_DB_URL`, маскировка пароля в логах. Расширения чужих модулей минимальны (3 строки в `cli._setup_logging` + хук в `cmd_cycle` + 2 новые subcommand'ы).

---

## Сводка

| Severity | Кол-во | Действие |
| --- | --- | --- |
| P1 (блокер) | 0 | — |
| P2 (warning) | 3 | можно фиксить сейчас или принять как known |
| P3 (informational) | 4 | принять и оставить |

Pre-ship gate **PASS** со стороны Claude review.

---

## P2 — Warning

### P2.1 · CHECK-violation на одной строке валит весь push (confidence 9/10)

**Где:** `src/cloud_sync/pusher.py:91-105` + `schema.sql:40-45`

```sql
mood       TEXT CHECK (mood IS NULL OR mood IN ('pos','neutral','neg')),
item_type  TEXT NOT NULL DEFAULT 'news'
           CHECK (item_type IN ('news','recommendation')),
status     TEXT NOT NULL DEFAULT 'new'
           CHECK (status IN ('new','analyzed','error')),
```

**Проблема.** Postgres-схема ставит CHECK-constraints на `mood`, `item_type`, `status`, но SQLite-схема (`src/db.py:30-47`) — нет. `analyzer` валидирует whitelist'ы, но если в SQLite попадёт строка с нестандартным значением (ручная правка, миграция со старой схемой, future bug в LLM-parser'е), `executemany` в `_push_news` упадёт с CHECK-violation **и откатит весь батч транзакции** (companies + sources + persons + news + news_persons). После этого пользователь видит cycle exit 0 с WARNING в логе — push'а нет, но он не понимает почему.

**Сценарий:** mood='positive' (вместо 'pos') в одной из 5000 строк. Весь push'а нет, пока пользователь не найдёт offending row через `python -m src status` или ручным SQL'ом по SQLite.

**Fix (5 строк):** Либо
- (a) добавить такие же CHECK-constraints в SQLite-схему (`src/db.py:SCHEMA_SQL`) — fail fast на этапе analyzer'а, не cloud_sync,
- (b) логировать в `_push_news` количество строк с invalid mood/item_type/status ПЕРЕД executemany и пропускать их.

Вариант (a) — корректнее: source-of-truth для валидности — local SQLite, не Postgres-mirror. Constraints на обеих сторонах должны совпадать.

---

### P2.2 · `dst.commit()` + context-manager → двойной commit (confidence 7/10)

**Где:** `src/cloud_sync/pusher.py:78-86`

```python
with psycopg.connect(db_url, sslmode="require", connect_timeout=15) as dst:
    try:
        stats = _push_inner(src, dst, company)
        dst.commit()
    except Exception:
        dst.rollback()
        raise
```

**Проблема.** В psycopg3 `Connection.__exit__` сам делает `commit()` (no exception) или `rollback()` (exception). Текущий код делает `dst.commit()` руками, потом `__exit__` пытается коммитнуть уже-закрытую транзакцию — это no-op, но семантически избыточно. Аналогично с `dst.rollback()` + `raise` → `__exit__` снова `rollback()`.

Не баг (никакой регрессии нет), но читателя путает: "почему мы коммитим, если `with` сам коммитит?".

**Fix (2 строки):**

```python
with psycopg.connect(db_url, sslmode="require", connect_timeout=15) as dst:
    stats = _push_inner(src, dst, company)
# __exit__ сам коммитит на успех / rollback на exception
```

---

### P2.3 · `cmd_cycle` ловит `Exception`, проглатывая `KeyboardInterrupt`/`SystemExit`-производные (confidence 6/10)

**Где:** `src/cli.py:126-133`

```python
try:
    from src import cloud_sync
    stats = cloud_sync.push_all(cfg.db_path, db_url, company=args.company)
    print(f"cloud:    {stats}")
except Exception as exc:
    log.warning("cloud_sync skipped: %s: %s", type(exc).__name__, exc)
```

**Проблема.** `Exception` не ловит `KeyboardInterrupt` / `SystemExit` — это нормально. НО `psycopg.OperationalError` (network down, pooler reject, auth fail) ловится — это **дизайн**, пользователь хотел best-effort cloud push.

Тонкий момент: ловится также `MemoryError` / `RecursionError` (классы `Exception`'а в Python). Если что-то пойдёт катастрофически — мы пишем WARNING и говорим "ok, cycle exit 0". Это маскирует реальные баги в pusher'е, которые потом всплывут только при `sync-cloud --company X5` (там exception пробрасывается).

**Fix:** Сузить catch до `(psycopg.Error, OSError)` — только сетевые/БД-ошибки трактуем как best-effort, остальное пробрасываем:

```python
import psycopg
...
except (psycopg.Error, OSError) as exc:
    log.warning("cloud_sync skipped: %s: %s", type(exc).__name__, exc)
```

Тогда баг в `_push_news` (например, мисматч кол-ва колонок) даст полноценный traceback в `cycle`, а не молчаливый WARNING.

---

## P3 — Informational

### P3.1 · `init_schema` шлёт весь DDL одним `cur.execute` — гранулярность ошибки теряется (confidence 8/10)

**Где:** `src/cloud_sync/pusher.py:51-59`

```python
ddl = _SCHEMA_FILE.read_text(encoding="utf-8")
...
cur.execute(ddl)
```

psycopg3 нормально парсит multi-statement SQL, но если упадёт CREATE TABLE №3, Postgres вернёт ошибку только на этой statement, и понять "что именно сломалось" из лога не выйдет без выгрузки traceback. Это разовая команда (`init-cloud-db` запускается раз в проекте), поэтому P3, не P2.

**Если когда-то пригодится:** разбить через `sqlparse.split(ddl)` и вызывать `cur.execute(stmt)` в цикле с `log.info("applying: %s", stmt[:80])` перед каждой.

---

### P3.2 · `push_all` тянет всю таблицу `news` каждый цикл (confidence 9/10)

**Где:** `_push_news` в `pusher.py:177-221`

При 5000 строк новостей это 5000 × ~14 колонок × ~2 КБ tek text → ~140 МБ payload в один `executemany`. Supabase pooler (Transaction mode) такое съест, но cycle станет на 5-10 секунд медленнее. Сейчас в БД ~50 строк — не проблема. Через год при 5к строк — заметим.

**Если когда-то пригодится:** добавить `WHERE n.fetched_at > ?` и хранить last-pushed timestamp в `trading_news.sync_state` таблице. Дельта-push'а быстрее, но усложняет схему. Решать когда упрёмся.

---

### P3.3 · `_mask_db_url` не маскирует query-string params (confidence 5/10)

**Где:** `pusher.py:46-48`

```python
return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", url)
```

Маскирует только user:password в authority. Если кто-то когда-то начнёт класть `password` в query string (`?password=...&sslmode=require`), это утечёт в лог. Сейчас Supabase так не делает (пароль строго в authority), поэтому теоретическая дыра.

**Defence-in-depth fix:** добавить regex для query-param `password=...&`. Сейчас можно не делать — концепт risk не материален без exploit'а.

---

### P3.4 · Тест `test_cycle_silent_skips_when_env_unset` не проверяет `cmd_cycle` (confidence 8/10)

**Где:** `tests/test_cloud_sync.py:248-268`

```python
# We bypass the heavy cycle wiring; just exercise the gate logic directly.
import os
assert "SUPABASE_DB_URL" not in os.environ
# The contract is checked: when env is unset, push_all must not be called.
```

Этот тест не вызывает `cli.cmd_cycle`, а просто проверяет что `os.environ` пуст и `push_all` не был вызван (что и так очевидно — он не дергался). Получается тест-тавтология: проверяет что `MagicMock` не дергался самой проверкой. Не падает, но и не сторожит "не вызывает push_all без env var" — баг типа "забыли `if db_url:`" пройдёт мимо.

**Fix:** вызвать настоящий `cli.cmd_cycle` через `argparse.Namespace(company=None, ...)` с замоканными `fetcher.fetch_all`/`analyzer.analyze_all`/`reporter.report_all` чтобы дойти до cloud-блока. Тогда `monkeypatch.delenv("SUPABASE_DB_URL")` действительно сторожит контракт.

Объём — ~20 строк теста, оставляем P3 потому что текущий «гейт» материала ещё видно из кода (тривиальный `if db_url:`).

---

## Что зачёт

- **Изоляция в `trading_news.*` schema** — правильно. Не конфликтуем с n8n/RAG в том же проекте.
- **Natural keys** (`companies.name`, `sources.code`, `news.(source_code, url)`) — корректно. SQLite-id'шки никогда не пересекают boundary.
- **`bool(r["enabled"])` / `bool(r["from_seed"])`** — явный каст int→bool на boundary, не оставили implicit truthiness.
- **`file:...?mode=ro` + `uri=True`** в SQLite-connect — read-only гарантирован, никаких случайных writes в local DB при push'е.
- **`_mask_db_url` всюду перед log'ом** — пароль не утечёт в файл лога.
- **`psycopg` pinned to INFO** в `_setup_logging` — даже `-v` не вытащит connection string в DEBUG.
- **Silent skip без `SUPABASE_DB_URL`** — корректная opt-in семантика.
- **Single transaction на весь push** — атомарность mirror'а; partial state в Postgres невозможно.
- **Параметризованный SQL везде** — `%s` placeholder, никаких f-string'ов с user-data.
- **Тесты** покрывают: init_schema DDL, push happy-path, company-filter, rollback на exception, маскировку URL — хорошее coverage для standalone модуля.

## Решения / TODO

- P2.1 (CHECK-constraints в SQLite) → можно фиксить сейчас или зафиксировать как TODO для следующей пробы.
- P2.2 (двойной commit) → косметика, фиксить когда трогаем pusher'а в следующий раз.
- P2.3 (узкий `except`) → стоит фиксить в этой же ветке: 2 строки, реальная польза для отладки.
- P3 — оставить.

**Gate:** PASS.
