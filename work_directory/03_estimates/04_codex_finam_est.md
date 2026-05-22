# Estimate 04 (codex critique) — атака на `plans/04_claude_finam_plan.md`

Источник: `/codex consult` (gpt-5.5, model_reasoning_effort=medium)
Дата: 2026-05-21
Цель: вскрыть слабые места плана 04 (finam + Playwright + item_type) **до старта T8.2**.
Связанный план: `plans/04_claude_finam_plan.md`
Связанная спека: `specs/04_finam_spec.md`

GATE: **FAIL** (7 P1, в том числе противоречие с твоим решением P2 «no filter»)

---

## ⚠️ Главное противоречие — P1.1

**Codex прочитал реальный HTML фикстуры `finam_x5_publications.html` и нашёл:**
> «листинг содержит broad-market мусор: **SpaceX, Bill Ackman / Amazon / Microsoft, escrow страховку, золото, Ethereum, T-Technologies**. То есть `/quote/moex/x5/publications/` это **не чистый issuer feed**».

Ты в P2 выбрал «без фильтра — берём все 70». Это решение основывалось на допущении что страница «attached to X5 sector». **Это допущение неверно** — фактически это **общий финансовый поток с слабой привязкой к X5**.

Последствия твоего P2.A в полной мере:
- 70 items × LLM call = ~$0.35 per fetch cycle
- 4-часовая cadence × 30 дней = $10-60/month **только на X5**
- Большинство статей не имеет отношения к X5 — мусор в БД
- `output/X5/news/` забивается несвязанными статьями (SpaceX, золото)
- Recommendations папка тоже забьётся — analyst recs **про другие компании**

**Требуется пересмотр P2** до старта реализации.

---

## P1 — Must Fix перед T8.2

### 1. Listing не X5-specific — pre-filter обязателен

См. выше. Варианты:
- **Fetch-stage strong/sector filter** с явным allowlist: `X5, ИКС 5, Пятёрочка, Перекрёсток, Чижик, Магнит, Лента, О'Кей, Fix Price, retail, groceries, ритейл`
- **Analyzer возвращает `is_relevant: true|false`** — irrelevant rows не вставляются в БД (или маркируются skipped)

**Решение:** требуется ack пользователя на пересмотр P2. Мой пred-recommendation: **fetch-stage strong filter** (как у RBC, но с расширенным sector списком) — режет 80% noise до того как доходит до LLM.

### 2. `item_type` критерий sloppy

GPT-5 mini, вероятно, справится с binary classification, но **текущий критерий слаб**:
- «недооценён» появляется в макро-комментариях, competitor analysis, цитатах рыночного chatter без actionable рекомендации
- «держать» вне stock context: hold cash, hold rates, hold position
- Mixing hard signals (target price) и vague valuation language

**Fix:** определить `recommendation` как «explicit investment recommendation about a security or issuer, including action/stance AND rationale/target/upside/downside». Добавить negative examples в SYSTEM_PROMPT: macro note, earnings preview, article про другой issuer, generic «акции выглядят дорого» без actionable stance.

### 3. Bundling `item_type` в mood call — OK только с fixture-based eval

Separate call cleaner, но overkill для MVP. Bundled acceptable **если** T8.4 добавляет реальные примеры (golden set):
- X5 earnings preview → `news`
- AFK Sistema article с рекомендацией про X5 → `recommendation`
- quantum/SpaceX/gold/ETH → not relevant (или news но skipped — см. P1.1)
- broker target-price article → `recommendation`
- статья с «держать» НЕ про stock stance → `news`

Без этого golden set план **рукомашет** вокруг центральной новой архитектуры.

### 4. DB миграция design **сломана** vs current `init_db`

Текущий `src/db.py` всегда выполняет:
```python
conn.executescript(SCHEMA_SQL)
conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
```

Если план просто добавит `item_type` в `SCHEMA_SQL` **И** также сделает `ALTER TABLE ... ADD COLUMN` при `user_version < 2` — на fresh v0 БД колонка создастся через `executescript`, потом будет попытка добавить её снова через ALTER → **error**.

**Fix migration design:**
- Читать `PRAGMA user_version` **до** schema mutation
- Fresh DB → создать v2 schema **directly**, set `user_version=2`
- Existing v0/v1 DB без колонки → `ALTER TABLE`
- Сделать migration column-presence-aware (`PRAGMA table_info(news)` check) или раздельные create/migrate пути

### 5. T8.2 должен **verify URL date vs article meta**, не только селекторы

План фильтрует по URL date **до открытия статьи**. Это **data-loss risk** если finam URL time = publication update time / Moscow-local mismatch / malformed для подмножества.

