# Plan 06 — Отдельная таблица `recommendations` (refactor архитектуры) — v2

Автор: Claude
Дата: 2026-05-23
Версия: **v2** (после codex critique → `03_estimates/06_codex_recommendations_split_est.md`, accept all P1 + P2 by Claude)
Статус: APPROVED
Основание: `01_specs/06_recommendations_split_spec.md` (APPROVED)
Стратегия: **γ** (finam → news+item_type как раньше; новая таблица только для recommendation-only источников)

**Изменения v1 → v2 (по итогам codex critique):**
- Analyzer разделён на два хелпера с hardcoded именами таблиц + двумя SYSTEM_PROMPT'ами (P1.1, P1.2)
- Reporter UNION получил детерминированный tie-break (P1.3)
- Cloud sync — явный порядок push'а с junctions в конце (P1.4)
- `recommendation_persons` natural key — зеркалит news_persons (P1.5)
- `RawItem` расширяется сразу в задаче 06, не откладывается на 07 (P1.6)
- Миграция v3 — транзакционная, user_version ставится последним (P1.7)
- `ensure_migrated` поддерживает цепочку v1→v2→v3 (P1.8)
- T-фазы переупорядочены: T1 → T7-smoke → T2 → T3 → T4 → T5 → T6 → T8
- T8 не запускает live external cycle (P2.11)
- Все P2 fix'ы интегрированы по месту

---

## 1. Цель и scope

Подготовить инфраструктуру для recommendation-only источников (lmsic в задаче 07) без изменения текущего behaviour для x5_ir / finam / rbc:

- Новые таблицы `recommendations` + `recommendation_persons` (v3 миграция)
- Новый dispatcher в fetcher по `Source.item_destination`
- Расширение analyzer'а — два независимых path'а (news, recommendations) с двумя SYSTEM_PROMPT'ами
- Расширение reporter'а: dual-source UNION с детерминированным tie-break для recommendations-папки
- Расширение cloud_sync на новые таблицы с явным порядком push'а

**Acceptance criteria для всего PR:**
- AC-A. Существующие тесты зелёные. **Допустимые правки**: `test_status_counts` (breakdown по таблицам — намеренный change, отмечено в спеке P6), `test_reporter_xlsx_sheets` (теперь 2 листа). Все остальные — без правок.
- AC-B. `pytest`, `ruff`, `mypy`, `coverage ≥ 90%` — зелёные
- AC-C. Fresh DB, v1 БД, v2 БД, partial-v3 БД — все мигрируют в v3 без ошибок
- AC-D. Smoke `cmd_cycle` против temp DB с mocked sources / OpenAI / no Supabase — отрабатывает без ошибок
- AC-E. `python -m src init-cloud-db` идемпотентно добавляет новые таблицы в Supabase

---

## 2. Архитектурное место

```
БД (SQLite):
┌──────────────┐    ┌──────────────────────┐
│  news        │    │  recommendations     │  ← NEW
│  (existing)  │    │  + target_price       │
│  item_type   │    │  + recommendation_act │
│  ('news'|    │    │  + potential_pct      │
│   'recom.')  │    │  + multipliers_json   │
└──────────────┘    └──────────────────────┘
       │                       │
┌──────────────┐    ┌──────────────────────────┐
│ news_persons │    │ recommendation_persons   │  ← NEW
└──────────────┘    └──────────────────────────┘

Source ABC:
class Source:
    item_destination: ItemDestination = NEWS  ← NEW class attr

RawItem (расширен сразу в 06):
    + target_price: float | None = None
    + recommendation_action: str | None = None
    + potential_pct: float | None = None
    + multipliers_json: str | None = None

Fetcher:
RawItem → dispatcher(source.item_destination) → _insert_into_news | _insert_into_recommendations

Analyzer (два независимых пути):
  _analyze_news()           — LLM prompt with item_type, UPDATE news
  _analyze_recommendation() — LLM prompt без item_type, UPDATE recommendations

Reporter:
  output/X5/news/...           ← news WHERE item_type='news'
  output/X5/recommendations/.. ← UNION(news WHERE item_type='recommendation', recommendations)
                                   ORDER BY published_at, _src_table, source_code, url

cloud_sync push_all (строгий порядок, одна транзакция):
  1. companies → 2. sources → 3. persons →
  4. news → 5. recommendations →
  6. news_persons → 7. recommendation_persons
```

