# Review 06 — Codex review

Ветка: `recommendations_split` vs master
Reviewer: codex (GPT-5-codex, reasoning effort=high)
Дата: 2026-05-25

---

## Codex findings

### [P1] `cmd_status` не вызывает миграцию перед `db.status_counts()`

**Location:** `src/cli.py:172-174`

**Detail:** На существующей v2 БД после `git pull` команда `python -m src status` пойдёт в `src/db.py:status_counts()` читать таблицу `recommendations` (UNION ALL) и упадёт `sqlite3.OperationalError: no such table: recommendations`. Остальные команды (fetch/analyze/report/cycle/sync-cloud) мигрируют, status забыли.

**Fix:** добавить `db.ensure_migrated(cfg)` перед `status_counts`, плюс тест v2 DB → `cmd_status` → version=3 + tables created.

**Resolution: FIXED** ✅
- `src/cli.py:cmd_status` теперь вызывает `db.ensure_migrated(cfg)` перед `db.status_counts(cfg, args.company)`
- Новый тест `tests/test_status_counts.py::test_cmd_status_migrates_v2_db_before_querying`: создаёт v2 БД руками, запускает `cmd_status`, проверяет что после вызова `user_version=3` и обе новые таблицы созданы.
- Полный suite: 178/178 зелёных после fix'а

### [P2] Полный pytest не подтверждён в codex sandbox

**Detail:** Запуск `pytest tests/ -q` заблокирован codex policy/sandbox; codex проверил только статический diff.

**Resolution: N/A** — pytest прогоняется Claude'ом локально, 178/178 passed; ruff clean; mypy clean.

---

## Codex Verdict (initial): FAIL (P1)

## Resolution Verdict: PASS

P1 закрыт fix'ом + тестом. P2 — статическая ограниченность codex sandbox, не код-проблема.

По проверенным участкам codex явно подтвердил (verbatim):
- SQL параметризован
- `_migrate_to_v3` через SAVEPOINT откатывает DDL/user_version
- Reporter tie-break/NULL frontmatter корректны
- Analyzer dispatch/error tiers согласованы
- Cloud push order parent → junction соблюдён
- Оба SYSTEM_PROMPT содержат prompt-injection guard

---

## GATE PASS (после resolution)

Готово к `/cso` security audit.
