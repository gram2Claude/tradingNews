# Plan 08 — δ-completion task 06 — v1

Автор: Claude
Дата: 2026-05-25
Ветка: `delta_completion_06` (от master @ `024028c`)
Спек: `01_specs/08_delta_completion_spec.md` (P1-P5 pre-filled defaults)

---

## T-фазы

### T1 — db.py migration v3 → v4 (~1ч)

**Изменения:**
- `SCHEMA_VERSION = 4`
- `SCHEMA_SQL.news` — удалить колонку `item_type` (для fresh БД)
- `_migrate_to_v4(conn)`:
  1. `SAVEPOINT v4_migration`
  2. Перенести news.item_type='recommendation' → recommendations table (с NULL structural fields, сохраняя mood/mood_reason/status/url/headline)
  3. Перенести junction строки `news_persons` → `recommendation_persons` через JOIN по `(source_id, url)`
  4. `DELETE FROM news WHERE item_type='recommendation'` (CASCADE удалит остатки news_persons)
  5. Rebuild news table без item_type column (SQLite < 3.35 не имеет DROP COLUMN):
     - `CREATE TABLE news_new (...)` без item_type
     - `INSERT INTO news_new SELECT (все колонки кроме item_type) FROM news`
     - `DROP TABLE news`
     - `ALTER TABLE news_new RENAME TO news`
     - пересоздать indices
  6. `PRAGMA user_version = 4`
  7. `RELEASE v4_migration`
- `ensure_migrated`: добавить ветку `if user_version < 4: _migrate_to_v4(conn)`

**Acceptance:**
- Fresh БД (`init-db` с нуля): `PRAGMA table_info(news)` не содержит `item_type`
- Существующая БД с finam-recs (создать через fixture): после `ensure_migrated`:
  - `news` не имеет column `item_type`
  - `recommendations` содержит N мигрированных строк с `target_price=NULL`
  - `recommendation_persons` содержит соответствующие person-links
- Migration идемпотентна — повторный вызов no-op
- Crash mid-migration (моковая) → user_version остаётся 3, состояние не повреждено

---

### T2 — analyzer.py per-item dispatch (~1.5ч)

**Изменения:**
- `SYSTEM_PROMPT_NEWS` остаётся как есть (LLM продолжает возвращать `item_type`)
- В `_analyze_news` ПОСЛЕ парса `mood, mood_reason, item_type`:
  - Если `item_type == 'news'`: UPDATE news (без item_type column), INSERT news_persons как сейчас
  - Если `item_type == 'recommendation'`: per-item dispatch:
    ```
    SAVEPOINT per_item_dispatch
      INSERT INTO recommendations (
        company_id, source_id, url, headline, body, published_at,
        fetched_at, mood, mood_reason, status='analyzed',
        retry_count, tokens_used, error_msg=NULL,
        target_price=NULL, recommendation_action=NULL,
        potential_pct=NULL, multipliers_json=NULL
      ) SELECT ... FROM news WHERE id=?
      ON CONFLICT(source_id, url) DO UPDATE SET
        mood=excluded.mood, mood_reason=excluded.mood_reason,
        status='analyzed', retry_count=excluded.retry_count,
        tokens_used=excluded.tokens_used
      RETURNING id
      → rec_id

      INSERT OR IGNORE INTO recommendation_persons (recommendation_id, person_id)
      SELECT ?, person_id FROM news_persons WHERE news_id=?

      INSERT OR IGNORE INTO recommendation_persons VALUES (?, ?)
        for each person in matches  (новые матчи)

      DELETE FROM news WHERE id=?  -- cascade удалит news_persons
    RELEASE per_item_dispatch
    ```
- `_select_pending_news` остаётся
- Update body comment про γ-стратегию → δ-completion

**Acceptance:**
- Test: insert news row, mock LLM возвращает `item_type='news'` → row остаётся в news.
- Test: insert news row, mock LLM возвращает `item_type='recommendation'` → строка в recommendations.id=X, news_persons (если были) → recommendation_persons, исходная news row удалена.
- Test: cross-table move атомарен — если INSERT recommendations кидает ConflictError на дубликате, savepoint rollback'ит, news row остаётся `status='new'` с error_msg
- Test: row с уже существующим (source_id,url) в recommendations (повторный analyze) — UPSERT через ON CONFLICT, не упасть
- Body comment обновлён (γ → δ)

---

### T3 — reporter.py упрощение (~1ч)

**Изменения:**
- Удалить UNION ALL в `_report_company` — заменить на два независимых SELECT (news + recommendations) и склейку в Python (либо два цикла)
- Альтернатива: один SELECT с двумя CTE — но Python проще для tie-break логики
- `news` query: только SELECT FROM news WHERE status='analyzed' — все строки идут в news/ папку
- `recommendations` query: SELECT FROM recommendations WHERE status='analyzed' — все в recommendations/
- Убрать колонку `item_type` из `XLSX_COLUMNS_NEWS` (теперь все news == news)
- Frontmatter `_write_md` — убрать `item_type` ключ (или оставить с default='news'/'recommendation' для backward-compat output чтения)
- `_write_persons_csv` CTE: UNION остаётся (junctions всё ещё две таблицы), но упрощается логика name lookups

