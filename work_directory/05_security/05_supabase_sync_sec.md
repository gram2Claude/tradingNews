# 05 · supabase_sync · CSO audit

**Ветка:** `news_refactoring`
**Дата:** 2026-05-23
**Scope:** SQLite → Supabase Postgres one-way mirror (`src/cloud_sync/`), новые subcommand'ы `init-cloud-db` / `sync-cloud` / cloud-hook в `cycle`, выход за trust boundary к managed Postgres.

---

## Сводка

| Категория | Статус | Комментарий |
| --- | --- | --- |
| SQL injection (SQLite read) | ✅ PASS | `?`-параметризация; `company` единственный user-input, идёт через bind |
| SQL injection (Postgres write) | ✅ PASS | `%s`-параметризация во всех executemany |
| Credential leak (logs) | ✅ PASS | `_mask_db_url` + psycopg pinned INFO + DDL не печатается полностью с creds |
| Credential leak (disk) | ✅ PASS | `SUPABASE_DB_URL` только в `.env` (gitignored), нигде не дублируется |
| Connection security | ✅ PASS | `sslmode="require"` явно на обеих `psycopg.connect` |
| TLS-cert validation | ⚠️ NOTE | `sslmode=require` не проверяет CN сертификата (см. C-3) |
| Trust boundary (SQLite → PG) | ✅ PASS | Read-only mode на SQLite (`file:...?mode=ro`); UPSERT на PG в одной транзакции |
| Code execution (eval/exec/subprocess) | ✅ PASS | Grep — 0 совпадений в `src/cloud_sync/` |
| Schema injection (DDL multi-stmt) | ✅ PASS | `schema.sql` — статика в репо, не из user-input |
| Dependency supply chain | ⚠️ NOTE | `psycopg[binary]>=3.2` без upper bound (см. C-4) |
| Error message leak | ✅ PASS | `cmd_*` печатают `type(exc).__name__: exc` — для local CLI допустимо |
| Race / concurrent push | ⚠️ NOTE | Нет advisory lock'а на push (см. C-5) |

**Вердикт:** **PASS**. Блокирующих security-issues нет. 3 NOTE-уровня для будущих итераций.

---

## Детально

### C-1 · Маскировка пароля в логах — корректно

`src/cloud_sync/pusher.py:46-48`:

```python
def _mask_db_url(url: str) -> str:
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", url)
```

Применяется в обоих info-log'ах (`init_schema`, `push_all`). Регекс корректно покрывает Supabase pooler URI формат (`postgresql://postgres.<ref>:<password>@<host>:6543/postgres`).

**Тесты** `test_mask_db_url_hides_password` и `test_mask_db_url_handles_empty_password` — присутствуют (`tests/test_cloud_sync.py:234-245`). ✅

**Defence-in-depth:** `psycopg` логгер закреплён INFO в `src/cli.py:30`:

```python
logging.getLogger("psycopg").setLevel(logging.INFO)
```

Даже под `-v` (DEBUG) psycopg не выгружает connection string или параметры в лог. ✅

### C-2 · SQL injection — параметризация безупречна

**SQLite-сторона** (`pusher.py:113-118, 152-159, 177-194, 224-241`):
- Все запросы используют `?` placeholder; `company` единственный user-input (CLI arg), биндится. ✅
- SQL конкатенация ограничена `WHERE name = ?` — добавляется одна статичная строка, плейсхолдер всегда `?`. Не пробить. ✅

**Postgres-сторона** (`pusher.py:121-125, 137-143, 166-173, 206-220, 248-254`):
- `executemany("INSERT ... VALUES (%s, ...)", rows)` — psycopg сам параметризует. Никаких f-string'ов с user-data. ✅
- `init_schema(db_url)` принимает URL, не SQL; DDL читается из статичного `schema.sql` файла в репо. Никакого user-input не доходит до DDL. ✅

### C-3 · TLS — `sslmode=require` не проверяет CN сертификата

**Где:** `pusher.py:55, 78`.

`sslmode=require` гарантирует **шифрование** канала, но **не валидирует** что сертификат подписан доверенным CA или что CN/SAN совпадает с `host`. MitM с self-signed cert (например, в hostile WiFi) **технически** пройдёт.

**Threat model:** Supabase pooler resolved через DNS → `*.pooler.supabase.com`. Если attacker подменит DNS + раскатает свой TLS-cert, мы отправим пароль ему. Сценарий низкоprobable (DNS hijack на дом. сети, plus DPI/MitM box), но не нулевой.

**Mitigation:** `sslmode=verify-full` + распределить root CA bundle. Для Supabase pooler требуется качать `prod-ca-2021.crt` и указывать `sslrootcert=...` в connection URI.

**Не блокер.** Threat low, текущая защита — гигиена + ограничение трафика к публичной supabase pooler edge. Поднимаем до P2 если когда-то пойдём через corporate proxy или другой WAN.

### C-4 · Supply chain — `psycopg[binary]>=3.2` без upper bound

**Где:** `requirements.txt:12`.

