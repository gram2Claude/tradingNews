# TODOS

Issues deferred from reviews — not blocking current ship, but worth tracking.

---

## Open

### init-db не синхронизирует `sources.enabled` с конфигом
*Источник: `work_directory/04_reviews/02_claude_rbc_news_rew.md` P3 #4 (2026-05-21).*

При повторном запуске `python -m src init-db` поле `sources.enabled` в БД не
обновляется из `config.yaml`. Наблюдаемо: в `data/db.sqlite` `sources.rbc.enabled = 0`,
хотя в YAML стоит `enabled: true`. Не блокирует pipeline — `fetcher.py:51` проверяет
`src_cfg.enabled` из YAML, не из БД. Поле в БД сейчас наследие.

**Варианты решения** (для следующей спеки):
- Убрать колонку `enabled` из таблицы `sources` (БД-схема + миграция).
- Сделать `init-db` upsert по флагу `enabled`.
- Оставить как есть, добавить комментарий в схему что поле deprecated.

Приоритет: P3, ~30 минут.

### finam body — потерянные параграфы
*Источник: чистка body 2026-05-22.*

После whitelist-извлечения через selectolax body finam-статьи приходит одной
длинной строкой — `node.text(separator=" ")` склеивает все вложенные параграфы /
brs в одну линию. Читаемость в Obsidian MD страдает (см.
`output/X5/news/2026/2026_05/2026_05_06_fnm_*.md`). Но смысловое содержание
сохранено, LLM анализирует корректно.

**Решение:** в `finam._extract_body` использовать `separator="\n"` для
`mt2x p-margin font-xl` контейнера + проредить переносы через `_clean_text`
(он уже сжимает `\n{3,}` → `\n\n`).

Приоритет: P3, ~10 минут. Косметика, не блокирует pipeline.

### finam fetch — slug `iks-5` дублирует `iks5`
*Источник: smoke run 2026-05-22 — relevance_filtered=45 of 68 listing_hits.*

В `_FINAM_RELEVANT_SLUG_PARTS` есть `iks-5`, `iks5`, `iks` варианты — это
работает, но листинг даёт 45 broad-market статей которые отсекаются. Стоит
audit'ить через лог: на каких slug'ах режется и нет ли false-negative
(пропущенных X5-новостей).

Приоритет: P3, ~30 минут.

---

## Done

(пусто)
