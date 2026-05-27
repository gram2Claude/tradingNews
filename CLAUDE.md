# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Bootstrapping a new task or project?** Read `WORKFLOW.md` — it describes the
> 6-phase pipeline (spec → plan → estimate → recon → impl → review → ship),
> gstack prerequisites, and the artifact naming convention used throughout.
> Deferred work tracked in `TODOS.md`.

## Project artifact convention

The user organises planning artifacts in five parallel folders under
**`work_directory/`** with **matching numeric prefixes** so any task's spec /
plan / estimate / reviews / audit can be cross-referenced at a glance:

| Folder                          | Naming                          | Purpose                                           |
| ------------------------------- | ------------------------------- | ------------------------------------------------- |
| `work_directory/01_specs/`         | `NN_<slug>_spec.md`             | Task specification (problem, decisions, scope)    |
| `work_directory/02_plans/`         | `NN_<model>_<slug>_plan.md`     | Implementation plan per AI (claude, codex critiques) |
| `work_directory/03_estimates/`     | `NN_<model>_<slug>_est.md`      | Plan critique/estimate per AI (claude self-review, codex consult) |
| `work_directory/04_reviews/`       | `NN_<model>_<slug>_rew.md`      | Pre-landing code review per AI (claude, codex)    |
| `work_directory/05_security/`      | `NN_<slug>_sec.md`              | CSO audit                                         |

All artifacts for one task share the same `NN` and the same `<slug>`. The type
suffix (`_spec`, `_plan`, `_est`, `_rew`, `_sec`) makes the artifact's role
obvious from the filename alone, so files stay self-describing when moved /
referenced out of folder context. The user fills in answers directly inside
spec / estimate / review files (under "Твой ответ:" or "Решение:" markers) —
don't duplicate the conversation in chat once a file exists, edit it in place.

**Архив:** задачи, развитие которых остановлено, переезжают в
`work_directory/_archive/<подпапка>/` с сохранением структуры подпапок и имени
файла (`_archive/01_specs/02_rbc_news_spec.md` и т.д.). Сейчас в архиве лежат
артефакты задач `02_rbc_news` и `03_e_disclosure`.

## Commands

```powershell
# One-time setup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy config.example.yaml config.yaml
copy .env.example .env             # then put real OPENAI_API_KEY into .env
python -m src init-db              # creates data/db.sqlite + seed persons

# Daily use
python -m src fetch    --company X5
python -m src analyze  --company X5
python -m src report   --company X5
python -m src cycle    --company X5   # all three above + cloud push (if SUPABASE_DB_URL set)
python -m src status                  # counts by status per company

# Cloud mirror (optional, off if SUPABASE_DB_URL absent in .env)
python -m src init-cloud-db            # apply DDL to Supabase Postgres (idempotent, one-off)
python -m src sync-cloud --company X5  # standalone push SQLite → Supabase

# Quality
pytest tests/ -q                                  # 178 tests, all should pass
pytest tests/test_analyzer.py::test_happy_path    # single test
python -m ruff check src/ tests/                  # lint (auto-fix: --fix)
python -m mypy src/ --ignore-missing-imports      # types
python -m coverage run --source=src -m pytest && python -m coverage report
```

## Health Stack

Commands invoked by `/health`. Keep in sync with the "Quality" block above.

- typecheck: `python -m mypy src/ --ignore-missing-imports`
- lint: `python -m ruff check src/ tests/`
- test: `python -m pytest tests/ -q`

## Ship sequence

Pre-ship gates: `/review` + `/codex review` + `/cso` + `/health` — all PASS.
Then the user (NOT Claude) runs in their terminal:

```powershell
git add <specific files, NOT -A without inspection>
git commit -m @'<russian, lowercase title>'@
git push -u origin <feature-branch>
gh pr create  # or via GitHub UI
# After merge:
git checkout master && git pull && git branch -d <feature>
```

Double-click `run.bat` runs `python -m src cycle` with `pause` at the end.

**Auto-run is intentionally OFF** (`auto_run: false` in `config.yaml`). No
Task Scheduler job is registered. README has the manual setup steps for when
the user explicitly enables it.

## Architecture

CLI tool, no web layer. Single-process per invocation. SQLite is the source
of truth; everything in `output/` is regenerated from it.

### Data flow

