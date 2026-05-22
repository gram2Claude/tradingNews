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
python -m src cycle    --company X5   # all three above
python -m src status                  # counts by status per company

# Quality
pytest tests/ -q                                  # 59 tests, all should pass
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
  → fetcher.fetch_all       — walks (company × enabled source), inserts into news
  → analyzer.analyze_all    — picks status='new', calls GPT-5 mini, updates row
  → reporter.report_all     — wipes output/<COMPANY>/news/, regenerates artifacts
```

Each stage commits per logical unit (per-source for fetch, per-row for
analyze, per-company for report). Re-running any stage is idempotent —
`UNIQUE(source_id, url)` dedups news; `status` machine prevents double
analysis; reporter wipes `news/` tree before regen so file numbering is
stable.

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
  within a company.
- **Person frequencies are NEVER stored** — they're computed by SQL agg in
  `reporter._write_persons_csv` against `news.mood` and `news.status='analyzed'`.
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
- `rbc` — **временно остановлен** (`config.yaml: enabled: false`). RSS at
  `rssexport.rbc.ru/rbcnews/news/30/full.rss`. Main rbc.ru закрыт Qrator
  JS-challenge. Жёсткий лимит 30 items / ~7-часовое окно, backfill через
  RSS невозможен. Возвращать только под конкретный use-case.
- `e_disclosure` — **разработка не завершена** (recon в `tests/fixtures/EDISCLOSURE_RECON.md`,
  план `work_directory/02_plans/03_*`, имплементации `src/sources/e_disclosure.py`
  пока нет).

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
