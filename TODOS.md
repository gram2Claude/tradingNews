# TODOS

Issues deferred from reviews — not blocking current ship, but worth tracking.

---

## Open

(нет открытых пунктов)

---

## Done

### δ-completion: устранение дуального read-path для recommendations (задача 08)
*Источник: spec/plan 06 `recommendations_split` (γ-стратегия, осознанный техдолг).*
*Открыто: 2026-05-25 (после merge PR #4) / Закрыто: 2026-05-25.*

Реализовано:
- Migration v3 → v4 в `_migrate_to_v4`: INSERT INTO recommendations SELECT
  FROM news WHERE item_type='recommendation' (с NULL structural fields);
  миграция junction `news_persons` → `recommendation_persons`; DELETE
  мигрированных rows; SQLite-rebuild news table без колонки item_type
  (CREATE news_new + INSERT + DROP + RENAME — обход отсутствия DROP
  COLUMN в SQLite < 3.35). Атомарно через SAVEPOINT.
- analyzer **per-item dispatch**: SYSTEM_PROMPT_NEWS продолжает
  классифицировать item_type, но при `item_type='recommendation'` строка
  переезжает в recommendations через `_dispatch_news_to_recommendations`
  (атомарно SAVEPOINT, UPSERT по `(source_id, url)` для повторных
  анализов). Persons-junction следует за строкой.
- reporter: убран UNION ALL, два независимых SELECT (news + recommendations),
  склейка в Python. `_src_table` derived в Python (не из SQL).
- XLSX news header: убрана колонка `item_type` (XLSX_COLUMNS_NEWS теперь
  `[date, headline, persons, mood]`).
- `persons.csv` CTE с UNION ALL остаётся — junctions всё ещё две таблицы.
- Postgres schema.sql: убран `item_type` из `trading_news.news`. На live
  Postgres требует ручного `ALTER TABLE trading_news.news DROP COLUMN
  IF EXISTS item_type` либо `python -m src init-cloud-db` (idempotent CREATE
  IF NOT EXISTS не дроп'ает существующие колонки).
- pusher.py: SELECT и INSERT для news без item_type.

Тесты: 242 passed (217 baseline + 25 новых/обновлённых на δ-completion).
Live migration на проде: 2 finam-recs мигрировали из news (21→19) в
recommendations (1→3), user_version=3→4, колонка item_type удалена. Backup
`data/db.sqlite.before_v4` сохранён.

### RBC + e_disclosure окончательно архивированы (задача 08)

### init-db не синхронизирует `sources.enabled` с конфигом — STALE
*Источник: `work_directory/04_reviews/02_claude_rbc_news_rew.md` P3 #4 (2026-05-21).*
*Закрыто: 2026-05-23.*

Проверено: `src/db.py:99-104` уже делает
`ON CONFLICT(code) DO UPDATE SET enabled=excluded.enabled` — sync работает,
требовалось просто перезапустить `init-db`. Колонка не deprecated: её читает
`src/cloud_sync/pusher.py:133` и шлёт в Supabase для дашборда. Никаких изменений
в коде не нужно.

### finam body — потерянные параграфы
*Источник: чистка body 2026-05-22.*
*Закрыто: 2026-05-23.*

В `src/sources/finam.py:_extract_body` заменён `separator=" "` на `separator="\n"`
для child-нод whitelist'а. `_clean_text` сжимает лишние переносы. На fixture
`finam_article_news.html` body теперь 4 строки вместо одной. Все 141 тест зелёные.

### finam fetch — audit slug-фильтра
*Источник: smoke run 2026-05-22 — relevance_filtered=45 of 68 listing_hits.*
*Закрыто: 2026-05-23.*

Добавлен INFO-log с полным списком rejected slug'ов в `FinamSource.fetch`. Аудит
fixture'а вскрыл **3 чистых false-negative** (все три про X5):
- `kh5-pokupaet-krupnogo-distribyutora-produktov`
- `kh5-s-bolshoy-veroyatnostyu-zaplatit-dividendy...`
- `sovokupnyy-dividend-kh5-za-2025-god...`

`Х5` finam транслитерирует как `kh5` (BSI/GOST), в whitelist'е было только
`x5 / iks-5 / iks5`. Добавлены `kh5` и `riteyl` (граничный кейс — `riteylom`).
После фикса: 27 kept / 42 rejected (было 24/45). Все 141 тест зелёные.