`psycopg` (Bechtolsheim's psycopg3) — широкоиспользуемый драйвер от автора psycopg2, активная команда maintainer'ов, **в целом стабильная supply chain**. Но `>=3.2` без upper bound означает что `pip install -r requirements.txt` на новой машине через 6 месяцев потянет 3.3 / 3.4 без явного approval'а.

**Threat:** compromised release (захват PyPI-аккаунта, malicious dependency) пройдёт в build без code review. Урон — `psycopg` выполняется внутри `src/cloud_sync/pusher.py` процесса, видит `SUPABASE_DB_URL` (с паролем) и весь read-only поток SQLite-данных.

**Mitigation:**
- Пин на exact version после первого успешного push: `psycopg[binary]==3.2.X` где X — текущая (см. `pip freeze`).
- Аналогичная рекомендация для всех dep'ов в requirements.txt без upper bound — это **общий gap репо**, не специфичный для 05.

**Не блокер.** Bump'ить пины — отдельный пробег TODO, без срочности.

### C-5 · Concurrent push — нет advisory lock'а

**Где:** `cmd_cycle` / `cmd_sync_cloud`.

Если пользователь запускает `python -m src cycle` и `python -m src sync-cloud --company X5` одновременно (например, scheduler + manual), оба процесса откроют свою psycopg-connection и сделают `executemany`. UPSERT'ы коммутативны (одна и та же строка → одно и то же финальное состояние), но:

- `executemany` на каждой стороне начинается с одного и того же snapshot'а SQLite, идёт в свою transaction Postgres'а;
- если row.A коммитит первый, row.B (slightly different fetched_at) перетрёт его — last-writer-wins;
- consistency не нарушается, но WARN-уровень race в `fetched_at` колонке (миллисекунды разные).

Не security-issue в классическом смысле (нет escalation), но потенциально ломает атомарность mirror. Mitigation — `pg_advisory_xact_lock(hashtext('trading_news.push'))` в начале транзакции в `_push_inner`. P3-инфо в Claude review (P3.2/P3.4) уже косвенно об этом.

### C-6 · Secrets-on-disk — `.env` защищён

```
.gitignore:
  .env
  .env.local
  config.yaml
```

`SUPABASE_DB_URL` живёт **только** в `.env`. `.env.example` содержит placeholder без real-creds (`postgresql://postgres.PROJECT_REF:PASSWORD@...`). Проверил:

```bash
git log --all -p -S "SUPABASE_DB_URL=postgresql" | wc -l
```

Pull request не вытащит реальный creds — он не в репо. ✅

### C-7 · `init_schema` не атомарен, но и не вреден

`pusher.py:51-59` шлёт весь DDL одним `cur.execute(ddl)`. Если 3-я CREATE TABLE упадёт (например, при апгрейде схемы и колонка уже изменена), Postgres откатит транзакцию (`with psycopg.connect(...)` — atomic commit/rollback). Это **не** оставит частично-созданный schema state. ✅

Однако: если DDL содержит **destructive** statements (DROP, ALTER, TRUNCATE), они выполнятся в той же транзакции. Сейчас `schema.sql` — только `CREATE ... IF NOT EXISTS`, безопасно. Когда введём миграции (next iteration), это станет острее. **Рекомендация:** для будущих миграций ввести явный versioning по аналогии с `PRAGMA user_version` в SQLite — отдельная таблица `trading_news.schema_version`, миграции в `migrations/NN_*.sql`.

### C-8 · `cmd_cycle` ошибки cloud-push не валят cycle — это by design

`src/cli.py:126-133` ловит `Exception` и продолжает с exit 0. Это документировано (CLAUDE.md → Architecture → "Cloud mirror is opt-in via SUPABASE_DB_URL"). Сценарий безопасен:

- network down → WARNING, cycle ok;
- bad creds → WARNING, cycle ok (пользователь чинит `.env`, следующий cycle догонит);
- Postgres CHECK violation на одной строке → весь batch откатывается, WARNING, cycle ok.

В **none** из этих случаев local state не повреждается. ✅

См. Claude-review P2.3 — рекомендация **сузить catch до `(psycopg.Error, OSError)`**. Это не security (любой Exception в pusher'е не leak'ает creds — `_mask_db_url` уже применён в lifecycle log'ах), а observability fix.

### C-9 · Postgres → SQLite — write возможен?

Ответ: **нет**. SQLite-side connection открыт через

```python
sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
```

`mode=ro` — read-only, любые `INSERT/UPDATE/DELETE` на src упадут с `sqlite3.OperationalError: attempt to write a readonly database`. **Trust boundary жёсткий, никакого случайного write-через-cloud'а на local.** ✅

### C-10 · CLI subcommand'ы — input validation

`cmd_init_cloud_db`, `cmd_sync_cloud`: оба читают `SUPABASE_DB_URL` из env, передают в `psycopg.connect`. Никакого user-input в URL — псевдо-immutable.

`args.company` (для sync-cloud) → проходит как параметр в SQLite-запрос с `?`-bind. SQL-injection невозможна.

`load_dotenv(PROJECT_ROOT / ".env")` в `main()`: path — статика, не user-input. ✅

---

## Рекомендации (ни одна не блокирует ship)

1. **C-4** — закрепить `psycopg[binary]==<exact>` в `requirements.txt` после первого успешного e2e push'а.
2. **C-3** — рассмотреть `sslmode=verify-full` + `sslrootcert=<bundle>` если когда-то пойдём не из дом. сети.
3. **C-7** — ввести `trading_news.schema_version` table перед первой destructive миграцией.
4. **C-5** — `pg_advisory_xact_lock` в `_push_inner` если когда-то будут гнаться concurrent cycle и sync-cloud.
5. **C-8** (cross-ref Claude P2.3) — сузить except до `(psycopg.Error, OSError)` для лучшей наблюдаемости non-network багов.

**Gate:** PASS.
