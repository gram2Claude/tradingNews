# Спец 08 — δ-completion: устранить дуальный read-path для recommendations

Автор: Claude
Дата: 2026-05-25
Ветка: `delta_completion_06` (от master @ `024028c`)
Источник: TODOS.md → «δ-completion» (отложено из task 06 γ-стратегии)

---

## Контекст

После задачи 06 (`recommendations_split`) рекомендации хранятся **в двух
источниках**:

1. `news WHERE item_type='recommendation'` — legacy finam-recs
   (исторические данные + новые finam-items, классифицированные LLM как
   recommendation)
2. `recommendations` — новая таблица со структурными полями
   (`target_price`, `recommendation_action`, `potential_pct`,
   `multipliers_json`); заполняется recommendation-only источниками
   (`lmsic` из task 07)

**Проблема (тех долг task 06):**
- Reporter, `persons.csv`, `data.xlsx` делают UNION ALL над обоими
  источниками — два path'а для каждого read'а
- analyzer имеет два SYSTEM_PROMPT'а (`SYSTEM_PROMPT_NEWS` с item_type
  классификацией + `SYSTEM_PROMPT_RECOMMENDATION` без неё)
- `news.item_type` колонка имеет смысл только для finam (mixed-stream),
  для x5_ir всегда `'news'` — schema-level noise
- Любая правка reporter'а требует синхронизации двух SQL queries

**δ-completion (задача 08):** свести всё к одной таблице `recommendations`.

---

## Цели

1. `news.item_type` колонка **удаляется** (миграция v3 → v4)
2. Legacy finam-recs мигрируют в таблицу `recommendations`
   (target_price etc. остаются NULL — структурных полей у них нет)
3. analyzer.SYSTEM_PROMPT_NEWS **больше не классифицирует** item_type;
   но finam recommendation-классификация **сохраняется** через
   per-item dispatch в analyzer (P3)
4. Reporter / `persons.csv` / `data.xlsx` читают из одной таблицы каждый
   (news → news, recommendations → recommendations), без UNION
5. Cloud sync остаётся работающим (Postgres schema совпадает уже —
   таблица recommendations есть с task 06)

---

## Open questions / Решения

### P1: Что делать с классификацией finam-новостей после удаления `item_type`?

**Вариант A — drop classification полностью** (radical scope cut).
finam становится news-only stream. Будущие рекомендации finam уходят в
news как обычные новости (LLM даёт mood/mood_reason, но не выделяет
их в отдельный поток).
- Плюс: проще всего, меньше LLM-токенов, нет cross-table move
- Минус: теряем фичу разделения news vs recommendation для finam.
  Lmsic остаётся единственным structured-recommendation источником.

**Вариант B — per-item dispatch в analyzer (рекомендуемый).**
analyzer.SYSTEM_PROMPT_NEWS продолжает классифицировать item_type
(news / recommendation). Если LLM сказал `recommendation`:
- INSERT новая строка в `recommendations` (с теми же mood/mood_reason,
  body, headline и `recommendation_action=None`, `target_price=None`)
- DELETE из `news`
- Иначе — UPDATE news как сейчас (без item_type колонки — она удалена)
- Persons-junction тоже перемещаются: `news_persons` → `recommendation_persons`

- Плюс: сохраняем поведение «finam микс новости и рекомендации
  автоклассифицируется»; reporter упрощается; per-source dispatch
  чистый.
- Минус: внутри analyzer появляется cross-table-move logic — нужны
  тесты на атомарность (если что-то падает между INSERT и DELETE).

**Решение:** _Твой ответ:_

```
B (per-item dispatch в analyzer). Структурные поля все NULL для finam.
```

### P2: Migration v3 → v4 — стратегия

Миграция при `db.ensure_migrated()`:

```sql
SAVEPOINT v4_migration;

-- 1. Перенос existing finam-recs в recommendations
INSERT OR IGNORE INTO recommendations
    (company_id, source_id, url, headline, body, published_at,
     status, mood, mood_reason, retry_count, error_msg, created_at,
     target_price, recommendation_action, potential_pct, multipliers_json)
SELECT
    company_id, source_id, url, headline, body, published_at,
    status, mood, mood_reason, retry_count, error_msg, created_at,
    NULL, NULL, NULL, NULL
FROM news WHERE item_type = 'recommendation';

-- 2. Перенос junction-строк
INSERT OR IGNORE INTO recommendation_persons (recommendation_id, person_id)
SELECT r.id, np.person_id
FROM news n
JOIN news_persons np ON np.news_id = n.id
JOIN recommendations r ON r.source_id = n.source_id AND r.url = n.url
WHERE n.item_type = 'recommendation';

-- 3. Удаление migrated news (CASCADE удалит news_persons автоматически)
DELETE FROM news WHERE item_type = 'recommendation';

-- 4. Drop column (SQLite — через rebuild)
-- В SQLite нет ALTER TABLE DROP COLUMN < 3.35; делаем CREATE TABLE news_new + INSERT + DROP + RENAME
CREATE TABLE news_new (... без item_type ...);
INSERT INTO news_new SELECT (все колонки кроме item_type) FROM news;
DROP TABLE news;
ALTER TABLE news_new RENAME TO news;
-- Восстановить indices и triggers

RELEASE v4_migration;
PRAGMA user_version = 4;
```