### Locations to remember during γ (read-paths которые обязаны помнить про две таблицы)

При δ-completion этот список — карта для refactor'а. Каждое из этих мест читает recommendations из ОБОИХ источников (news WHERE item_type='recommendation' + recommendations):

- `reporter._generate_recommendations_md` — UNION
- `reporter._write_xlsx` — sheet `recommendations` через UNION
- `reporter._write_persons_csv` — count по обеим таблицам через UNION
- `cli.cmd_status` — отдельные строки для news / recommendations breakdown
- Postgres queries для recommendations (если когда-то writes appear) — UNION на стороне читателя

---

## 3. T-фазы (новый порядок)

### T1 — БД схема + транзакционная миграция v3

**Файлы:** `src/db.py`

1. Добавить SQL для `recommendations` и `recommendation_persons` в `SCHEMA_SQL`. **7 таблиц** итого: companies, sources, news, persons, news_persons, recommendations, recommendation_persons.
2. Bump `SCHEMA_VERSION = 3`
3. Новая функция `_migrate_to_v3(conn)`:
   ```python
   def _migrate_to_v3(conn):
       # Идемпотентная транзакционная миграция v2 → v3.
       # PRAGMA user_version ставится ПОСЛЕДНИМ в той же транзакции —
       # если CREATE упадёт, версия не сдвинется, ensure_migrated починит на следующем запуске.
       with conn:  # implicit BEGIN/COMMIT
           conn.execute("CREATE TABLE IF NOT EXISTS recommendations (...)")
           conn.execute("CREATE TABLE IF NOT EXISTS recommendation_persons (...)")
           conn.execute("CREATE INDEX IF NOT EXISTS idx_recommendations_company_date ON recommendations(company_id, published_at)")
           conn.execute("CREATE INDEX IF NOT EXISTS idx_recommendations_status ON recommendations(status)")
           conn.execute("CREATE INDEX IF NOT EXISTS idx_recommendation_persons_person ON recommendation_persons(person_id)")
           conn.execute("PRAGMA user_version = 3")
   ```
4. `ensure_migrated` — цепочка v1→v2→v3:
   ```python
   def ensure_migrated(cfg):
       conn = connect(cfg.db_path)
       try:
           user_version = conn.execute("PRAGMA user_version").fetchone()[0]
           if user_version >= SCHEMA_VERSION:
               return
           if user_version < 2:
               _migrate_to_v2(conn)
               conn.execute("PRAGMA user_version = 2")
               conn.commit()
           if user_version < 3:
               _migrate_to_v3(conn)  # уже выставляет user_version=3 в транзакции
       finally:
           conn.close()
   ```
5. `init_db` — после executescript(SCHEMA_SQL) ставит `PRAGMA user_version = 3`.

