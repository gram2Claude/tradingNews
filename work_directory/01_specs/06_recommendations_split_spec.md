# Spec 06 — Отдельная таблица `recommendations` (refactor архитектуры)

Статус: APPROVED
Дата: 2026-05-23
Ветка: создать новую `recommendations_split` от master (после слияния PR #3)
Зависит от:
- `01_*_spec.md` (Source ABC, БД, schema, ensure_migrated)
- `04_finam_spec.md` (введение `item_type='recommendation'` в `news` — теперь становится **legacy путём** для finam-recs)
- `05_supabase_sync_spec.md` (cloud_sync схема — расширяется на новую таблицу)
Блокирует: `07_lmsic_ideas_spec.md` (lmsic = первый consumer новой таблицы)
Связанный план: `02_plans/06_claude_recommendations_split_plan.md` (создаётся после APPROVED)

---

## Контекст и мотивация

Сейчас торговые рекомендации (item_type='recommendation') хранятся в той же таблице `news`, что и обычные новости — различаются только колонкой `news.item_type`, добавленной в v2-миграции задачи 04 (finam). Это решение было компромиссом: finam-лента смешанная (новости + рекомендации), LLM классифицирует per item, разводить таблицу по типу было дорого.

В ходе подготовки задачи `07_lmsic_ideas` (третий источник — **только** торговые рекомендации) обсудили P3 «структурированные поля для рекомендаций» и пришли к решению:

1. Завести **отдельную таблицу `recommendations`** с собственными структурированными колонками (`target_price`, `recommendation_action`, `potential_pct`)
2. **Стратегия для finam: γ (компромисс)** — finam продолжает писать в `news` с `item_type='recommendation'` как раньше; новая таблица `recommendations` заполняется только из специализированных источников (lmsic и будущие).
3. **Existing finam-rec данные** в `news` остаются на месте — миграции данных нет.

Это refactor архитектуры, который **выполняется первым** (NN=06), затем поверх готовой схемы добавляется lmsic (NN=07).

Связь с обсуждением — см. `07_lmsic_ideas_spec.md`, секция «P3-EXT».

---

## Цели refactor'а

1. **Структурированные поля для recommendations**: фильтрация / агрегация по target / action без LLM post-processing
2. **Чёткое разделение типов**: news vs recommendations — два самостоятельных потока с собственными invariants
3. **Готовая основа** для дальнейших recommendation-only источников (lmsic, потенциально дальше)
4. **Zero behavior change** для существующих flows на момент merge: новая таблица пуста, dispatcher для всех источников отдаёт ту же логику что раньше. Видимая работа появляется только когда подключается lmsic (задача 07).

---

## Что меняется

### 1. БД (миграция v2 → v3)

Новая таблица `recommendations`:
```sql
CREATE TABLE IF NOT EXISTS recommendations (
    id                    INTEGER PRIMARY KEY,
    company_id            INTEGER NOT NULL REFERENCES companies(id),
    source_id             INTEGER NOT NULL REFERENCES sources(id),
    url                   TEXT NOT NULL,
    headline              TEXT NOT NULL,
    body                  TEXT,
    published_at          TEXT NOT NULL,
    fetched_at            TEXT DEFAULT CURRENT_TIMESTAMP,
    mood                  TEXT,
    mood_reason           TEXT,
    target_price          REAL,          -- v3 NEW
    recommendation_action TEXT,          -- v3 NEW: 'buy' | 'hold' | 'sell' | NULL
    potential_pct         REAL,          -- v3 NEW
    multipliers_json      TEXT,          -- v3 NEW: '{"EV/EBITDA":4.1,"P/E":6.8,...}'
    status                TEXT DEFAULT 'new',
    error_msg             TEXT,
    retry_count           INTEGER DEFAULT 0,
    tokens_used           INTEGER,
    UNIQUE (source_id, url)
);

CREATE INDEX IF NOT EXISTS idx_recommendations_company_date
    ON recommendations(company_id, published_at);
CREATE INDEX IF NOT EXISTS idx_recommendations_status
    ON recommendations(status);
```

Новая таблица persons-связи:
```sql
CREATE TABLE IF NOT EXISTS recommendation_persons (
    recommendation_id  INTEGER NOT NULL REFERENCES recommendations(id) ON DELETE CASCADE,
    person_id          INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    PRIMARY KEY (recommendation_id, person_id)
);
```

**Миграция (`_migrate_to_v3` в `db.py`):**
- Идемпотентная: проверка `CREATE TABLE IF NOT EXISTS`
- `news.item_type` **остаётся** (γ): finam продолжает её использовать
- Bump `PRAGMA user_version` 2 → 3
- `ensure_migrated()` подтягивает на старте `cmd_*`, как и для v2

---

### 2. Source ABC — расширение

`src/sources/base.py:Source` сообщает свой output-канал:
```python
class Source(ABC):
    # Existing
    code: str
    ...
    # NEW
    item_destination: ItemDestination = ItemDestination.NEWS  # default

class ItemDestination(Enum):
    NEWS = "news"
    RECOMMENDATIONS = "recommendations"
```

- `X5IRSource.item_destination = NEWS` (default — без изменений)
- `FinamSource.item_destination = NEWS` (γ-решение: finam-recs остаются в news + item_type, как раньше)
- `RBCSource.item_destination = NEWS` (когда/если воскреснет)
- `LmsicSource.item_destination = RECOMMENDATIONS` (вводится в задаче 07)

**Альтернативно:** не enum в Source, а тип RawItem.item_destination — позволяет одному источнику писать в обе таблицы (если когда-то понадобится — отказались в γ, но это держим как возможность). На MVP — лучше на уровне Source (проще).

---

### 3. `fetcher.py` — dispatcher

```python
def _insert_raw_item(conn, item: RawItem, source_id: int, company_id: int,
                    destination: ItemDestination) -> int:
    if destination == ItemDestination.NEWS:
        return _insert_into_news(conn, item, source_id, company_id)
    else:
        return _insert_into_recommendations(conn, item, source_id, company_id)
```

Source вызывает dispatcher с `self.item_destination`. INSERT OR IGNORE по `UNIQUE(source_id, url)` работает одинаково в обеих таблицах.

---

### 4. `analyzer.py` — два прохода

```python
def analyze_all(cfg):
    ...
    # Первый проход — news (как сейчас)
    for row in conn.execute("SELECT ... FROM news WHERE status='new' AND retry_count < 3"):
        _analyze_one(row, table='news')
    # Второй проход — recommendations (NEW)
    for row in conn.execute("SELECT ... FROM recommendations WHERE status='new' AND retry_count < 3"):
        _analyze_one(row, table='recommendations')
```

**SYSTEM_PROMPT — единый для обеих**, MVP. Если позже выяснится что для recommendations нужен другой prompt (фокус на торговую идею, не на новостной mood) — заведём отдельный (отдельная задача).

**Persons-matching** для recommendations пишет в `recommendation_persons` вместо `news_persons` — диспатч по типу item'а.

**LLM ответ для recommendations** содержит те же `mood` + `mood_reason`. Структурированные поля (`target_price`, `recommendation_action`, `potential_pct`, `multipliers_json`) **заполняет fetcher** при парсинге HTML lmsic (задача 07), а не LLM.

---

### 5. `reporter.py` — dual-source для recommendations-папки

Сейчас reporter читает `news`, фильтрует по `item_type`, разводит по папкам. После refactor'а:

```python
def _generate_recommendations_md(conn, company_id, company_name):
    # γ-техдолг: UNION двух источников
    rows = conn.execute("""
        SELECT id, headline, body, published_at, mood, mood_reason,
               NULL AS target_price, NULL AS recommendation_action,
               NULL AS potential_pct, NULL AS multipliers_json,
               'news' AS _src_table
        FROM news WHERE company_id = ? AND item_type = 'recommendation'
                  AND status = 'analyzed'
        UNION ALL
        SELECT id, headline, body, published_at, mood, mood_reason,
               target_price, recommendation_action, potential_pct, multipliers_json,
               'recommendations' AS _src_table
        FROM recommendations WHERE company_id = ? AND status = 'analyzed'
        ORDER BY published_at DESC
    """, (company_id, company_id))
    ...
```

News-папка читает только `news WHERE item_type = 'news'` (без изменений).

**`data.xlsx`:** новый лист `recommendations` с дополнительными колонками. Старый лист `news` остаётся (фильтрует `item_type='news'`).

**Persons CSV:** агрегация по обеим таблицам через UNION.

---

### 6. `cloud_sync/` — push новой таблицы

- `schema.sql` (Postgres): добавить `trading_news.recommendations` + `trading_news.recommendation_persons`
- `pusher.py`: новый `_push_recommendations(...)` + `_push_recommendation_persons(...)`, аналогично существующим
- Natural keys: `recommendations.(source_code, url)` — как у news

**`init-cloud-db`** должна быть идемпотентной для существующих установок (добавление новых таблиц через `CREATE TABLE IF NOT EXISTS`).

---

### 7. `models.py` — новый dataclass

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

---

### 8. CLI — `status` показывает breakdown

```
$ python -m src status

X5
  news (5 sources):  142 analyzed, 3 new
  recommendations:   17 analyzed, 0 new   ← NEW row
```

---

## Что НЕ меняется (явно)

- `news.item_type` колонка остаётся — finam продолжает писать в `news` с `item_type='recommendation'`. **Техдолг γ, осознанный.**
- Существующие строки в `news` — не трогаем. Никаких UPDATE/DELETE/MOVE.
- `name_matcher.py` — pure function, без изменений
- `text_cleanup.py` — без изменений
- `config.py` — без изменений (новых полей CompanyCfg/SourceCfg не требуется)
- Все существующие тесты должны проходить без правок (zero behavior change для существующих источников)

---

## Premise Challenge

Поставь ответ рядом с каждым пунктом.

### P1. ItemDestination — где хранить признак

**A) Атрибут класса `Source.item_destination`** (предложено выше)
- ✅ Один источник = одна целевая таблица. Просто, статично.
- ❌ Если в будущем source понадобится писать в обе таблицы (пока не требуется) — overhead на переделку.