**Решение:** _Твой ответ:_

```
ОК. Migration делать через rebuild-таблицу (SQLite legacy mode).
```

### P3: Postgres schema (cloud_sync)

Cloud schema из task 06 уже имеет таблицу `trading_news.recommendations`
со всеми колонками. `news.item_type` в Postgres схеме **есть** —
надо тоже удалить?

Вариант A: оставить `news.item_type` в Postgres как nullable, заполнять
NULL'ом при push. Незаметно для аналитики.
Вариант B: убрать колонку из Postgres (DDL migration в schema.sql) —
требует ручного `init-cloud-db` на стороне пользователя.

**Решение:** _Твой ответ:_

```
B (убрать). Запустить init-cloud-db руками после первого cycle на
обновлённой версии. Schema.sql обновить.
```

### P4: analyzer prompts — насколько урезать SYSTEM_PROMPT_NEWS

После B (P1) analyzer всё ещё **классифицирует** item_type, но не
пишет его в news. Значит SYSTEM_PROMPT_NEWS остаётся почти как есть,
только output schema меняется: LLM возвращает {mood, mood_reason,
item_type}, analyzer использует item_type для dispatch'а (INSERT в
recommendations + DELETE news ИЛИ просто UPDATE news), а не для UPDATE
news.item_type column.

**Решение:** _Твой ответ:_

```
Оставить SYSTEM_PROMPT_NEWS как есть (LLM продолжает возвращать
item_type). Изменяется только обработка ответа.
```

### P5: backward-compat для existing output/

В output/ уже могут быть `.md` файлы с `item_type: ...` в frontmatter.
- Вариант A: reporter wipe'ает output перед regen (как сейчас), новые
  файлы не пишут item_type → backward-compat OK
- Вариант B: миграция чистит и output тоже

**Решение:** _Твой ответ:_

```
A. Reporter wipe уже делает работу, дополнительной миграции не надо.
item_type в frontmatter — убирается из шаблона.
```

---

## Acceptance

- `python -m src init-db` на свежей БД — нет колонки `news.item_type`,
  есть таблица `recommendations` со всеми полями
- Existing БД с finam-recs → после `ensure_migrated()` (v3→v4):
  - 0 строк в `news WHERE item_type=...` (колонки нет → ошибка
    «no such column» если попробовать SELECT)
  - N строк в `recommendations` где `recommendation_action IS NULL AND
    target_price IS NULL` (legacy finam-recs)
  - junction-строки сохранились (`recommendation_persons` содержит
    person→rec linkage)
- analyzer на mixed-stream finam:
  - LLM возвращает `item_type=recommendation` → строка переходит в
    recommendations, news_persons → recommendation_persons, status='analyzed'
  - LLM возвращает `item_type=news` → строка остаётся в news,
    обновлены mood/mood_reason
- reporter: 0 UNION в SQL, один путь для каждой таблицы
- `data.xlsx` имеет два листа — news ТОЛЬКО из news table,
  recommendations ТОЛЬКО из recommendations table
- `persons.csv` CTE упрощается — single SELECT FROM recommendations
- cloud_sync push: schema без news.item_type, recommendations пуш как раньше
- Все 215 baseline тестов + новые ~15-20 (миграция + per-item dispatch)
  зелёные
- ruff + mypy clean
- Live smoke: `python -m src cycle --company X5` exit 0,
  recommendations table содержит legacy finam-recs + lmsic-recs

---

## Out of scope

- Бэкап БД перед миграцией (миграция идемпотентна через SAVEPOINT;
  пользователь сам решает делать ли копию `data/db.sqlite` перед
  ensure_migrated)
- Reporter UI changes (frontmatter формат остаётся как сейчас минус
  `item_type` ключ)
- Performance оптимизации analyzer'а (cross-table move медленнее
  одного UPDATE — но 1 строка / запрос, не bottleneck)

---

## Риски

| Риск | Митигация |
|---|---|
| Cross-table move в analyzer не атомарен (INSERT recommendations + DELETE news) | Всё под одной `with conn:` транзакцией; либо обе операции, либо rollback |
| Junction-rows news_persons не переезжают | INSERT recommendation_persons ДО DELETE FROM news (FK CASCADE удалит news_persons после) |
| Postgres init-cloud-db требует ручного действия | Логируется WARNING при push'е если в облаке всё ещё есть news.item_type column |
| Migration v3→v4 на пустой свежей БД | Защита: `IF EXISTS (SELECT 1 FROM news WHERE item_type IS NOT NULL)` или просто проверка существования колонки через `PRAGMA table_info` |
| Реализация SQLite drop column через rebuild — потеря indices/triggers | В db.py SCHEMA_SQL все indices указаны явно; пересоздать после rename |

---

## После ship'а

- Удалить TODOS.md → «δ-completion» секцию (переедет в Done)
- Обновить CLAUDE.md секции «News vs recommendations», убрать γ-стратегию
- Schema version → v4 фиксируется