**Acceptance:**
- AC1.1. Старая v2 БД мигрирует в v3 без потери данных: news count неизменен, user_version=3
- AC1.2. Fresh БД создаётся в v3 со всеми **7 таблицами**
- AC1.3. v1 БД мигрирует через v2 в v3 (`_migrate_to_v2` затем `_migrate_to_v3`)
- AC1.4. **Partial-v3** scenario: руками `CREATE TABLE recommendations` без recommendation_persons и `user_version=2` → `ensure_migrated()` корректно дочинит (CREATE IF NOT EXISTS no-op'ает существующее, добавит недостающее, выставит version=3)
- AC1.5. Двойной запуск `init_db()` идемпотентен
- AC1.6. Если внутри `_migrate_to_v3` `CREATE TABLE` упадёт (например, симулированно через connection-level mock) — `user_version` остаётся 2, partial state не возникает (откат транзакции)
- AC1.7. Все существующие тесты `test_db.py` зелёные + 4 новых: test_migrate_v1_to_v3, test_migrate_v2_to_v3, test_migrate_partial_v3, test_migrate_v3_transactional_rollback

**Время:** ~2 ч (добавился partial-v3 + rollback тест)

---

### T7-smoke — CLI status breakdown (раннее)

**Файлы:** `src/cli.py`, `src/db.py`

Переехало с конца на T1+1. Причина: status показывает что обе таблицы доступны, миграция T1 фактически работает. Это smoke check для T1, не финальная фаза.

1. `db.status_counts` — расширить:
   - Текущая реализация возвращает `[(company, status, cnt)]` для news
   - Новая: возвращает `[(company, table, status, cnt)]` где table ∈ {'news', 'recommendations'}
   - Две query, UNION ALL в Python или в SQL — на выбор; в Python проще для типизации
2. `cmd_status` в `cli.py` — печатать секцию `news:` + `recommendations:` для каждой компании
3. Обновить `test_status_counts` (acceptance — это намеренный change, в спеке-P6 явно допущен)

**Acceptance:**
- AC7.1. `python -m src status` показывает обе секции; на свежей БД recommendations: 0/0
- AC7.2. `test_status_counts` обновлён (зелёный), новый тест `test_status_counts_recommendations` добавлен
- AC7.3. Smoke: после T1 + T7 запуск `init-db` → `status` показывает recommendations:0/0 без ошибок (proof что таблица существует и selectable)

**Время:** ~0.7 ч

---

### T2 — Source ABC + ItemDestination enum

**Файлы:** `src/sources/base.py`, все `src/sources/*.py`

1. `ItemDestination(Enum)` в `base.py`: `NEWS = "news"`, `RECOMMENDATIONS = "recommendations"`
2. `Source` class attribute `item_destination: ItemDestination = ItemDestination.NEWS` (default — backward-compat)
3. **`RawItem` расширяется СРАЗУ** (P1.6 — фиксируем контракт):
   ```python
   @dataclass
   class RawItem:
       url: str
       headline: str
       body: str | None
       published_at: str
       # v2 fields (для recommendations — None для news-sources)
       target_price: float | None = None
       recommendation_action: str | None = None  # 'buy' | 'hold' | 'sell' | None
       potential_pct: float | None = None
       multipliers_json: str | None = None
   ```
4. Источники без изменений: X5IRSource, FinamSource, RBCSource — наследуют default `NEWS` и не заполняют новые RawItem поля (остаются None).

**Acceptance:**
- AC2.1. `Source.item_destination == ItemDestination.NEWS` для всех existing sources
- AC2.2. `RawItem(url="...", headline="...", body="...", published_at="...")` всё ещё работает (новые поля default None)
- AC2.3. Все `test_sources*.py` зелёные без правок

**Время:** ~0.7 ч

---

### T3 — Fetcher dispatcher + insert helpers

**Файлы:** `src/fetcher.py`

1. Текущая логика INSERT в news — вынести в `_insert_into_news(conn, item, source_id, company_id) -> int | None`
2. Новый `_insert_into_recommendations(conn, item, source_id, company_id) -> int | None`:
   - INSERT OR IGNORE INTO recommendations (company_id, source_id, url, headline, body, published_at, target_price, recommendation_action, potential_pct, multipliers_json)
   - **Поля target/action/potential/multipliers берутся из RawItem** (заполнены или None)
3. Dispatcher:
   ```python
   def _insert_raw_item(conn, item, source_id, company_id, destination: ItemDestination) -> int | None:
       if destination is ItemDestination.NEWS:
           return _insert_into_news(conn, item, source_id, company_id)
       elif destination is ItemDestination.RECOMMENDATIONS:
           return _insert_into_recommendations(conn, item, source_id, company_id)
       else:
           raise ValueError(f"unknown destination: {destination!r}")
   ```
4. В `fetch_all` — вызов с `source.item_destination`

**Acceptance:**
- AC3.1. `pytest tests/test_fetcher.py` — все зелёные без правок (existing flows работают)
- AC3.2. Новый `test_fetcher_dispatcher.py`:
  - INSERT в news через mocked Source с destination=NEWS — строка попадает в news, не в recommendations
  - INSERT в recommendations через mocked Source с destination=RECOMMENDATIONS + RawItem с target_price=3200 — строка в recommendations, target_price=3200
  - Unknown destination raises ValueError
- AC3.3. UNIQUE(source_id, url) работает в обеих таблицах; повторный INSERT — no-op
- AC3.4. RawItem с дефолтными None полями inserts в recommendations с NULL'ами

**Время:** ~2 ч

---

### T4 — Analyzer два независимых path'а

**Файлы:** `src/analyzer.py`

**Ключевое изменение v2:** НЕ параметризованный `_analyze_one(row, table)`, а **два отдельных хелпера** с hardcoded именами таблиц и **двумя SYSTEM_PROMPT'ами** (P1.1, P1.2).

1. Два SYSTEM_PROMPT'а:
   - `SYSTEM_PROMPT_NEWS` — текущий (с инструкциями про item_type классификацию)
   - `SYSTEM_PROMPT_RECOMMENDATION` — урезанная версия: убраны секции про item_type, JSON-ответ содержит только `{mood, mood_reason}`
2. Два хелпера:
   ```python
   def _analyze_news(conn, cfg, row):
       result = _call_openai(SYSTEM_PROMPT_NEWS, ...)
       # parse mood, mood_reason, item_type
       conn.execute("UPDATE news SET mood=?, mood_reason=?, item_type=?, status='analyzed', tokens_used=? WHERE id=?", ...)
       _link_persons_to_news(conn, row['id'], person_ids)

   def _analyze_recommendation(conn, cfg, row):
       result = _call_openai(SYSTEM_PROMPT_RECOMMENDATION, ...)
       # parse mood, mood_reason
       conn.execute("UPDATE recommendations SET mood=?, mood_reason=?, status='analyzed', tokens_used=? WHERE id=?", ...)
       _link_persons_to_recommendation(conn, row['id'], person_ids)
   ```
3. Два error-marker'а:
   ```python
   def _mark_news_error(conn, news_id, exc_class_name, transient: bool):
       # UPDATE news SET error_msg=?, retry_count=retry_count+1, status=? WHERE id=?

   def _mark_recommendation_error(conn, rec_id, exc_class_name, transient: bool):
       # UPDATE recommendations SET error_msg=?, retry_count=retry_count+1, status=? WHERE id=?
   ```
4. `analyze_all` — два прохода в чёткой последовательности:
   ```python
   def analyze_all(cfg) -> AnalyzeResult:
       result = AnalyzeResult(news_analyzed=0, news_errored=0,
                              recommendations_analyzed=0, recommendations_errored=0,
                              tokens_used=0)
       # Pass 1: news
       try:
           for row in conn.execute("SELECT * FROM news WHERE status='new' AND retry_count < 3"):
               _analyze_news(conn, cfg, row)
               result.news_analyzed += 1
       except _GlobalConfigError:
           log.error("global config error during news pass; recommendations pass SKIPPED")
           raise

       # Pass 2: recommendations (только если news pass прошёл без global error)
       try:
           for row in conn.execute("SELECT * FROM recommendations WHERE status='new' AND retry_count < 3"):
               _analyze_recommendation(conn, cfg, row)
               result.recommendations_analyzed += 1
       except _GlobalConfigError:
           log.error("global config error during recommendations pass; news already committed")
           raise

       return result
   ```
5. `AnalyzeResult` расширен полями `news_analyzed`, `news_errored`, `recommendations_analyzed`, `recommendations_errored` (P2.7)
6. Лог-вывод тоже разбит — отдельные строки для news / recommendations breakdown
7. **Persons-matching** для recommendations пишет в `recommendation_persons` через `_link_persons_to_recommendation` (P2.9-related)

**Semantics global config error (P2.8):**
- На news-проходе: прерывает batch до начала recs-прохода. Recommendations не трогаются.
- На recs-проходе: оставляет news-проход уже закоммиченным. Recommendations partial commit'нутся (по строкам — каждая в своей транзакции).

**Acceptance:**
- AC4.1. `pytest tests/test_analyzer.py` — все зелёные без правок (existing news flow)
- AC4.2. Новые тесты `test_analyzer_recommendations.py`:
  - INSERT тестовая строка в recommendations → analyze_all → mood/mood_reason заполнены, status='analyzed'
  - LLM response для recommendations не содержит item_type — analyzer не падает (item_type не валидируется, не парсится для recs-path'а)
  - Person из seed списка matches в body recommendation → recommendation_persons получает строку
  - Global config error на news-проходе → break до начала recs-прохода
  - Global config error на recs-проходе → news уже закоммичены, recs partial
  - Transient retry: rec падает с APITimeoutError → retry_count++, status='new' (unchanged); после 3 fails — остаётся status='new', retry_count=3
- AC4.3. На пустой recommendations таблице analyze_all отрабатывает (0 iterations в pass 2)
- AC4.4. `AnalyzeResult` содержит обе пары счётчиков

**Время:** ~3 ч (увеличилось из-за двух SYSTEM_PROMPT'ов + двух error-marker'ов)

---

### T5 — Reporter dual-source UNION с детерминированным tie-break

**Файлы:** `src/reporter.py`

1. UNION query с явным tie-break:
   ```python
   rows = conn.execute("""
       SELECT id, source_id, headline, body, published_at, mood, mood_reason,
              NULL AS target_price, NULL AS recommendation_action,
              NULL AS potential_pct, NULL AS multipliers_json,
              'news' AS _src_table
       FROM news WHERE company_id = ? AND item_type = 'recommendation'
                 AND status = 'analyzed'
       UNION ALL
       SELECT id, source_id, headline, body, published_at, mood, mood_reason,
              target_price, recommendation_action, potential_pct, multipliers_json,
              'recommendations' AS _src_table
       FROM recommendations WHERE company_id = ? AND status = 'analyzed'
       ORDER BY published_at ASC, _src_table ASC, source_code ASC, url ASC
   """, (company_id, company_id))
   ```
   (`source_code` — JOIN из sources таблицы; либо передаём source_id в ORDER и потом сортируем по code в Python — выбор по простоте)
2. NULL-обработка в YAML frontmatter:
   - Если `target_price IS NULL` — ключ `target_price:` **отсутствует** (не `target_price: None`, не `target_price:`)
   - Аналогично для action / potential_pct / multipliers_json
   - Использовать explicit `if value is not None: yaml_lines.append(f"{key}: {value}")`
3. News-папка читает только `news WHERE item_type = 'news'` (без изменений в логике, но фильтр now явно включает item_type)
4. **`data.xlsx`** — два листа:
   - Sheet 1 `news`: `WHERE item_type='news'` (раньше было все строки)
   - Sheet 2 `recommendations`: UNION с дополнительными колонками target_price, recommendation_action, potential_pct, multipliers
   - **Behavior change** для существующих пользователей xlsx: первый лист `news` теперь не содержит finam-recs. Документировать в README + commit message.
5. **`persons.csv`** — count по обеим таблицам через UNION (та же логика что reporter UNION query, но GROUP BY person)

**Acceptance:**
- AC5.1. `pytest tests/test_reporter.py` зелёные. Изменения: `test_reporter_xlsx_sheets` обновлён под 2 листа (намеренный change, в спеке-P6 допущен).
- AC5.2. Recommendations-папка для существующих finam-rec строк генерится как раньше: frontmatter, numbering, текст — все секции на месте, target/action/potential ключи отсутствуют (None)
- AC5.3. Новый тест: вставить fixture row в `recommendations` с target=3200/action='hold'/potential=12.5 → MD-карточка содержит ключи `target_price: 3200`, `recommendation_action: hold`, `potential_pct: 12.5`
- AC5.4. Новый тест `test_reporter_recommendations_tiebreak`: два item'а с одинаковым `published_at`, один из news, один из recommendations → порядок detrministic (news → recommendations по `_src_table ASC`), повторный regen даёт идентичные имена файлов
- AC5.5. Idempotent regen: wipe `output/X5/recommendations/` + повторный `report_all` → identical file set
- AC5.6. NULL-handling: рекомендация без target_price → frontmatter не содержит ключ target_price (не пустую строку, не "None")
- AC5.7. `data.xlsx` после refactor'а имеет sheet `news` (только item_type='news') + sheet `recommendations`. Порядок: news первым.

**Время:** ~3.5 ч (UNION tie-break + два листа xlsx + persons CSV UNION)

---

### T6 — Cloud sync расширение с явным порядком push'а

**Файлы:** `src/cloud_sync/schema.sql`, `src/cloud_sync/pusher.py`

1. `schema.sql` — добавить:
   ```sql
   CREATE TABLE IF NOT EXISTS trading_news.recommendations (
       source_code text NOT NULL,
       url         text NOT NULL,
       company_name text NOT NULL,
       headline    text NOT NULL,
       body        text,
       published_at timestamptz NOT NULL,
       fetched_at  timestamptz,
       mood        text,
       mood_reason text,
       target_price          double precision,
       recommendation_action text,
       potential_pct         double precision,
       multipliers_json      text,
       status      text NOT NULL DEFAULT 'new',
       error_msg   text,
       retry_count integer NOT NULL DEFAULT 0,
       tokens_used integer,
       PRIMARY KEY (source_code, url),
       FOREIGN KEY (source_code) REFERENCES trading_news.sources(code),
       FOREIGN KEY (company_name) REFERENCES trading_news.companies(name)
   );

   CREATE TABLE IF NOT EXISTS trading_news.recommendation_persons (
       source_code       text NOT NULL,
       url               text NOT NULL,
       company_name      text NOT NULL,
       person_full_name  text NOT NULL,
       PRIMARY KEY (source_code, url, company_name, person_full_name),
       FOREIGN KEY (source_code, url) REFERENCES trading_news.recommendations(source_code, url),
       FOREIGN KEY (company_name, person_full_name) REFERENCES trading_news.persons(company_name, full_name)
   );
   ```
   Natural key структура **зеркалит** news_persons для consistency (P1.5).
2. `pusher.py` — **явный порядок push'а** в `push_all`, одна транзакция:
   ```python
   def push_all(...):
       with pg_conn.transaction():
           stats.companies_pushed = _push_companies(...)
           stats.sources_pushed = _push_sources(...)
           stats.persons_pushed = _push_persons(...)
           stats.news_pushed = _push_news(...)
           stats.recommendations_pushed = _push_recommendations(...)
           stats.news_persons_pushed = _push_news_persons(...)
           stats.recommendation_persons_pushed = _push_recommendation_persons(...)
   ```
   Junction-таблицы **последними** — FK уже satisfied.
3. `PushStats` расширить полями `recommendations_pushed`, `recommendation_persons_pushed`
4. `_push_recommendations` — копия `_push_news` с заменой имени таблицы и набора колонок
5. `_push_recommendation_persons` — копия `_push_news_persons` структурно

**Acceptance:**
- AC6.1. `pytest tests/test_cloud_sync.py` зелёные + новые тесты:
  - `test_pusher_recommendations_call_order` — mock psycopg, проверяет что executemany'и вызваны в порядке companies→sources→persons→news→recommendations→news_persons→recommendation_persons
  - `test_pusher_recommendations_upsert` — INSERT с ON CONFLICT DO UPDATE по PK `(source_code, url)`
  - `test_pusher_recommendation_persons_after_recommendations` — порядок проверен явно
- AC6.2. На существующей БД с пустой recommendations: push отрабатывает, `recommendations_pushed=0`, `recommendation_persons_pushed=0`
- AC6.3. `init-cloud-db` на старой схеме (с 4 таблицами) добавляет 2 новые без потери данных в existing

**Время:** ~2.5 ч (добавились явные order-тесты)

---

### T8 — Models + final lint/type/test pass (без live external cycle)

**Файлы:** `src/models.py`, общие проверки

1. `models.py` — добавить `Recommendation` dataclass:
   ```python
   @dataclass
   class Recommendation:
       id: int
       company_id: int
       source_id: int
       url: str
       headline: str
       body: str | None
       published_at: str
       fetched_at: str
       mood: str | None
       mood_reason: str | None
       target_price: float | None
       recommendation_action: str | None
       potential_pct: float | None
       multipliers_json: str | None
       status: str
       error_msg: str | None
       retry_count: int
       tokens_used: int | None
   ```
2. Полный health stack:
   - `python -m mypy src/ --ignore-missing-imports` — 0 issues
   - `python -m ruff check src/ tests/` — clean
   - `python -m pytest tests/ -q` — все зелёные
   - `coverage` для затронутых модулей — ≥ 90%
3. **Smoke (NOT live external):** `cmd_cycle` против temp DB с mocked sources / mocked OpenAI / Supabase OFF → отрабатывает, recommendations.count == 0 после cycle
4. Manual verification step (опциональный, не gate): пользователь может запустить live `python -m src cycle --company X5` на реальной БД и убедиться что existing flow не сломан

**Acceptance:**
- AC8.1. Health stack: typecheck, lint, test — все зелёные
- AC8.2. Coverage `src/fetcher.py`, `src/analyzer.py`, `src/reporter.py`, `src/db.py`, `src/cloud_sync/pusher.py` — ≥ 90%
- AC8.3. Smoke `cmd_cycle` с mocked deps — успешно, recommendations.count == 0
- AC8.4. `python -m src init-cloud-db` — успешно (если SUPABASE_DB_URL установлен; иначе skip)

**Время:** ~1.5 ч

---

## 4. Сводная оценка

| T-фаза | Задача | Время |
|---|---|---|
| T1 | БД миграция v3 (транзакционная + partial recovery) | 2 ч |
| T7-smoke | CLI status breakdown (раннее) | 0.7 ч |
| T2 | Source ABC + ItemDestination + RawItem расширение | 0.7 ч |
| T3 | Fetcher dispatcher + insert helpers | 2 ч |
| T4 | Analyzer два path'а + два SYSTEM_PROMPT'а | 3 ч |
| T5 | Reporter UNION dual-source + tie-break + xlsx | 3.5 ч |
| T6 | Cloud sync расширение + явный push order | 2.5 ч |
| T8 | Models + final pass (без live external) | 1.5 ч |
| **Итого** | | **~16 ч** (~2 рабочих дня) |

**Δ vs v1:** +3 ч (было ~13 ч). Прирост связан с правками P1: транзакционная миграция + partial recovery (T1), два SYSTEM_PROMPT'а + два error-marker'а (T4), tie-break tests + два листа xlsx (T5), explicit push order tests (T6).

---

## 5. Риски и митигация (v2)

| Риск | Митигация |
|---|---|
| Partial-v3 миграция при сбое в середине | Транзакция `with conn:` + user_version=3 последним (P1.7). Тест AC1.6 проверяет rollback. |
| Reporter UNION numbering флакает при равных timestamp'ах | Detrministic tie-break `_src_table, source_code, url` (P1.3). Тест AC5.4 проверяет регенерацию. |
| Cloud push падает на junction из-за FK | Strict order: companies→sources→persons→news→recommendations→junctions последними (P1.4). Тест AC6.1 проверяет order. |
| `news_persons` и `recommendation_persons` natural key несовместимы | Зеркальная структура (P1.5). |
| LLM возвращает `item_type` в recs-проходе (потому что SYSTEM_PROMPT_RECOMMENDATION urезанный — но модель может игнорировать) | Recs-парсер не валидирует `item_type` (P1.1). Тест AC4.2.2 проверяет что лишнее поле не ломает. |
| Existing finam-recs в news vs новые в recommendations — split data | Это **техдолг γ**, осознанный. Locations to remember (см. секцию 2) — карта для δ-completion. |
| `data.xlsx` sheet `news` поведенчески изменился (не содержит finam-recs) | Документировать в commit message + README после ship'а |
| `cloud_sync` additive-only не удаляет старые junction rows | Унаследовано из задачи 05; TODOS-запись на cleanup-задачу |
| Coverage падает из-за двух SYSTEM_PROMPT'ов и двух error-marker'ов | Тесты на оба path'а в T4 (AC4.2) |

---

## 6. Out of scope (явно)

- ❌ Подключение lmsic-источника — задача 07
- ❌ Миграция existing finam-rec строк в `recommendations` — γ-решение, оставляем
- ❌ Удаление `news.item_type` — δ-completion
- ❌ CHECK constraints на status/mood/recommendation_action — consistency с news (там их тоже нет)
- ❌ Жёсткие колонки под мультипликаторы — JSON покрывает
- ❌ Cleanup orphan junction rows в Supabase — TODOS-задача

---

## 7. Порядок ship'а

1. Создать ветку `recommendations_split` от master (после слияния PR #3 если ещё не)
2. Phase 4 (T1 → T7-smoke → T2 → T3 → T4 → T5 → T6 → T8) на этой ветке
3. Pre-ship gates: `/review` (claude) → `/codex review` → `/cso` → `/health`
4. PR `recommendations_split → master`. Title: «рефакторинг: отдельная таблица recommendations»
5. Body упомянуть behavior change: `data.xlsx` sheet `news` теперь без finam-recs (они в sheet `recommendations`)
6. После merge: обновить `TODOS.md` записью про δ-completion (см. спека 06)
7. Создать ветку `lmsic_ideas` для задачи 07 поверх обновлённого master

---

## 8. После APPROVED плана

План v2 закрывает все P1 из codex critique + auto-applied P2. Готов к Phase 4 (имплементация) без второго `/codex consult`.

**Опции пользователя:**
- A) Поехали в Phase 4 (имплементация T1 → ...)
- B) Сначала ещё один `/codex consult` против v2 для validation (не обязательно, но возможно — я не вижу зачем)
- C) Подождать с имплементацией, я хочу сначала почитать v2

Жду слово.
