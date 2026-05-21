# TODOS

Issues deferred from reviews — not blocking current ship, but worth tracking.

---

## Open

### init-db не синхронизирует `sources.enabled` с конфигом
*Источник: `reviews/02_claude_rbc_news_rew.md` P3 #4 (2026-05-21).*

При повторном запуске `python -m src init-db` поле `sources.enabled` в БД не
обновляется из `config.yaml`. Наблюдаемо: в `data/db.sqlite` `sources.rbc.enabled = 0`,
хотя в YAML стоит `enabled: true`. Не блокирует pipeline — `fetcher.py:51` проверяет
`src_cfg.enabled` из YAML, не из БД. Поле в БД сейчас наследие.

**Варианты решения** (для следующей спеки):
- Убрать колонку `enabled` из таблицы `sources` (БД-схема + миграция).
- Сделать `init-db` upsert по флагу `enabled`.
- Оставить как есть, добавить комментарий в схему что поле deprecated.

Приоритет: P3, ~30 минут.

---

## Done

(пусто)
