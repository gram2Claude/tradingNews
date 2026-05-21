# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project artifact convention

The user organises planning artifacts in five parallel folders with **matching
numeric prefixes** so any task's spec / plan / estimate / reviews / audit can
be cross-referenced at a glance:

| Folder        | Naming                          | Purpose                                           |
| ------------- | ------------------------------- | ------------------------------------------------- |
| `specs/`      | `NN_<slug>_spec.md`             | Task specification (problem, decisions, scope)    |
| `plans/`      | `NN_<model>_<slug>_plan.md`     | Implementation plan per AI (claude, codex critiques) |
| `estimates/`  | `NN_<model>_<slug>_est.md`      | Plan critique/estimate per AI (claude self-review, codex consult) |
| `reviews/`    | `NN_<model>_<slug>_rew.md`      | Pre-landing code review per AI (claude, codex)    |
| `security/`   | `NN_<slug>_sec.md`              | CSO audit                                         |

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
register in `fetcher.SOURCE_REGISTRY`, add an entry to `config.yaml`
sources. Sources own a persistent `httpx.Client`; `Source.__enter__/__exit__`
handles teardown.

`x5_ir` discovery channel: WordPress press-releases listing
`/ru/press-center/press-releases/page/N/` (NOT the news sitemap — it lags
months on x5.ru). Article fields come from meta tags (`og:title`,
`article:published_time`) and `.content` block.

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
