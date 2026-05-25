# Estimate 06 — Codex critique плана `recommendations_split`

Источник: `codex exec --reasoning-effort=medium` (GPT-5-codex)
Дата: 2026-05-23
Объект ревью: `work_directory/02_plans/06_claude_recommendations_split_plan.md`
Связанная спека: `work_directory/01_specs/06_recommendations_split_spec.md`

**Как читать:** [P1] — критика блокирующая (нужно зафиксировать решение до начала имплементации). [P2] — advisory (стоит учесть, но можно defer'ить в TODOS если осознанно решено).

После каждого пункта оставлен маркер `Решение:` для твоего ответа. Можно написать «accept all P1, по P2 решай сам» — тогда я закрою estimate-файл и буду действовать соответственно.

---

## P1 — критика блокирующая

### [P1.1] Analyzer не «игнорирует» item_type у recommendations — нужен явный отдельный path

Codex: «Сейчас LLM prompt требует `item_type`, парсер валидирует `VALID_ITEM_TYPES`, а `UPDATE` всегда пишет `news.item_type`. Для `recommendations` этого поля нет. "Ignore если придёт" в плане противоречит текущему коду: нужен отдельный parse/update path или явный режим `table='recommendations'`, где `item_type` не требуется, не валидируется и не сохраняется.»

Моя интерпретация: единый SYSTEM_PROMPT с обязательным полем `item_type` в JSON-ответе будет ломаться на recommendations-path'е, потому что (а) колонки нет в таблице, (б) LLM будет тратить токены классифицируя бесполезно, (в) валидация `VALID_ITEM_TYPES` не сможет работать как было.

**Действие в плане:** T4 должен явно специфицировать **два режима analyze'а**:
- `_analyze_news(row)` — LLM возвращает `{mood, mood_reason, item_type}`, UPDATE news SET ... item_type=...
- `_analyze_recommendation(row)` — LLM возвращает `{mood, mood_reason}`, UPDATE recommendations SET ... (без item_type)

SYSTEM_PROMPT для recommendations — урезанная версия без инструкций классификации (несмотря на спека-P2.A «единый prompt»). Это **не** new prompt в смысле «новая семантика», это **dropped instruction** — убираем секцию про классификацию item_type. Низкий риск дрейфа.

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


### [P1.2] `_analyze_one(row, table: str)` опасен без whitelist таблицы

Codex: «SQL-плейсхолдеры не работают для имён таблиц. Если делать f-string, нужен закрытый enum/dispatch, не произвольная строка.»

**Действие в плане:** T4 — два отдельных хелпера (`_analyze_news`, `_analyze_recommendation`) с hardcoded именем таблицы в SQL. Никаких параметризованных table names через f-string. Естественно следует из P1.1.

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


### [P1.3] Reporter UNION — нестабильный numbering файлов при одинаковых `published_at`

Codex: «Сейчас порядок детерминирован `published_at ASC, id ASC`. В UNION у `news.id` и `recommendations.id` пересекающиеся пространства; нужен tie-breaker: local datetime, source table rank, source_code, id/url. Иначе idempotent regen будет иногда менять имена файлов.»

**Действие в плане:** T5 — explicit tie-break в UNION:
```sql
ORDER BY published_at ASC, _src_table ASC, source_code ASC, url ASC
```
- `_src_table` (literal в SELECT, "news" / "recommendations") даёт стабильное упорядочивание при коллизии timestamp'ов
- `source_code` + `url` дальше дисамбиугируют

AC5.5 (idempotent regen) — добавить тест на коллизию timestamp'ов (два item'а с одинаковым `published_at` в разных таблицах).

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


### [P1.4] Cloud sync — не зафиксирован порядок push'а и FK-зависимости для junction-таблиц

Codex: «Не описаны FK/ordering для `recommendation_persons`. Если junction пушится до recommendations/persons или FK ссылается не на тот natural key, push будет падать. В плане сказано "news → recommendations", но не зафиксирован полный порядок: companies, sources, persons, news, recommendations, news_persons, recommendation_persons.»

**Действие в плане:** T6 — явный порядок в `push_all`:
1. companies
2. sources
3. persons
4. news
5. recommendations
6. news_persons (FK на news, persons)
7. recommendation_persons (FK на recommendations, persons)