**Add T8.2 acceptance:**
- Sample at least **5 URLs** across listing, включая oldest/newest и AFK Sistema
- Compare URL `YYYYMMDD-HHMM` to article visible/meta date
- Document timezone
- Decide policy if mismatch > small tolerance

### 6. Backfill coverage acceptance incomplete

«70 items likely cover since 2026-05-01» — недостаточно. T8.2/T8.7 должны parse all listing dates и **доказать**:
- Newest-first ordering (sort confirmation)
- Oldest parsed URL date ≤ 2026-05-01 ИЛИ explicitly report что нужна pagination
- Count items per date/week — busy week silently truncate невозможно

### 7. Переименовать ветку **сейчас**, не перед PR

Текущая `e_disclosure_news` — название из abandoned task. Эта задача — finam. **До любых code commits** переименовать:
```bash
git branch -m e_disclosure_news finam_news
```

Иначе git log путанный.

---

## P2 — Should Address

1. **Missing item_type → 'news' fallback OK; invalid → 'error' OK** — это contract failure, не «safe fallback». Документировать как design choice.

2. **Reporter tests** — explicit header-order test для Excel: `["date", "headline", "persons", "mood", "item_type"]`. Старые тесты не падают (assert только `date` at column 1), но точный список header'ов нужен.

3. **Не создавать empty `recommendations/`** — план говорит так в risks, но snippet wipes both. Implementation: don't `mkdir recommendations` если 0 recommendation rows.

4. **Reclassification orphan cleanup** — wipe **обоих** folders перед regen уже в плане. Хорошо: re-analysis может move item между folders.

5. **`finam_ticker` validation scoped to X5** — `[a-z0-9]+` works для `x5`, рejects MOEX preferred shares (`RTKM-P`). Spec says MVP=X5; **документировать** «X5-only proven; general MOEX ticker support deferred», не претендовать на universal.

6. **PlaywrightSource — rough edges:**
   - `FinamSource.fetch` catches `PlaywrightTimeoutError` но **не импортирует** его — нужен `from playwright.sync_api import TimeoutError as PlaywrightTimeoutError`
   - `_ChallengeFailure` из warmup **не счётчик** в Finam stats; просто bubble. Противоречит «per-class error classification» (P1.6 из codex est 03). Добавить `challenge_failures` counter в stats.
   - `WARMUP_SELECTOR = "h1"` слабый — challenge page тоже может иметь h1. T8.2 выбрать селектор tied to publications listing (например, специфичный для X5 page элемент).

7. **Cross-source coupling — explicit policy:**
   - No re-analysis of old rows в MVP
   - New rows from all sources get LLM `item_type`
   - Если user хочет historical recommendation split — one-off reset/reanalyze migration позже
   Зафиксировать в плане.

8. **Short-body analyzer path** — текущий analyzer skip'ает LLM для bodies < `MIN_BODY_CHARS` (50), маркирует `mood='neutral'`. Этот путь должен либо оставить DB default `item_type='news'` либо explicit set. **Добавить тест.**

9. **Performance estimate too optimistic** — warmup 8s + listing 8s + 70 × article (sleep+goto+pause) = **3-5 минут**, не 30-60s. Dedup/date filter помогают normal cycles, но **первый backfill будет медленный**. Сказать honestly.

10. **Offline fixtures acceptable для parser regression**, но **не доказывают что stealth ещё работает**. Keep opt-in smoke/live probe для servicepipe + document fixture capture date.

---

## P3 — Nice To Have

1. **Excel column** после `mood` — fine, just assert it in test.

2. **Filename для recommendations** — same `yyyy_mm_dd_slug_NN.md`, different root folder. Document explicitly.

3. **`persons.csv` для cross-company recommendations** может быть **пустым** для item'а. OK. Recommendation про X5 в AFK Sistema article может не иметь mentions X5 founders. Не treat «no persons» как bad data.

4. **Install sanity earlier** — `pip install -r requirements.txt` + `python -m playwright install chromium` + smoke **before deep implementation**, не only T8.8. Помещай в T8.3 acceptance.