**Decision на CTE persons.csv:** UNION над news_persons + recommendation_persons остаётся — это естественно: персона может быть упомянута и в news, и в рекомендациях. Удалить нельзя.

**Acceptance:**
- Существующие тесты test_reporter.py продолжают работать (с поправкой на отсутствие item_type в XLSX header)
- `data.xlsx` всё ещё имеет два листа: news (теперь без item_type column) + recommendations
- output/X5/news/ содержит только news (не finam-recs из старого item_type)
- output/X5/recommendations/ содержит lmsic + finam-recs (мигрировавшие из news → recommendations)

---

### T4 — cloud_sync (~30м)

**Изменения:**
- `schema.sql`: удалить `item_type TEXT NOT NULL DEFAULT 'news' CHECK(item_type IN ...)` из `trading_news.news`
- `pusher.py:_push_news`: убрать item_type из SELECT и INSERT (8 строк правок)
- README cloud section: упомянуть что после миграции нужен `python -m src init-cloud-db` чтобы убрать колонку из Postgres

**Acceptance:**
- Existing test_cloud_sync.py продолжает работать (с обновлёнными mock'ами)
- Schema.sql idempotent (CREATE TABLE IF NOT EXISTS — на live Postgres не сломает)
- В реальном Supabase удаление колонки требует ручной ALTER TABLE — задокументировано в README

---

### T5 — Tests update + new (~1.5ч)

**Существующие тесты — обновить:**
- `test_db_migrations.py` — добавить test `test_migrate_v3_to_v4_finam_recs`
- `test_analyzer.py` — обновить `UPDATE news` expectations (нет item_type column)
- `test_analyzer_recommendations.py` — без изменений (recommendations path не трогаем кроме комментариев)
- `test_reporter.py` — XLSX header без item_type; news/recommendations sheet separation
- `test_reporter_recommendations.py` — без изменений
- `test_cloud_sync.py` / `test_cloud_sync_recommendations.py` — без item_type в news mock
- `test_fetcher_dispatcher.py` / `test_fetcher_insert.py` — news INSERT без item_type column

**Новые тесты:**
- `test_db_migrations.py::test_migrate_v3_to_v4_clean_fresh_db` — fresh БД, no-op
- `test_db_migrations.py::test_migrate_v3_to_v4_with_legacy_recs` — pre-seed news+recs, migration moves correctly
- `test_db_migrations.py::test_migrate_v3_to_v4_preserves_news_persons_for_news_rows` — junction unaffected for non-rec rows
- `test_analyzer.py::test_dispatch_news_to_news_table` — LLM='news' → row stays
- `test_analyzer.py::test_dispatch_recommendation_moves_to_recs_table` — LLM='recommendation' → row migrates
- `test_analyzer.py::test_dispatch_persons_follow_to_rec_table` — news_persons → recommendation_persons

**Acceptance:** ~215 + 6 new = ~221+ tests, все зелёные.

---

### T6 — Health + smoke (~30м)

**Acceptance:**
- `pytest tests/ -q`: all green
- `ruff check src/ tests/`: clean
- `mypy src/ --ignore-missing-imports`: clean
- Live: `python -m src cycle --company X5` exit 0
- SQL verify:
  ```sql
  SELECT 'news has item_type' as msg WHERE EXISTS (
    SELECT 1 FROM pragma_table_info('news') WHERE name='item_type'
  );  -- should be empty
  SELECT COUNT(*) FROM recommendations;  -- ≥ legacy finam-recs count
  ```

---

## Out of scope

- Backup БД (пользователь сам)
- Postgres ALTER TABLE DROP COLUMN (ручная команда)
- Performance: cross-table move медленнее одного UPDATE, но 1 строка / запрос — не bottleneck

---

## Оценка времени

| T | Время |
|---|---|
| T1 db migration | 1ч |
| T2 analyzer dispatch | 1.5ч |
| T3 reporter simplify | 1ч |
| T4 cloud_sync | 0.5ч |
| T5 tests | 1.5ч |
| T6 health + smoke | 0.5ч |
| **Итого** | **~6ч** |

Плюс pre-ship гейты ~1ч.

---

## Acceptance целиком

- ✅ SCHEMA_VERSION = 4
- ✅ news.item_type column удалён
- ✅ Legacy finam-recs мигрированы в recommendations (target_price=NULL)
- ✅ junction news_persons → recommendation_persons мигрированы
- ✅ analyzer per-item dispatch: news → news table, recommendation → recommendations table
- ✅ analyzer cross-table move атомарен (SAVEPOINT)
- ✅ reporter: 0 UNION ALL над таблицами (один query per таблица)
- ✅ data.xlsx: news лист без item_type column; recommendations лист как раньше
- ✅ persons.csv CTE остаётся (junctions всё ещё две)
- ✅ Postgres schema.sql без item_type; pusher.py без item_type
- ✅ ~221+ тестов green; ruff/mypy clean
- ✅ Live smoke: cycle exit 0
