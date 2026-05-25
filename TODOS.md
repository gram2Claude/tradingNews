# TODOS

Issues deferred from reviews — not blocking current ship, but worth tracking.

---

## Open

### δ-completion: устранить дуальный read-path для recommendations
*Источник: spec/plan 06 `recommendations_split` (γ-стратегия, осознанный техдолг).*
*Открыто: 2026-05-25 (после merge PR #4).*

После задачи 06 рекомендации хранятся в двух источниках:
- `news WHERE item_type='recommendation'` — legacy finam-recs (существующие данные)
- `recommendations` — новая таблица, заполняется recommendation-only источниками (lmsic из задачи 07)

Reporter, persons.csv, data.xlsx делают UNION над обоими. См.
`02_plans/06_claude_recommendations_split_plan.md` секция «Locations to
remember during γ» — там перечислены все места.

**Scope δ-completion (~1 день):**
- Migration v3 → v4: `INSERT INTO recommendations SELECT ... FROM news WHERE item_type='recommendation'; DELETE FROM news WHERE item_type='recommendation'; ALTER TABLE news DROP COLUMN item_type;`
- analyzer: убрать SYSTEM_PROMPT_NEWS секцию про item_type классификацию (либо сделать per-item dispatcher если finam останется mixed-stream)
- reporter: UNION → single SELECT FROM recommendations
- persons.csv: убрать UNION в CTE
- data.xlsx: больше нет смешанной семантики

**Trigger:** finam recommendation accuracy становится бизнес-критичным,
ИЛИ появляется второй mixed-stream источник (где LLM-классификация
item_type снова нужна), ИЛИ разработчик устал поддерживать дуальный
read-path при правках reporter'а.

---

## Done

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