**B) Поле `RawItem.item_destination`** (per-item решение)
- ✅ Гибче — finam теоретически мог бы решать per item (LLM-классификация → dest)
- ❌ Сложнее: каждый source должен заполнять поле; для x5_ir / lmsic — boilerplate; γ нам этого как раз и не нужен

**Рекомендация: A** (Source-level). γ-решение не требует per-item диспатча.

Твой ответ: Рекомендация: A


### P2. SYSTEM_PROMPT для recommendations — единый или отдельный

В MVP оба типа items анализируются одним promptом (mood + mood_reason). Для recommendations возможно стоит дать LLM фокус на «торговую идею оптимистична / нейтральна / пессимистична» вместо general «настроение новости».

**A) Единый prompt (как сейчас)**
- ✅ Нулевой refactor analyzer'а
- ❌ Mood для recommendations может быть менее точным

**B) Per-table prompt**
- ✅ Точнее семантически
- ❌ Больше работы; два prompt'а поддерживать; risk промпт-дрейфа

**Рекомендация: A для MVP.** Если в эксплуатации увидим что mood у recommendations плохой — отдельная задача.

Твой ответ: Рекомендация: A


### P3. `multipliers_json` — JSON-строка vs отдельные колонки

EV/EBITDA, P/E, Net debt/EBITDA — это набор именованных чисел, варьируется по источникам/датам.

