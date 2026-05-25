# Checkpoint 06 — recommendations_split — все T-фазы завершены

Дата начала: 2026-05-23
Дата завершения: 2026-05-25
Ветка: `recommendations_split` (от master, ещё не закоммичена)
Прогресс: **8 из 8 T-фаз** ✅ READY FOR PRE-SHIP REVIEW
Тесты: **177/177 зелёных** (было 141, +36 новых)
Health stack: ✅ pytest + ruff + mypy все зелёные
Diff vs master: +1152 / −249 строк в 12 файлах

## Следующие шаги
1. `/review` (Claude self-review)
2. `/codex review`
3. `/cso` security audit
4. `/health` composite ≥ 9/10
5. User делает commit + push + PR
6. После merge: запись в TODOS.md про δ-completion

---

## Как продолжить в новой сессии

Скажи Claude'у:
> «Продолжаем задачу 06 (recommendations_split) с T5. Прочитай
> `work_directory/06_progress_checkpoint.md` и
> `work_directory/02_plans/06_claude_recommendations_split_plan.md`,
> затем стартуй T5.»

Claude:
1. Прочитает чекпоинт + план v2
2. Проверит git ветку (`recommendations_split`) и текущий state тестов
3. Стартует T5 (Reporter UNION dual-source)

---

## Что готово (T1-T4)

### T1 — БД миграция v3 ✅
- `src/db.py`:
  - `SCHEMA_VERSION = 3`
  - `SCHEMA_SQL` добавлены таблицы `recommendations` + `recommendation_persons` со всеми индексами
  - `_migrate_to_v3(conn)` — транзакционная через `SAVEPOINT v3_migration` (Python sqlite3 в legacy mode делает implicit commit перед DDL — `with conn:` не работает для CREATE TABLE, savepoint работает)
  - `ensure_migrated` — цепочка v1→v2→v3
- `tests/test_db_migrations.py` — переписан под v3, +тесты:
  - `test_migrate_v1_to_v3`, `test_migrate_v2_to_v3`, `test_partial_v3_recovery`, `test_migrate_to_v3_transactional_rollback`
  - 10/10 зелёных

### T7-smoke — CLI status breakdown ✅
- `src/db.py:status_counts` — теперь UNION ALL news + recommendations с колонкой `kind`
- `src/cli.py:cmd_status` — печатает 4-колоночную таблицу
- `tests/test_status_counts.py` — новый файл, 4/4 зелёных

### T2 — Source ABC + ItemDestination + RawItem ✅
- `src/sources/base.py`:
  - `ItemDestination(Enum)` — NEWS, RECOMMENDATIONS
  - `Source.item_destination: ItemDestination = ItemDestination.NEWS` (class attr, default)
  - `RawItem` расширен полями `target_price`, `recommendation_action`, `potential_pct`, `multipliers_json` (все default None) — **контракт зафиксирован сейчас, чтобы lmsic в задаче 07 просто заполнял**
- Существующие source'ы (x5_ir, finam, rbc) — без изменений (наследуют NEWS)

### T3 — Fetcher dispatcher ✅
- `src/fetcher.py`:
  - `_insert_into_news(...)` — старая логика
  - `_insert_into_recommendations(...)` — новый INSERT, пишет structural fields из RawItem
  - `_insert(...)` — dispatcher по `destination`, raises `ValueError` на unknown
  - `destination: ItemDestination = ItemDestination.NEWS` default (backward-compat для существующих тестов test_fetcher_insert.py)
- `tests/test_fetcher_dispatcher.py` — 6/6 новых тестов

### T4 — Analyzer два path'а ✅
- `src/analyzer.py` — переписан целиком:
  - **Два SYSTEM_PROMPT'а:** SYSTEM_PROMPT_NEWS (с item_type) + SYSTEM_PROMPT_RECOMMENDATION (без item_type)
  - **Два хелпера:** `_analyze_news` / `_analyze_recommendation` (hardcoded имена таблиц — codex P1.2 satisfied)
  - **Два error-marker'а:** `_mark_news_error` / `_mark_recommendation_error`
  - **Persons-link диспатч:** news → `news_persons`, recs → `recommendation_persons`
  - **`AnalyzeResult` расширен:** `news_analyzed`, `news_errored`, `recommendations_analyzed`, `recommendations_errored`, `aborted_during` (`'news' | 'recommendations'`)
  - **Backward-compat aliases** в конце файла: `_analyze_one`, `_mark_error`, `_select_pending`, `SYSTEM_PROMPT` — старые тесты их используют
- `tests/test_analyzer_recommendations.py` — 8/8 новых: happy path, prompt validation, item_type tolerance, persons link, global config error semantics, transient retry, parse error, empty table

---

## Что осталось (T5, T6, T8)

