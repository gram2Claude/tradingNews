# 05 — Supabase sync — Plan (Claude)

Спека: `work_directory/01_specs/05_supabase_sync_spec.md`.

## Этапы

### T1 — Зависимость + .env

1. `requirements.txt`: добавить `psycopg[binary]>=3.2`.
2. `.env.example`: добавить
   ```
   # Supabase: Settings → Database → Connection string (URI)
   # Пароль — Database password из того же раздела (не путать с anon/service_role JWT)
   SUPABASE_DB_URL=postgresql://postgres:PASSWORD@db.<project-ref>.supabase.co:5432/postgres
   ```
3. Установить локально: `pip install psycopg[binary]`.

### T2 — DDL и `init-cloud-db`

1. `src/cloud_sync/__init__.py` — пустой пакет-маркер.
2. `src/cloud_sync/schema.sql`:
   ```sql
   CREATE TABLE IF NOT EXISTS companies (
     code TEXT PRIMARY KEY,
     name TEXT NOT NULL,
     enabled BOOLEAN NOT NULL DEFAULT TRUE,
     created_at TIMESTAMPTZ NOT NULL DEFAULT now()
   );

   CREATE TABLE IF NOT EXISTS sources (
     code TEXT PRIMARY KEY,
     name TEXT NOT NULL,
     enabled BOOLEAN NOT NULL DEFAULT TRUE,
     created_at TIMESTAMPTZ NOT NULL DEFAULT now()
   );

   CREATE TABLE IF NOT EXISTS persons (
     company_code TEXT NOT NULL REFERENCES companies(code) ON DELETE CASCADE,
     surname TEXT NOT NULL,
     full_name TEXT,
     role TEXT,
     created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
     PRIMARY KEY (company_code, surname)
   );

   CREATE TABLE IF NOT EXISTS news (
     source_code TEXT NOT NULL REFERENCES sources(code) ON DELETE CASCADE,
     url TEXT NOT NULL,
     company_code TEXT NOT NULL REFERENCES companies(code) ON DELETE CASCADE,
     headline TEXT NOT NULL,
     body TEXT,
     published_at TIMESTAMPTZ NOT NULL,
     fetched_at TIMESTAMPTZ NOT NULL,
     status TEXT NOT NULL CHECK (status IN ('new','analyzed','error')),
     retry_count INTEGER NOT NULL DEFAULT 0,
     mood TEXT CHECK (mood IN ('pos','neutral','neg')),
     item_type TEXT,
     mood_reason TEXT,
     analyzed_at TIMESTAMPTZ,
     error_msg TEXT,
     tokens_input INTEGER,
     tokens_output INTEGER,
     PRIMARY KEY (source_code, url)
   );

   CREATE INDEX IF NOT EXISTS idx_news_company_published
     ON news (company_code, published_at DESC);
   CREATE INDEX IF NOT EXISTS idx_news_status
     ON news (status);
   ```
3. `src/cloud_sync/pusher.py:init_schema(conn)` — читает schema.sql,
   выполняет одним батчем.
4. `src/cli.py:cmd_init_cloud_db()` — `argparse` подкоманда. Читает
   `SUPABASE_DB_URL` из env, подключается, зовёт `init_schema`. Логи:
   `cloud_sync: schema applied to db.<ref>.supabase.co`.

### T3 — Push логика

`src/cloud_sync/pusher.py`:

```python
def push_all(sqlite_path: Path, db_url: str, company: str | None = None) -> PushStats:
    """Pushes companies/sources/persons/news from SQLite to Postgres.

    Filter by company.code if provided; otherwise everything.
    Returns counts: companies/sources/persons/news_new/news_updated.
    """
```

Алгоритм:
1. Открываем SQLite read-only через `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`.
2. Открываем psycopg connection. autocommit=False, одна транзакция на push.
3. Читаем `companies` (с фильтром если задан) → собираем `code, name, enabled`.
   Bulk upsert через `executemany`:
   ```sql
   INSERT INTO companies (code, name, enabled) VALUES (%s, %s, %s)
   ON CONFLICT (code) DO UPDATE SET name=EXCLUDED.name, enabled=EXCLUDED.enabled;
   ```
4. Аналогично `sources`.
5. `persons`: JOIN с companies в SQLite, получаем `(company_code, surname, full_name, role)`.
   Upsert по `(company_code, surname)`.
6. `news`: JOIN с companies+sources в SQLite, получаем все 16 полей. Upsert
   по `(source_code, url)`.
   - Возвращаем counts: `result.rowcount` от psycopg по факту INSERT vs UPDATE
     через `RETURNING xmax = 0 AS inserted` aggregate.
7. `conn.commit()`. Любое исключение → `conn.rollback()` + reraise.