**A) Одна колонка `multipliers_json TEXT`** (предложено)
- ✅ Гибко: разные источники могут отдавать разный набор мультипликаторов
- ❌ SQL-запросы по конкретному мультипликатору — через json_extract (SQLite JSON1)

**B) Жёсткие колонки** `ev_ebitda REAL`, `pe REAL`, `net_debt_ebitda REAL`, ...
- ✅ Простые SQL-запросы
- ❌ Раздувание схемы; если источник добавит `ROE` — миграция

**Рекомендация: A.** Запросы по конкретному мультипликатору — это нишевый use case; gibkost' важнее.

Твой ответ: Рекомендация: A


### P4. Cloud sync — расширять схему или нет

`init-cloud-db` уже отработан на v2 (4 таблицы). Сейчас расширяем до 6 (recommendations + recommendation_persons). Schema migration в Postgres — `CREATE TABLE IF NOT EXISTS`.

**Что с push'ем для существующих установок?**

**A) Auto-extend через `init-cloud-db`** (одной командой)
- ✅ User запускает `python -m src init-cloud-db` после pull — обе новые таблицы создаются
- ✅ Идемпотентно — повторный запуск ничего не ломает

**B) Сделать отдельную команду `init-cloud-db --upgrade`**
- ❌ Overkill для CREATE TABLE IF NOT EXISTS

**Рекомендация: A** — встроить в `init-cloud-db`, документировать в README что после pull нужно запустить.

Твой ответ: Рекомендация: A


### P5. Reporter dual-source для recommendations-папки — UNION vs два отдельных пути

Сейчас reporter генерирует один Markdown-файл per item. Для папки `output/X5/recommendations/...` теперь два источника данных.

**A) Один UNION-запрос** (предложено выше)
- ✅ Один проход по выдаче, последовательный numbering файлов корректен
- ❌ NULL-колонки для finam-row'ов — нужно явно обрабатывать в генерации Markdown