```
[Task Scheduler / manual] → cli.py → cmd_cycle
  → fetcher.fetch_all       — walks (company × enabled source), dispatches RawItems
                              into `news` ИЛИ `recommendations` по Source.item_destination
  → analyzer.analyze_all    — два прохода (news, потом recommendations),
                              два SYSTEM_PROMPT'а, два error-marker'а
  → reporter.report_all     — wipes output/<COMPANY>/{news,recommendations}/,
                              регенерирует UNION-ом из обеих таблиц
  → cloud_sync.push_all     — UPSERT 7 tables to Supabase в строгом порядке
                              (companies → sources → persons → news →
                               recommendations → news_persons → recommendation_persons),
                              if SUPABASE_DB_URL set
```

Each stage commits per logical unit (per-source for fetch, per-row for
analyze, per-company for report). Re-running any stage is idempotent —
`UNIQUE(source_id, url)` dedups news и recommendations отдельно; `status`
machine prevents double analysis; reporter wipes `news/` И `recommendations/`
trees перед regen so file numbering is stable.

### Key invariants

- **`news.published_at` is always UTC ISO with offset**. Reporter converts to
  `Europe/Moscow` (`config.global.timezone`) before forming `YYYY_MM` paths
  and `yyyy_mm_dd` filenames. The trading day must follow Moscow, not UTC —
  there is a test for the month-boundary case.
- **`news.status` ∈ `{new, analyzed, error}`**. `error` is permanent;
  `new + retry_count >= MAX_ATTEMPTS=3` is "stuck transient" (won't be
  retried in the loop until reset).
- **`news.mood` ∈ `{pos, neutral, neg}`** when status='analyzed'. Validated
  against a whitelist after JSON parse; invalid → status='error'.
- **Persons matching uses pymorphy3 lemmas of surnames.** Seed list lives in
  `seed/x5_persons.csv`; LLM does not extract persons — `name_matcher` runs
  on `headline + body` after the LLM call. Surnames are assumed unique
  within a company. Связь хранится в **двух junction-таблицах**:
  `news_persons` (для news) и `recommendation_persons` (для recommendations) —
  analyzer диспатчит INSERT по тому, какой path обрабатывается.
- **Person frequencies are NEVER stored** — they're computed by SQL agg in
  `reporter._write_persons_csv` через CTE `all_mentions` (UNION над обоими
  junction-источниками) против `mood` analyzed-строк.
- **News vs recommendations — две отдельные таблицы (v3, задача 06; δ-completion v4, задача 08).**
  `news` — пресс-релизы (x5_ir) и новости (finam без рекомендаций).
  `recommendations` — отдельная таблица со структурными полями (`target_price`,
  `recommendation_action`, `potential_pct`, `multipliers_json`); туда пишут
  recommendation-only источники (lmsic) **и** finam-рекомендации после
  per-item dispatch в analyzer'е (когда LLM классифицирует item как
  recommendation). Колонка `news.item_type` удалена в δ-completion (v4).
- **`Source.item_destination` enum** управляет диспатчем в `fetcher._insert`:
  `NEWS` (default — x5_ir/finam) или `RECOMMENDATIONS` (lmsic). Для finam
  диспатч в recommendations происходит на стадии **analyzer'а** (per-item
  через `_dispatch_news_to_recommendations`, атомарно SAVEPOINT'ом).
- **Cloud mirror is opt-in via `SUPABASE_DB_URL` in `.env`**. Absent → `cycle`
  silently skips push. Present → push runs after reporter; any psycopg/network
  error is logged at WARNING, cycle exits 0. The cloud copy is one-way,
  read-only from app side; push **additive-only** (UPSERT по PK): правки
  существующих строк в Supabase UI затрутся; удаления в SQLite не удаляют
  строки в облаке; новые строки, вставленные в Supabase UI, push'ем не трогаются.
  Денормализация: Postgres ключует строки натуральными ключами
  (`companies.name`, `sources.code`, `news.(source_code, url)`), не суррогатными
  SQLite `id` — id'шки SQLite drift'ятся между машинами.
- **Keyword filter at fetch stage** (для shared news streams вроде RSS РБК):
  prefilter by strong keywords (aliases + brands) before insertion. Weak-only
  matches (bare surname) are rejected to avoid homonymy. Применяется в `rbc`
  (сейчас остановлен) через `src/sources/rbc.py:_keyword_match` с pymorphy3
  для русских declensions. Для finam используется более узкий **slug
  relevance filter** — анализ ASCII-transliterated slug'а на токены
  (`pyaterochka`, `iks-5`, `chizhik`) без LLM.

### Error handling tiers (analyzer)

1. **Global config errors** (`AuthenticationError`, `PermissionDeniedError`,
   `NotFoundError`, `BadRequestError`) → raise `_GlobalConfigError` →
   `analyze_all` breaks the batch without touching any row. User fixes
   config, reruns, rows pick up cleanly.
