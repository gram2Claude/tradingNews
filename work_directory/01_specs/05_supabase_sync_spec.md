# 05 — Supabase sync (SQLite → Postgres зеркало)

## Проблема

`data/db.sqlite` лежит локально на одной машине. Чтобы посмотреть/проанализировать
данные с другого компа сейчас нужен файловый шаринг (OneDrive, копирование).
Хочется иметь облачное зеркало, доступное из любого браузера через Supabase
dashboard или из других клиентов через REST.

## Решение

**One-way push: SQLite → Supabase Postgres**, без обратного синка.

- SQLite остаётся source of truth (как в `CLAUDE.md` «Architecture»).
- После каждого успешного `cycle` (когда локальная БД и output/ уже
  обновлены) скрипт пушит дельту в Postgres через `UPSERT`.
- Любые правки в Supabase dashboard будут затёрты следующим push'ем —
  это **сознательное ограничение**, документируется в README.

## Решения

| Вопрос | Ответ |
|--------|-------|
| Какие таблицы зеркалим? | `companies`, `sources`, `persons`, `news` (полное зеркало) |
| Какой ключ используем? | `service_role` (server-side, обходит RLS), хранится в `.env` |
| RLS политики? | **Off** на старте (single-user). Включим если откроем dashboard для других |
| Где живёт URL? | `.env: SUPABASE_URL`, `.env: SUPABASE_SERVICE_ROLE_KEY` |
| Когда триггерится? | Внутри `cycle` после reporter'а (см. ниже) + standalone `python -m src sync-cloud` |
| Что при сетевой ошибке? | Cycle не падает: лог-warning, exit 0. Standalone — exit 1 |
| Upsert ключ? | `news.(source_id_code, url)` UNIQUE; `companies.code`; `sources.code`; `persons.(company_code, surname)` |
| Чем пушим? | Прямой Postgres через `psycopg[binary]` — НЕ REST. Меньше зависимостей, проще debug |
| Connection string? | Из Supabase dashboard → Settings → Database → Connection string (URI mode). Хранится в `.env: SUPABASE_DB_URL` |
| Pooling? | `?sslmode=require` обязательно. Pooler (port 6543) или direct (5432) — на выбор |

## Поток данных

```
cli.cmd_cycle:
  fetcher.fetch_all      → SQLite
  analyzer.analyze_all   → SQLite
  reporter.report_all    → output/ (regen)
  ──────────  локальная часть закончена  ──────────
  cloud_sync.push_all    → Supabase Postgres (NEW)
```

Standalone:
```
python -m src sync-cloud [--company X5]  # без фильтра — все компании
```

## Схема Postgres

Один-в-один с SQLite (см. `src/db.py:SCHEMA`), но:

1. **Денормализованные коды вместо FK id**. SQLite использует целочисленные
   `company_id`, `source_id` — они зависят от порядка вставки и НЕ
   совпадают между машинами. В Postgres ключуем по строкам:
   - `news.company_code TEXT` вместо `news.company_id INTEGER`
   - `news.source_code TEXT` вместо `news.source_id INTEGER`
   - `persons.company_code TEXT` аналогично.
2. **`TIMESTAMPTZ`** для `published_at`, `created_at`, `analyzed_at`.
   SQLite хранит ISO-строки с offset, Postgres парсит их в native tz-aware.
3. **`mood` и `item_type` остаются TEXT** с CHECK constraint (whitelist).
4. **PRIMARY KEY** = `(source_code, url)` для news (вместо суррогатного id) —
   делает upsert тривиальным и решает дрейф id'шек между машинами.

Файл DDL: `src/cloud_sync/schema.sql` (применяется один раз вручную через
Supabase SQL editor либо через `python -m src init-cloud-db`).

## Скоуп

**В:**
- `src/cloud_sync/` модуль: `schema.sql`, `pusher.py`, `__init__.py`.
- CLI команда `sync-cloud` в `src/cli.py`.
- Интеграция в `cmd_cycle` после reporter'а, обёрнутая в try/except.
- `init-cloud-db` команда для разворачивания схемы (idempotent: `CREATE TABLE IF NOT EXISTS`).
- `.env.example`: добавить `SUPABASE_DB_URL=` плейсхолдер.
- `requirements.txt`: `psycopg[binary]>=3.2`.
- Тесты: unit на upsert SQL (через pytest fixture с локальной Postgres? — нет, тяжело. Mock psycopg connection).
- README: раздел «Облачное зеркало (Supabase)».
- CLAUDE.md: добавить cloud_sync в «Architecture» секцию и команду в «Commands».

**Вне:**
- Two-way sync (правки в Supabase → SQLite).
- RLS policies.
- Дашборд / web UI поверх Supabase.
- Real-time подписки (Supabase Realtime).
- Миграция SQLite → Postgres как source of truth.

## Безопасность

- `SUPABASE_SERVICE_ROLE_KEY` и `SUPABASE_DB_URL` — **только в .env**,
  никогда не коммитятся (`.gitignore` уже прикрывает `.env`).
- `.env.example` содержит только плейсхолдеры с комментарием
  `# from Supabase dashboard → Settings → Database`.
- При логировании connection string **маскировать пароль**:
  `postgresql://postgres:***@db.xxx.supabase.co:5432/postgres`.
- Логи psycopg на INFO (не DEBUG) — статусы выполнения, но не значения
  параметров. Аналогично пиннингу httpx в `CLAUDE.md` секция «Security».
- `psycopg` параметризует все запросы (passes values not as string concat) —
  SQL injection защита на уровне библиотеки.
- Все исключения `psycopg.Error` в `cycle` глотаются с лог-warning'ом,
  не пробрасывают значения параметров наружу.

## Acceptance criteria

1. `python -m src init-cloud-db` создаёт 4 таблицы в Supabase, idempotent
   (повторный запуск не падает).
2. `python -m src cycle --company X5` после успешного reporter'а пушит
   все строки `news` в Postgres. Логи показывают `cloud_sync: pushed N
   rows (M new, K updated)`.
3. Запуск `cycle` при сломанной сети: локальная часть проходит, push
   падает с WARNING в логах, exit code = 0, news.status остаются как есть.
4. `python -m src sync-cloud` standalone делает то же без `fetch/analyze/report`.
5. В Supabase SQL editor: `SELECT count(*) FROM news WHERE company_code='X5'`
   совпадает с локальным `SELECT count(*) FROM news WHERE company_id=1`.
6. Тесты: 5+ новых, все зелёные. Lint/type — clean.

## Открытые вопросы

(нет — все решено выше через AskUserQuestion)

## Твой ответ:

(пусто — спека согласована inline)