**B) Два прохода**: сначала `news WHERE item_type='recommendation'`, потом `recommendations`
- ✅ Чистый код (две функции, разные dataclass'ы)
- ❌ Файлы пересортируются неудобно (если хотим mixed-by-date — нужен post-merge)

**Рекомендация: A** (UNION) — единая выборка by date, NULL-обработка тривиальна (если у row нет target — секция «Цель» в frontmatter просто отсутствует).

Твой ответ:Рекомендация: A


### P6. Existing tests — должны ли все продолжать проходить без правок?

Refactor — zero behavior change для всех существующих источников. Все 141 тест из `pytest tests/ -q` **должны проходить без изменений**. Это ключевой gate перед ship'ом.

Если какой-то тест придётся менять — это сигнал что refactor затронул semantics, нужно явно объяснить (например, `test_status_counts` ожидает breakdown по таблицам — приемлемый change, expected).

**Рекомендация: yes — все 141 тест зелёные.** Новые тесты добавятся для recommendations-таблицы (insert / analyze / report path с пустыми данными).

Твой ответ: Рекомендация: yes — все 141 тест зелёные


---

## Поток данных (после refactor'а)

```
fetch:
  Source.fetch() → list[RawItem]
  → fetcher dispatcher(source.item_destination)
      → INSERT OR IGNORE → news  (x5_ir, finam) или recommendations (никто пока в 06)

analyze:
  analyzer проход 1: SELECT FROM news WHERE status='new' → LLM → UPDATE news
  analyzer проход 2: SELECT FROM recommendations WHERE status='new' → LLM → UPDATE recommendations

report:
  reporter.news/         ← news WHERE item_type='news'
  reporter.recommendations/ ← UNION (news WHERE item_type='recommendation') + recommendations
  reporter.data.xlsx     ← two sheets

cloud_sync (если SUPABASE_DB_URL):
  push companies, sources, persons, news_persons, news
  push recommendations, recommendation_persons (NEW)
```

---

## Безопасность

| Риск | Митигация |
|---|---|
| Миграция БД падает в середине | `_migrate_to_v3` — `CREATE TABLE IF NOT EXISTS`, нет ALTER на existing tables. Idempotent. |
| Параметризация SQL | Все INSERT в recommendations — `?`-плейсхолдеры (как везде) |
| JSON injection в multipliers_json | `multipliers_json` парсится из контролируемых HTML-полей source'а, не user input. На вставке — `json.dumps(dict)` гарантирует валидный JSON. |
| Cloud sync drift | natural keys (`source_code`, `url`) — те же что у news. Идемпотентность UPSERT'а гарантирует zero drift при повторных push'ах. |
| Существующие данные в news повреждаются | Refactor НЕ трогает news — никаких UPDATE/DELETE. Только новая таблица + новый dispatcher на новый код-путь, которым пока никто не пользуется. |
| psycopg / SQLite migration race | `init-cloud-db` идемпотентен и atomic per transaction. |

---

## Out of scope (задача 06)

- ❌ **Подключение нового source** — отдельная задача 07 (lmsic). Здесь только infrastructure prep.
- ❌ **Миграция existing finam-rec строк** из news в recommendations — γ-решение, оставляем как есть. Возможно сделается в δ-completion (когда-то в будущем).
- ❌ **Per-table SYSTEM_PROMPT** в analyzer (P2.B) — отдельная задача если понадобится.
- ❌ **Removal of `news.item_type`** колонки — отдельная задача (δ-completion).
- ❌ **Жёсткие колонки под каждый мультипликатор** (P3.B) — JSON покрывает.

---

## TODO для будущего (δ-completion)

После ship'а задачи 06 — добавить в `TODOS.md`:

> **δ-completion** (трансформация γ → δ): убрать `news.item_type`; мигрировать existing finam-rec строки в `recommendations`; LLM-классификация в finam заменяется на «всегда news» (или per-item dispatcher если оставляем mixed-stream).
>
> **Trigger:** finam recommendation accuracy становится бизнес-критичным, ИЛИ появляется другой mixed-stream источник (где LLM-классификация нужна).
>
> **Scope:** ~1 день. Migration v3 → v4: ALTER news DROP COLUMN item_type, INSERT INTO recommendations SELECT ... FROM news WHERE item_type='recommendation', DELETE FROM news WHERE ... Также: убрать LLM-классификацию item_type в analyzer (или сделать per-item dispatcher).

---

## Открытые вопросы

- P1 — ItemDestination: Source-level (A) или per-item (B)?
- P2 — SYSTEM_PROMPT: единый (A) или per-table (B)?
- P3 — multipliers: JSON (A) или жёсткие колонки (B)?
- P4 — Cloud sync init-extension (A) подтвердить?
- P5 — Reporter dual-source: UNION (A) или два прохода (B)?
- P6 — Все 141 теста зелёные после refactor'а — подтвердить как gate.

После ответов перевожу в **APPROVED** и пишу `02_plans/06_claude_recommendations_split_plan.md`.