2. **Transient** (`RateLimitError`, `APIConnectionError`, `APITimeoutError`,
   `InternalServerError`) → tenacity retries 3× with exp backoff; if all
   three fail, row stays `status='new'` with `retry_count=3` (loop-level
   filter skips until user resets via SQL).
3. **Other `APIError`** / **JSON parse errors** → per-row terminal,
   `status='error'`.

### Source abstraction

`src/sources/base.py:Source` is the ABC. New source = subclass `Source`,
register in `fetcher.SOURCE_REGISTRY`, add an entry to `config.yaml.sources`,
optionally add the code to `companies[X].sources`. Sources own a persistent
`httpx.Client`; `Source.__enter__/__exit__` handles teardown.

**`FetchContext`** (in `base.py`) is passed via `Source.__init__(.., context=)`
— carries `company_cfg`, `company_id`, `source_id`, `db_path`. Sources that
need company-specific data (keyword filter, brand / surname lookup) consume
the context; sources that don't (`x5_ir` — single-company site) ignore it.
`FetchContext.load_keywords()` returns `Keywords(strong, weak)` where
strong = aliases + brands, weak = surnames. Matchers should pass on
strong-only and reject weak-only (avoids surname homonymy — see
`work_directory/04_reviews/02_*_rew.md`).

**Recon before architecture.** Any new source touching an external site
must first produce `tests/fixtures/<SRC>_RECON.md` documenting endpoint
URL, response shape, anti-bot behavior, and selectors. The plan for a new
source is written only after recon — see how `02_rbc_news` pivoted from
HTML search to RSS after recon found Qrator JS-challenge on www.rbc.ru.

**Sources today:**
- `x5_ir` — **активен**. WordPress press-releases at `/ru/press-center/press-releases/page/N/`
  (sitemap lags months — confirmed unusable). HTML scraper, per-article fetch.
  Meta tags: `og:title`, `article:published_time`; body: `.content` block,
  прогнан через `_clean_text`.
- `finam` — **активен**. `finam.ru/quote/moex/{ticker}/publications/` через
  Playwright + stealth (`PlaywrightSource`). Listing → URL date filter →
  slug relevance filter (отсекает broad-market мусор: SpaceX, ETH, золото) →
  per-article fetch. Headline из `og:title`, published_at из URL pattern
  `-YYYYMMDD-HHMM/` (meta `article:published_time` finam НЕ ставит).
  Body: контейнер `[class*="finfin-local-plugin-publication-item-item"]`
  через selectolax, whitelist по class (`bold font-xl` + `p-margin`) —
  отбрасывает price ticker, "Купить на демосчёт", AI-инсайты, social footer.
  См. `tests/fixtures/FINAM_RECON.md`.
- `rbc` — **архивирован** (`config.yaml: enabled: false`, артефакты в
  `work_directory/_archive/`). Код в `src/sources/rbc.py` оставлен. RSS at
  `rssexport.rbc.ru/rbcnews/news/30/full.rss`. Main rbc.ru закрыт Qrator
  JS-challenge. Жёсткий лимит 30 items / ~7-часовое окно, backfill через
  RSS невозможен. Возвращать только под конкретный use-case.
- `e_disclosure` — **архивирован, имплементации нет** (recon в
  `tests/fixtures/EDISCLOSURE_RECON.md`, spec/plan в
  `work_directory/_archive/{01_specs,02_plans,03_estimates}/03_*`,
  `src/sources/e_disclosure.py` отсутствует).

### Body cleaning convention

Все парсеры пропускают извлечённый текст (headline и body) через `_clean_text`
перед возвратом `RawItem` — этот хелпер живёт в каждом source-модуле параллельно
(`x5_ir._clean_text`, `finam._clean_text`):

- `html.unescape` для `&nbsp;`, `&quot;`, `&mdash;` и пр.
- замена NBSP / narrow-NBSP / figure-space на обычный пробел
- удаление control-символов (кроме `\n` и `\t`)
- сжатие горизонтальных пробелов, не более одной пустой строки подряд

**Чистка обязательна** — без неё в БД залетает HTML / JS / виджет-мусор, токены
LLM улетают на нерелевантный текст, а LLM начинает «отвлекаться» (видно по mood_reason).
Для finam критично: до whitelist по class body был ~27 КБ против ~2 КБ реального
текста. См. `_extract_body` в `src/sources/finam.py` и `parse_article` в
`src/sources/x5_ir.py`.

**Любой новый source** обязан:
1. Применять `_clean_text` к headline и body перед `RawItem(...)`.
2. Если HTML-источник — использовать `selectolax` + whitelist по селекторам
   (не regex по тегам; regex не справится с виджетами и JS внутри контейнера).