### T5 — Reporter UNION dual-source + tie-break + xlsx (~3.5 ч)
- `src/reporter.py`:
  - UNION query для recommendations-папки: `news WHERE item_type='recommendation'` + `recommendations`
  - ORDER BY с tie-break: `published_at ASC, _src_table ASC, source_code ASC, url ASC`
  - News-папка теперь явно фильтрует `news WHERE item_type='news'` (behavior change — раньше там были все news включая finam-recs)
  - YAML frontmatter — NULL-keys отсутствуют (не `target_price: None`)
  - `data.xlsx` — два листа: `news` (только news.item_type='news') + `recommendations` (UNION с extra колонками)
  - `persons.csv` — count по UNION обеих таблиц
- Тесты:
  - existing test_reporter.py — почти все зелёные, исключение `test_reporter_xlsx_sheets` (намеренный change под 2 листа)
  - новый: `test_reporter_recommendations_tiebreak` (два item'а с одинаковым `published_at`, deterministic order)
  - новый: `test_reporter_null_handling` (target=NULL → ключ отсутствует в frontmatter)

### T6 — Cloud sync расширение (~2.5 ч)
- `src/cloud_sync/schema.sql`:
  - +`trading_news.recommendations` (natural PK `(source_code, url)`, FK на sources + companies)
  - +`trading_news.recommendation_persons` (PK зеркалит news_persons: `(source_code, url, company_name, person_full_name)`)
- `src/cloud_sync/pusher.py`:
  - Явный порядок в `push_all`: companies → sources → persons → **news** → **recommendations** → news_persons → recommendation_persons (junctions последними)
  - Одна транзакция
  - `PushStats` расширить: `recommendations_pushed`, `recommendation_persons_pushed`
  - Новые helpers `_push_recommendations` + `_push_recommendation_persons`
- Тесты:
  - `test_pusher_recommendations_call_order` (проверка порядка executemany'ев)
  - `test_pusher_recommendations_upsert`
  - `test_pusher_recommendation_persons_after_recommendations`

### T8 — Models + final pass (~1.5 ч)
- `src/models.py` — `Recommendation` dataclass
- Health stack: mypy + ruff + pytest + coverage ≥ 90%
- Smoke `cmd_cycle` с mocked deps (NOT live external) — recommendations.count == 0 после
- Manual verification (optional): live cycle на X5

---

## Известные технические детали

### SAVEPOINT vs `with conn:`

Python `sqlite3` module в legacy isolation mode (default) делает implicit COMMIT
перед DDL statement. Это значит `with conn: conn.execute("CREATE TABLE ...")`
**не откатит** CREATE при exception — он уже закоммичен.

Решение в `_migrate_to_v3`: использовать SAVEPOINT/RELEASE/ROLLBACK TO явно.
Они работают независимо от isolation_level. См. db.py:_migrate_to_v3.

### Default ItemDestination.NEWS в fetcher._insert

`destination` параметр имеет default = `ItemDestination.NEWS` для backward-compat
с `test_fetcher_insert.py` (10+ существующих тестов). Type safety сохраняется —
все enum checks внутри dispatcher на месте. См. fetcher.py:_insert.

### Reporter — γ-техдолг

Reporter dual-source для recommendations-папки — это `UNION ALL` двух таблиц.
Этот UNION остаётся пока не сделаем δ-completion (см. spec 06 «δ-completion»).
Все места UNION перечислены в плане v2 секция «Locations to remember during γ».

### Бэквард-совместимые aliases в analyzer.py

Старые имена (`_analyze_one`, `_mark_error`, `_select_pending`, `SYSTEM_PROMPT`)
оставлены как aliases на новые news-варианты. Тесты не правились, импортируют
старые имена. Если в будущем чистка — можно удалить, обновив тесты.

---

## Команды для проверки текущего state

```powershell
# На ветке recommendations_split:
git status
git log --oneline -5

# Прогнать тесты:
.venv\Scripts\python -m pytest tests\ -q

# Health stack отдельно:
.venv\Scripts\python -m ruff check src/ tests/
.venv\Scripts\python -m mypy src/ --ignore-missing-imports

# Посмотреть diff против master:
git diff master --stat
```

---

## Artefacts (контекст для будущей сессии)

- **Spec:** `work_directory/01_specs/06_recommendations_split_spec.md` (APPROVED)
- **Plan v2:** `work_directory/02_plans/06_claude_recommendations_split_plan.md` (APPROVED)
- **Codex estimate:** `work_directory/03_estimates/06_codex_recommendations_split_est.md` (все P1+P2 ACCEPTED)
- **Этот checkpoint:** `work_directory/06_progress_checkpoint.md`

Следующая задача после ship'а 06: `work_directory/01_specs/07_lmsic_ideas_spec.md` (BLOCKED by 06).