Junction-таблицы — всегда последними. Одна транзакция, чтобы partial-state не возникал.

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


### [P1.5] `recommendation_persons` natural key в Postgres — не зафиксирован

Codex: «Для `news_persons` PK включает `(source_code, url, company_name, person_full_name)`, хотя news PK `(source_code, url)`. Для recommendations надо явно решить: PK/FK `(source_code, url, company_name, person_full_name)` или без company. Иначе возможна FK-несогласованность с `recommendations.(source_code, url)`.»

**Действие в плане:** T6 — `recommendation_persons` natural key **зеркалит** `news_persons`:
```sql
PRIMARY KEY (source_code, url, company_name, person_full_name)
FOREIGN KEY (source_code, url) REFERENCES trading_news.recommendations(source_code, url)
FOREIGN KEY (company_name, person_full_name) REFERENCES trading_news.persons(company_name, full_name)
```
Симметрия с news_persons — упрощает code reuse в pusher.

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


### [P1.6] RawItem расширение надо делать СЕЙЧАС, не в задаче 07

Codex: «Fetcher планирует добавить structured fields в `RawItem`, но `_insert_into_recommendations` в T3 ставит их NULL "до lmsic". Тогда задача 07 будет вынуждена менять уже публичный `RawItem` контракт и fetcher insert. Лучше в 06 сразу вставлять эти поля из `RawItem`; иначе инфраструктура неполная.»

Согласен. В плане я писал «NULL на этапе 06» — это плохо, потому что задача 07 будет менять как `RawItem`, так и `_insert_into_recommendations`, и тесты обоих.

**Действие в плане:** T3 — `RawItem` сразу получает optional поля `target_price`, `recommendation_action`, `potential_pct`, `multipliers_json` (все default None). `_insert_into_recommendations` сразу их пишет. На задаче 06 никто их не заполняет → все будущие inserts будут NULL, но контракт фиксирован. Задача 07 = просто заполнить эти поля в LmsicSource.

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


### [P1.7] Migration v2→v3 — транзакционность и порядок установки user_version

Codex: «Не описана `PRAGMA foreign_keys=ON`/транзакционность/порядок установки `user_version`. Критично: `user_version=3` должен ставиться только после успешного создания обеих таблиц и индексов. При partial migration с поднятой версией `ensure_migrated` больше не починит схему.»

**Действие в плане:** T1 — `_migrate_to_v3(conn)` в одной транзакции:
```python
def _migrate_to_v3(conn):
    with conn:  # implicit BEGIN/COMMIT
        conn.execute("CREATE TABLE IF NOT EXISTS recommendations (...)")
        conn.execute("CREATE TABLE IF NOT EXISTS recommendation_persons (...)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recommendations_company_date ...")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recommendations_status ...")
        conn.execute("PRAGMA user_version = 3")
```
`PRAGMA user_version` ставится **последним** в той же транзакции — если CREATE упадёт, версия не сдвинется, `ensure_migrated` пересоберёт схему на следующем запуске.

