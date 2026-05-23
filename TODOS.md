# TODOS

Issues deferred from reviews — not blocking current ship, but worth tracking.

---

## Open

(пусто)

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