### Security guardrails baked in

- httpx logger pinned to INFO even under `-v` (avoids Authorization header
  leak at DEBUG).
- `X5IRSource._client` uses `follow_redirects=False`; `_http_get` manually
  follows up to 3 hops, validates each Location host against
  `{www.x5.ru, x5.ru}`. Closes the SSRF-via-302 vector.
- `analyzer.SYSTEM_PROMPT` explicitly instructs the LLM to treat news text
  as data, not commands — defence against prompt-injection in news bodies.
- All SQLite queries are parameterised. No `subprocess` / `eval` / `exec`
  anywhere in `src/`.
- `error_msg` stores only the exception class name, not the full message —
  avoids leaking diagnostic detail to disk.
- **XML parsing uses `defusedxml`**, not stdlib `xml.etree` — protects RSS
  / XML sources from XXE / billion-laughs / external-DTD attacks (PSF
  recommendation).
- **`FeedParseError` (in `rbc.py`) on whole-feed failures** — broken XML,
  missing `<channel>`, HTML interstitial with 200. Propagates as
  `errors += 1` in `_fetch_one` so a silently dead source doesn't masquerade
  as a successful empty fetch.
- **Keyword filter uses strong/weak split + pymorphy3 lemmatization** —
  strong terms (aliases, brands) alone pass; weak terms (surnames) alone
  reject (homonymy defense — `Гусев` from another region doesn't trigger).
  Russian declensions caught via lemma match (`Пятёрочки`, `Шехтермана`
  match their nominative form).
- `.gitattributes` enforces `eol=lf` repo-wide — avoids CRLF warnings on
  Windows and cross-OS diff noise. `*.bat` keeps CRLF intentionally.
- **`psycopg` logger pinned to INFO** in `cli._setup_logging` — DEBUG would
  log parameter values and connection strings (incl. password). All
  cloud_sync SQL uses `%s` parameterisation; never f-string interpolation.
- **`SUPABASE_DB_URL` lives only in `.env`** (gitignored). `pusher._mask_db_url`
  redacts the password before any log line, so masked URL is the only form
  ever written to stdout/log file.

### Cloud sync (`src/cloud_sync/`)

One-way SQLite → Supabase Postgres mirror. See README → "Облачное зеркало".

- **Schema isolation:** all tables in `trading_news.*` (own Postgres schema),
  not `public.*` — the Supabase project hosts other workloads (n8n + RAG
  embeddings) and we mustn't collide with their names like `news`.
- **Natural keys**: Postgres uses `companies.name`, `sources.code`,
  `news.(source_code, url)`, `recommendations.(source_code, url)` as primary
  keys. Junction-таблицы используют composite PK
  `(source_code, url, company_name, person_full_name)`. SQLite surrogate
  `id`s never cross the boundary — they aren't portable between machines.
- **Trigger**: invoked from `cmd_cycle` after `reporter.report_all` succeeds,
  guarded by `if os.environ.get("SUPABASE_DB_URL")`. Exceptions are caught
  and logged at WARNING, cycle exits 0 — local pipeline is the source of
  truth, cloud is best-effort.
- **Standalone**: `python -m src sync-cloud [--company X5]` for off-cycle pushes.
  Returns non-zero on push failure (unlike the cycle hook).
- **Connection**: Supabase pooler (port 6543, Transaction mode). Direct
  connection (5432) is IPv6-only and unusable from typical IPv4 home networks.

## Modules at a glance

`src/` is a flat layout — one file per pipeline stage. Subpackages only where
plurality is real (`sources/`, `cloud_sync/`). Cross-cutting helpers
(`text_cleanup`, `name_matcher`, `models`) stay at the top level.

- **`cli.py`** — argparse entrypoint. Subcommands: `init-db`, `fetch`, `analyze`,
  `report`, `cycle`, `init-cloud-db`, `sync-cloud`, `status`. Owns logging
  setup (`_setup_logging` — pins `httpx`/`httpcore`/`psycopg` to INFO).
  `cmd_cycle` wires the whole pipeline + optional cloud push.
- **`__main__.py`** — `python -m src` entrypoint; defers to `cli.main`.
- **`config.py`** — `Config`, `CompanyCfg`, `SourceCfg` dataclasses + `load_config()`.
  Reads `config.yaml` + `.env`. `PROJECT_ROOT` exported here as the canonical
  filesystem anchor — never hardcode paths elsewhere.
- **`db.py`** — SQLite schema (`SCHEMA_SQL`), `init_db`, `ensure_migrated`,
  `connect`. Migration uses `PRAGMA user_version` (currently **v4**; v1→v2
  added `news.item_type`; v2→v3 добавил таблицы `recommendations` +
  `recommendation_persons`; v3→v4 δ-completion перенёс finam-recs из
  news.item_type='recommendation' в recommendations table и удалил колонку
  через rebuild). Все миграции транзакционные через `SAVEPOINT` —
  `with conn:` не работает для DDL в Python sqlite3 legacy mode.
  `status_counts` возвращает UNION над обеими таблицами с колонкой `kind`.
- **`models.py`** — `Company`, `Source`, `NewsItem`, `Recommendation`, `Person`
  dataclasses (lightweight read models). NOT ORM-mapped; SQLite rows are
  plain dicts via `sqlite3.Row`.
- **`fetcher.py`** — orchestrates fetch stage. `SOURCE_REGISTRY` maps source
  code → class. `fetch_all` walks `(company × enabled source)`, opens each
  Source as a contextmanager. `_insert` — dispatcher по `source.item_destination`,
  два helper'а `_insert_into_news` / `_insert_into_recommendations`
  с `INSERT OR IGNORE`.
- **`analyzer.py`** — LLM analysis stage. Два независимых path'а:
  `_analyze_news` (с `SYSTEM_PROMPT_NEWS`, парсит item_type; для
  `item_type='news'` UPDATE news + INSERT news_persons; для
  `item_type='recommendation'` **per-item dispatch** через
  `_dispatch_news_to_recommendations` — атомарный SAVEPOINT перенос
  news-row → recommendations table + junction news_persons →
  recommendation_persons + DELETE из news; UPSERT по `(source_id, url)`
  для повторных анализов) и `_analyze_recommendation` (с
  `SYSTEM_PROMPT_RECOMMENDATION` — урезанный, без item_type,
  UPDATE recommendations + INSERT recommendation_persons).
  Два error-marker'а (`_mark_news_error`, `_mark_recommendation_error`).
  `analyze_all` — два прохода последовательно; global config error на news-pass
  прерывает batch до recs-pass; на recs-pass — news уже закоммичены.
  3-tier error handling — see "Error handling tiers" above.
- **`name_matcher.py`** — pymorphy3-based surname matching against company
  seed lists. Runs on `headline + body` after LLM call. Pure function, no
  state, no network.
- **`reporter.py`** — generates Obsidian MD + `data.xlsx` + `persons.csv`
  from SQLite. Wipes `output/<COMPANY>/{news,recommendations}/` перед regen
  для deterministic file numbering. После δ-completion (v4): два независимых
  SELECT (news + recommendations), без UNION; склейка в Python с
  `_src_table` (folder routing). `data.xlsx` с **двумя листами**:
  `news` (только настоящие новости из news table; без колонки item_type)
  и `recommendations` (все рекомендации со структурными колонками).
  YAML frontmatter не содержит ключей для `NULL` значений (target_price
  etc отсутствуют у legacy finam-recs). Timezone UTC → Europe/Moscow happens here.
- **`text_cleanup.py`** — `clean_text()` and `sanitize_inline_code()`.
  Shared utilities for normalising news text downstream of fetch (used by
  fetcher/analyzer/reporter). **Different from** `_clean_text` inside each
  source module — those run at extraction time on raw HTML; `text_cleanup`
  runs on already-cleaned text further down the pipeline.
- **`sources/`** — one file per news provider (`x5_ir`, `finam`, `rbc`),
  `base.py` (ABC + `FetchContext` + `RawItem`), `playwright_base.py`
  (`PlaywrightSource` mixin for Cloudflare/WAF sites — used by finam).
- **`cloud_sync/`** — `pusher.py` (`push_all`, `init_schema`, `PushStats`,
  `_mask_db_url`) + `schema.sql` (Postgres DDL for `trading_news.*` schema —
  7 таблиц). Connects via Supabase pooler; reads SQLite read-only, UPSERTs
  Postgres in one transaction в **строгом порядке**: companies → sources →
  persons → news → recommendations → news_persons → recommendation_persons.
  Junctions последними чтобы FK были satisfied.

## Style preferences (learned over the build)

- Russian for any user-facing artifact (specs, plans, reviews, commit
  messages). Code identifiers and docstrings — English.
- File names use Cyrillic slugs (`х5_покупает_дистрибьютора_вкт`); NTFS and
  Obsidian handle this natively. **Do not** add a transliteration library.
- The user writes `git commit -m` messages in Russian; don't translate them.
- Commits land via the user in their own terminal — don't run `git commit`
  unless explicitly asked.
- Defense-in-depth fixes are welcome but every "informational" finding
  needs a concrete exploit scenario, not just a pattern match.