Логирование: после каждой таблицы `cloud_sync: companies +N`,
`cloud_sync: news +N new, M updated`.

### T4 — Интеграция в CLI

1. `src/cli.py:cmd_sync_cloud(args)` — standalone команда. Принимает
   `--company`, опционально. Падает с exit 1 на любой psycopg.Error.
2. `src/cli.py:cmd_cycle` — после `reporter.report_all`:
   ```python
   if os.environ.get("SUPABASE_DB_URL"):
       try:
           stats = cloud_sync.push_all(db_path, os.environ["SUPABASE_DB_URL"], company=args.company)
           log.info("cloud_sync: %s", stats)
       except Exception as exc:
           log.warning("cloud_sync skipped: %s", type(exc).__name__)
   else:
       log.debug("cloud_sync: SUPABASE_DB_URL not set, skipping")
   ```
   **Если переменной нет — silent skip.** Это даёт пользователям без Supabase
   запускать cycle без warnings.

### T5 — Тесты

`tests/test_cloud_sync.py`:

1. `test_init_schema_idempotent` — mock psycopg connection, проверяем
   что schema.sql выполняется без ошибок при повторном вызове.
2. `test_push_all_empty_db` — SQLite с только seed данными (companies,
   sources, persons), без news. Mock psycopg. Проверяем что upsert'ы
   на companies/sources/persons вызваны, на news — нет.
3. `test_push_all_with_news` — SQLite с 3 строками news. Проверяем
   правильный SQL и параметры в `executemany`.
4. `test_push_company_filter` — `company='X5'` фильтрует и не пушит
   данные других компаний (если они есть).
5. `test_push_rolls_back_on_error` — middleware raises на 2-й таблице,
   проверяем `conn.rollback()` вызван.
6. `test_cycle_skips_when_env_unset` — `monkeypatch.delenv("SUPABASE_DB_URL")`,
   `cmd_cycle` отрабатывает без вызова cloud_sync.

Mock через `pytest`'s `monkeypatch` + dummy class с `executemany`,
`commit`, `rollback`, `close`. **Не используем testcontainers** —
оверхед на CI/локалке не оправдан для one-way push.

### T6 — Docs

1. `README.md`: новый раздел «Облачное зеркало (Supabase)» с инструкцией:
   - где взять `SUPABASE_DB_URL`;
   - `python -m src init-cloud-db` (один раз);
   - что `cycle` теперь авто-пушит если env установлен;
   - предупреждение: правки в Supabase UI будут затёрты.
2. `CLAUDE.md`:
   - В «Commands» добавить `init-cloud-db` и `sync-cloud`.
   - В «Architecture / Data flow» добавить `cloud_sync.push_all` шаг.
   - В «Key invariants»: один пункт — «`SUPABASE_DB_URL` отсутствие =
     silent skip, не warning».
   - В «Security guardrails»: `psycopg` параметризация, маскинг
     connection string в логах.

## Файлы

- `requirements.txt` — добавить psycopg.
- `.env.example` — плейсхолдер.
- `src/cloud_sync/__init__.py` — новый.
- `src/cloud_sync/schema.sql` — новый.
- `src/cloud_sync/pusher.py` — новый, ~150 LOC.
- `src/cli.py` — добавить 2 подкоманды + интеграция в cycle.
- `tests/test_cloud_sync.py` — новый, ~200 LOC.
- `README.md` — раздел.
- `CLAUDE.md` — обновить 3 секции.

## Риски

| Риск | Митигация |
|------|-----------|
| Supabase free tier paused при бездействии | Просто переподключается после возобновления, идемпотентно |
| Пароль БД попадает в логи | Маскинг в логгере; psycopg сам не логирует connection string |
| `service_role` ключ утечёт | Лежит только в .env (gitignored); ротация через Supabase dashboard |
| Schema drift между SQLite и Postgres | Один источник DDL (`schema.sql` для PG, `src/db.py:SCHEMA` для SQLite). Тест проверяет совпадение колонок |
| `executemany` падает на 10k+ строк | На ~16-1000 строк ок. Если разрастётся — переход на `COPY FROM` (P3 TODO) |

## Оценка времени

- T1 (deps + env): 5 мин
- T2 (DDL + init-cloud-db): 30 мин
- T3 (push логика): 60 мин
- T4 (CLI интеграция): 20 мин
- T5 (тесты): 60 мин
- T6 (docs): 20 мин
- **Итого: ~3.5 часа** (для одного эстимейта пользователя в реальном времени; для CC ≈ 30-40 мин).

## Out of scope (записать в TODOS.md если всплывёт)

- COPY FROM для больших батчей.
- RLS policies + anon key путь.
- Two-way sync.
- Real-time subscriptions (для будущего dashboard).