5. **Honest estimate:**
   - **Optimistic 6-8 days** (vs plan's 4.5-6.5)
   - **P90 11-14 days** (vs plan's 9-11)
   - Reason: Playwright infra never shipped (no real friction data); finam selectors/article parsing unknown; `item_type` introduces prompt/eval/reporter/schema coupling.

---

## Reaction (заполняется пользователем перед v2 плана)

Каждый пункт — accept / reject / defer. Под маркером **Решение:** — твой ответ.

### Главный вопрос — пересмотр P2

**Pre-filter listing? (codex P1.1)** Recommendation от меня: **YES, fetch-stage strong filter** (X5 aliases + retail competitors + retail/groceries keywords). Резко режет шум.
**Решение пользователя: variant A — fetch-stage strong filter.** Это пересматривает spec P2 (там было «без фильтра») в свете того, что HTML фикстура показала broad-market content (SpaceX, Ethereum, золото).

**Реализация фильтра (мой подход для plan v2):**
- Slugs на finam — Latin-transliterated (АФК Система → `afk-sistema`, Пятёрочка → `pyaterochka` и т.д.). Поэтому работаем со списком Latin-substring'ов.
- `_FINAM_RELEVANT_SLUG_PARTS` — module-level список substring'ов с word-boundary через дефис:
  ```python
  _FINAM_RELEVANT_SLUG_PARTS = [
      # X5 direct
      "x5", "iks-5", "iks5", "korporativnyy-centr-iks",
      # X5 brands (Latin transliteration)
      "pyatero", "pyaterochk", "perekr", "perekrestok", "perekrjostok", "chizhik",
      # Retail competitors (вмешательство для cross-company analytics)
      "magnit", "lenta", "okey", "o-key", "fix-price", "fixprice", "vkusvill",
      # X5 ownership / holdings related (codex отметил AFK Sistema → X5 recommendation case)
      "sistema", "afk-sistema",
      # Retail-sector keywords
      "ritejl", "ritail", "prodovolstvenn", "fmcg", "magazin-prodkutov",
  ]
  ```
- Match logic: case-insensitive substring + dash-boundary. «lenta» не должна матчить случайные «kalenta»; используем regex `(?:^|-)lenta(?:-|$)`.
- Filter применяется в `FinamSource.fetch()` после `_parse_listing`, до per-article `_goto`.
- Logged stats: `relevance_filtered` counter.

### Остальные P1

**P1.2** (item_type criterion crisp) — Решение: **accept**. SYSTEM_PROMPT добавит crisp definition + 5 negative examples (macro note, earnings preview, article про другой issuer, generic «дорого» без stance, «держать» не про stock).

**P1.3** (golden set для bundled classification) — Решение: **accept**. В T8.4 acceptance — golden set из 5 примеров (см. codex pункт). Сравниваем LLM output с expected → если ≥4/5 правильно → accept.

**P1.4** (DB migration design fix) — Решение: **accept**. Архитектура миграции:
1. Read `PRAGMA user_version` ДО schema mutation
2. If 0 (fresh DB): create v2 schema directly (SCHEMA_SQL уже содержит item_type), set user_version=2
3. If 1 (v1 existing): `PRAGMA table_info(news)` check column presence; если нет — `ALTER TABLE`; set user_version=2
4. If 2 (already migrated): no-op
5. Idempotent повторный init-db

**P1.5** (T8.2 URL date vs meta verification) — Решение: **accept**. T8.2 acceptance расширен: sample 5 URLs (oldest, newest, AFK Sistema, X5 direct, средний по позиции), compare URL `YYYYMMDD-HHMM` vs visible date в article body / meta. Document mismatch tolerance (например, ≤1 час = OK).

**P1.6** (backfill coverage proof) — Решение: **accept**. T8.2 acceptance: parse все 70 listing dates, sort newest-first, **доказать**: oldest ≤ 2026-05-01 ИЛИ требуется pagination (отдельная мини-спека). Items-per-week histogram — для проверки нет ли скрытого truncation.

**P1.7** (rename branch now) — Решение: **DONE** (выполнено перед записью этого estimate'а — `git branch -m e_disclosure_news finam_news`).

### P2 (мои предложения — взять на себя если accept):

**P2.1** (item_type missing → news, invalid → error; document as contract) — accept
**P2.2** (explicit Excel header-order test) — accept
**P2.3** (не создавать empty recommendations/) — accept
**P2.4** (wipe both folders) — already in plan
**P2.5** (finam_ticker X5-only documentation) — accept
**P2.6** (import PlaywrightTimeoutError, add challenge_failures counter, T8.2 selects WARMUP_SELECTOR) — accept
**P2.7** (no re-analysis policy explicit в плане) — accept
**P2.8** (short-body analyzer keeps default item_type='news', add test) — accept
**P2.9** (honest perf estimate: 3-5 min first backfill) — accept
**P2.10** (offline fixtures + opt-in live smoke с capture date) — accept

### P3 (nice-to-have)
Все accept или defer на твоё усмотрение.

---

После заполнения главного P2-вопроса (фильтровать или нет) + ack P1.2-P1.7 → `plans/04_claude_finam_plan.md` v2 пишется.

---

## Статус

**Все P1 закрыты решениями (variant A для filter + accept остальные).** Все P2/P3 — accept как baseline. План v2 пишется.
