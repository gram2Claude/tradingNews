# 04 · finam · pre-ship review (codex)

**Ветка:** `finam_news`
**Модель:** codex-cli 0.130.0 (`codex review --base master`)
**Дата:** 2026-05-22
**Вызов:** non-interactive review против master без custom prompt'а.

---

## Сводка по находкам codex

Codex выдал 2 находки (одна — P1-форма, на деле false-positive по форме но валидное напоминание; вторая — реальный P1).

### Находка 1 (codex P1) — finam.py не добавлен в трекинг

> The patch wires in a Finam source that is not part of the tracked diff, which breaks fetch/cycle imports. `src/sources/finam.py` is not added, but `src.fetcher` now imports it at module import time.

**Анализ.** Codex прав по форме: `git diff master` действительно не показывает `src/sources/finam.py`, потому что файл **untracked** (вместе с `playwright_base.py`, всеми новыми тестами, `04_finam_*`, `pytest.ini`, `tools/`).

```
?? src/sources/finam.py
?? src/sources/playwright_base.py
?? tests/test_finam.py
?? tests/test_db_migrations.py
?? tests/test_playwright_base.py
?? pytest.ini
... (полный список см. git status)
```

На диске файл существует — все 102 теста прогоняются. Импорт работает локально. Но **перед коммитом** untracked файлы должны быть в `git add`, иначе после push'а ветка действительно сломается.

**Severity:** не блокер для review (false positive в смысле «код не работает»), но **обязательный pre-commit checklist item**.

**Action:** `git add` всех untracked файлов из списка выше **перед `git commit`**. На этапе `/ship` это будет частью команды стадирования — Claude явно перечислит файлы, не делая `git add -A`.

---

### Находка 2 (codex P1) — миграция v1→v2 не выполняется на штатных командах

> On an existing v1 database, the normal `cycle`/`analyze` path does not call `init_db`, so this new `item_type` update will hit `sqlite3.OperationalError: no such column: item_type` unless the user manually reruns `init-db` first.

**Анализ.** Подтверждено grep'ом:

```
src/cli.py:31:def cmd_init_db(args):
src/cli.py:33:    counts = db.init_db(cfg)        # ← единственный call site
src/cli.py:142:p_init.set_defaults(func=cmd_init_db)
```

`db.init_db` вызывается **только** из `cmd_init_db` (CLI `python -m src init-db`). `cmd_fetch`, `cmd_analyze`, `cmd_report`, `cmd_cycle` не вызывают ни `init_db`, ни `_migrate_to_v2`.

**Сценарий поломки:**
1. Пользователь pull'ит ветку `finam_news` на машину, где уже есть `data/db.sqlite` (v1 PRAGMA user_version=1).
2. Запускает `python -m src cycle` (или `analyze`).
3. Fetcher отрабатывает, складывает строки в `news` со старой схемой (без `item_type` — INSERT не упадёт, потому что INSERT не указывает item_type явно, а в v1 колонки нет, поэтому SQL писатель тоже её не пишет).
4. Analyzer доходит до строки `UPDATE news SET ... item_type=? ...` → **`sqlite3.OperationalError: no such column: item_type`**.
5. На этой row'е analyzer crash, статус остаётся `new`, retry_count не повышается (т.к. это не transient/parse error — это unhandled). На повторе — то же самое. Цикл застывает.

Локально у нас всё прошло потому что пользователь после реализации T8.7 ручками сделал `python -m src init-db` (это упомянуто в summary прошлой сессии). После merge и pull'а на другой машине / в свежем checkout'е этого шага нет.

**Severity:** **P1 — блокер** для landing. Это та самая «работает у меня на машине» категория ошибок.

---

## Решение по находке 2 — рекомендация

Три варианта, в порядке возрастания инвазивности:

**Вариант A (minimal, рекомендую):** Вызывать `_migrate_to_v2` (или весь `init_db`) в начале `cmd_analyze` / `cmd_cycle` / `cmd_fetch` / `cmd_report`. Миграция уже идемпотентная (`PRAGMA user_version` + column-presence check), повторный вызов на v2 БД — no-op (~1мс). Самый безопасный и явный fix.

```python
# src/cli.py — в каждой cmd_* функции, перед основной логикой:
def cmd_cycle(args):
    cfg = load_config()
    db.ensure_migrated(cfg)   # ← новая публичная обёртка над migration check
    # ... rest of cycle logic
```

Добавить `db.ensure_migrated(cfg)` — тонкая обёртка, которая делает только PRAGMA-check и при необходимости вызывает `_migrate_to_v2` без выполнения SCHEMA_SQL / seed import. Цена — ~10 строк.

**Вариант B (документация):** Обновить README + CLAUDE.md секцию «Daily use», явно прописать «после pull'а с миграцией — `python -m src init-db`». Хрупко (легко забыть), но без code change.

**Вариант C (init-on-connect):** Поместить migration внутрь `db.connect()`. Слишком инвазивно — `connect` зовётся в hot path, плюс затрагивает поведение тестов. Не рекомендую.

**Моя рекомендация:** **Вариант A**. Это 10 строк, нулевой риск регрессии (миграция уже доказала идемпотентность в `tests/test_db_migrations.py::test_migration_idempotent`), решает проблему окончательно. Спросить пользователя перед фиксом, чтобы он явно одобрил.

---

## Что codex НЕ покрыл (но я добавлю как cross-reference с claude-review)

Из моего предыдущего review (`reviews/04_claude_finam_rew.md`):
- **P2.1** — silent fallback `item_type` на 'news' при отсутствии поля в LLM-ответе. Codex не дошёл до этого, его обзор остановился на двух блокерах. Я считаю это самостоятельной находкой.
- **P2.2** — verify не на главной finam.ru; **P2.3** — regex balancing в `_extract_body`. Менее критичны, но codex их не отметил.

---

## Итог codex review

| Severity | Кол-во | Что |
| --- | --- | --- |
| **P1** | **1** | миграция v1→v2 не выполняется на штатных командах → crash на v1 БД |
| P2 (informational pre-commit) | 1 | untracked finam.py — нужен git add перед коммитом |

**Gate:** **FAIL** (codex). До устранения P1 нельзя пушить.

**Рекомендуемое действие:**
1. Fix P1 (вариант A: `db.ensure_migrated()` в начале каждой cmd_*).
2. Перед `git commit` добавить все untracked файлы (codex P2).
3. Перезапустить `pytest tests/ -q` + `python -m ruff check src/ tests/` + `python -m mypy src/ --ignore-missing-imports`.
4. После — продолжить `/cso` → `/health` → PR.

Я остановлю pipeline на этом этапе и спрошу пользователя, делать ли fix варианта A прямо сейчас, или принимать как known и идти в `/cso` дальше.

— Claude (передача codex output)