Добавить тест на partial-v3: руками создать только `recommendations` без `recommendation_persons` и `user_version=2` → `ensure_migrated()` должен корректно дочинить (CREATE IF NOT EXISTS no-op'нет существующую, добавит недостающее, выставит version=3).

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


### [P1.8] `ensure_migrated` сейчас умеет только v2 — нужна цепочка v1→v2→v3

Codex: «Сейчас при любом `user_version < SCHEMA_VERSION` вызывает только `_migrate_to_v2`. После bump до 3 нельзя просто "добавить вызов после v2"; надо корректно обработать v1→v2→v3, v2→v3, частично созданную v3, fresh schema через `init_db`.»

**Действие в плане:** T1 — `ensure_migrated`:
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
            _migrate_to_v3(conn)
            # _migrate_to_v3 уже выставляет user_version=3 внутри транзакции
    finally:
        conn.close()
```
Тесты T1 — добавить test_db.py::test_migrate_v1_to_v3 (создать fake v1 БД руками, прогнать ensure_migrated, ожидать user_version=3 + обе новые таблицы).

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


---

## P2 — advisory

### [P2.1] AC1.2 говорит «6 таблиц», на самом деле 7

Codex: «Перечисляет 7 таблиц.»

Считаем: companies, sources, news, persons, news_persons, recommendations, recommendation_persons = **7**. План говорит «6». Опечатка.

**Действие:** обновить AC1.2.

Решение (предлагаю auto-fix): **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


### [P2.2] CHECK constraints для status / mood / recommendation_action

Codex: «Минимум `status IN (...)`, `mood IN (...)`, `recommendation_action IN ('buy','hold','sell') OR NULL`.»

Текущая `news` таблица **не** имеет CHECK constraints — validation на уровне приложения. Для consistency можно либо добавить везде, либо нигде. Я бы оставил как сейчас (валидация на уровне Python) — нет смысла менять подход ради одной новой таблицы.

**Действие:** оставляем без CHECK (consistency с news). Зафиксировать в плане как осознанное.

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


### [P2.3] Индекс на `recommendation_persons.person_id`

Codex: «Для агрегации persons.csv через UNION это быстро станет лишним full scan.»

Аналогично `news_persons` — у неё PK `(news_id, person_id)`, и Python-код по факту делает full scan когда агрегирует persons (но at scale ~100 строк это бесплатно). При росте до 10K+ имеет смысл.

**Действие:** добавить `CREATE INDEX IF NOT EXISTS idx_recommendation_persons_person ON recommendation_persons(person_id)` для симметрии с future-proofing.

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


### [P2.4] Reporter NULL-handling в YAML frontmatter

Codex: «Поля с `None` не должны превращаться в пустые ключи, строку `"None"` или невалидный YAML.»

**Действие:** T5 acceptance — добавить AC5.6: «Если `target_price IS NULL`, ключ `target_price:` отсутствует в frontmatter (не `target_price: None`, не `target_price:`). Аналогично для action/potential/multipliers.»

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


### [P2.5] `data.xlsx` — порядок листов и поведение sheet `news`

Codex: «План говорит, что sheet `news` остаётся, но не фиксирует порядок листов и не решает, включать ли legacy finam recommendations в sheet `news`. По смыслу нет, но это поведенческое изменение.»

**Действие:**
- Sheet 1: `news` — `WHERE item_type='news'` (раньше — все строки; теперь только news). **Это behavior change**, нужно явно отметить в плане.
- Sheet 2: `recommendations` — UNION(news WHERE item_type='recommendation', recommendations).
- Старые потребители xlsx, которые открывали первый лист и видели смесь, теперь увидят только news. Документировать в README + commit message.

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


### [P2.6] Persons CSV — семантическое изменение агрегации

Codex: «Раньше persons frequency была по всем analyzed news, включая finam recs как часть `news`. После split надо решить, считать recommendations вместе с news или делать отдельные колонки/файл.»

**Действие:** для P5/P2-точности — посчитать **обе таблицы** через UNION (то же что сейчас, потому что finam-recs остаются в news, плюс recommendations добавляются). Поведение не меняется концептуально: persons CSV всё ещё считает все упоминания везде.

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


### [P2.7] `AnalyzeResult` разделить по типу таблицы

Codex: «После двух проходов пользователь не увидит, где error/skipped/tokens: news или recommendations.»

**Действие:** `AnalyzeResult` расширить полями `news_analyzed`, `news_errored`, `recommendations_analyzed`, `recommendations_errored`. Лог-вывод тоже разбить. ~15 мин работы.

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


### [P2.8] Global config error на втором проходе — зафиксировать семантику

Codex: «news уже частично проанализированы и закоммичены, recs нет. Это нормально, но план должен явно зафиксировать semantics, иначе "abort whole batch" звучит неправдиво.»

**Действие:** в T4 явно прописать: «Global config error на news-проходе прерывает batch до начала recs-прохода. Global config error на recs-проходе оставляет news-проход уже закоммиченным (его уже не вернуть).» Пользователю надо это видеть в логах.

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


### [P2.9] Retry counting — generic marker нужен

Codex: «`_mark_error` сейчас жёстко обновляет `news`. Нужен generic marker с whitelist table или две функции.»

**Действие:** две функции `_mark_news_error(conn, news_id, ...)` и `_mark_recommendation_error(conn, rec_id, ...)` с hardcoded именем таблицы. Естественно следует из P1.1/P1.2.

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


### [P2.10] AC «141 тест без правок» конфликтует с T7

Codex: «test_status_counts обновляется. Надо убрать абсолютное утверждение или разделить.»

**Действие:** AC-A переформулировать: «Все существующие тесты зелёные. Допустимые правки: `test_status_counts` (breakdown по таблицам — намеренный change, отмечено в спеке P6), `test_reporter_xlsx_sheets` (теперь 2 листа). Все остальные — без правок.»

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


### [P2.11] AC8.3 live cycle как blocking gate — слишком нестабильно

Codex: «fetch ходит во внешние сайты, OpenAI нужен, Supabase опционален. Не должен быть обязательный gate для архитектурного refactor.»

**Действие:** AC8.3 переформулировать как **smoke**: вызвать `cmd_cycle` против temp DB с mocked sources, mocked OpenAI, no Supabase. Live cycle на реальном X5 — manual verification step, **не gate**.

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


---

## Architectural concerns

### [A.1] γ оставляет двойную read-path для recommendations

Codex: «Все read paths теперь обязаны помнить про два источника до δ-completion. План должен честно зафиксировать.»

Согласен. **Действие:** добавить в спеку и план секцию «Locations to remember during γ»:
- `reporter._generate_recommendations_md` — UNION
- `data.xlsx` recommendations sheet — UNION
- `persons.csv` — UNION
- CLI `status` — отдельные строки
- Cloud Postgres queries для recommendations — UNION на стороне читателя

Когда придёт δ-completion, эти места перечислены — лёгкий поиск.

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


### [A.2] Mixed-stream source невозможен в γ-архитектуре

Codex: «`Source.item_destination` на уровне класса закрывает mixed-stream sources. Для lmsic ок; для будущего это долг.»

Принято. Уже зафиксировано в TODO δ-completion (если появится другой mixed-stream source).

Решение (предлагаю acknowledge as known): **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


### [A.3] Cloud sync «additive-only» не удаляет старые junction rows

Codex: «Если локально persons matching изменится и связь исчезнет, `ON CONFLICT DO NOTHING` не удалит старую связь в Supabase. Это уже существующая проблема для `news_persons`, но новая таблица её удваивает.»

Это известная характеристика cloud_sync из задачи 05 (additive-only, documented in CLAUDE.md). Новая таблица наследует то же свойство.

**Действие:** не fix'им в 06; добавить в TODOS «cloud_sync: cleanup orphan junction rows in Supabase (news_persons + recommendation_persons)». Trigger: если когда-нибудь persons matching radically изменится.

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


---

## Sequencing recommendations (codex)

Codex предлагает порядок:
1. T1 + тесты миграций (включая partial v3) — **первым**
2. T2 + T3 — с полноценными RawItem полями, не NULL-заглушкой (P1.6)
3. T4 — два отдельных analyze-path'а (P1.1, P1.2) перед T5
4. T5 — после T4, с детерминированным UNION order (P1.3)
5. T6 — после фикса natural keys + FK order (P1.4, P1.5)
6. **T7 переносится ближе к T1** — CLI status быстро выявит что миграция подтянулась
7. T8 без live external cycle (P2.11)

Принимаю. **Новый порядок:** T1 → T7 (smoke check миграции через CLI) → T2 → T3 → T4 → T5 → T6 → T8.

Решение: **ACCEPT** (user: accept all P1, по P2 решай сам — Claude применил accept на все)


---

## Сводка по action items

Если accept all P1 — плану нужны существенные правки (особенно T1, T3, T4, T6). Я перепишу план v2 с этими правками после твоего ответа. Объём правок ~30 минут моей работы, не часов.

P2 — большинство small-fix; если accept all P2 — план тоже их учтёт.

После ответа:
1. Перепишу `02_plans/06_claude_recommendations_split_plan.md` → v2
2. Опционально — второй `/codex consult` против v2 если хочешь убедиться что критика учтена. (по моему опыту — не нужно, P1-фиксы достаточно)
3. Phase 3 (recon) для этой задачи не применим (внешних API не трогаем) → сразу Phase 4 (implementation)
