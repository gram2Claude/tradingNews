# Estimate 03 (codex critique) — атака на `plans/03_claude_e_disclosure_plan.md`

Источник: `/codex consult` (gpt-5.5, model_reasoning_effort=medium, ~205KB stderr)
Дата: 2026-05-21
Цель: вскрыть слабые места в плане 03 (Playwright + e-disclosure) **до старта T7.2**.
Связанный план: `plans/03_claude_e_disclosure_plan.md`
Связанная спека: `specs/03_e_disclosure_spec.md`
Связанный recon: `tests/fixtures/EDISCLOSURE_RECON.md`

GATE: **FAIL** (6 P1 — must-fix перед T7.2)

---

## Bottom line от Codex

> Architecture direction is fine. The weak part is not the `Source` inheritance;
> it is the **assumption that browser navigation equals WAF success**. Make T7.2
> a real browser/WAF acceptance gate with cookie/content/selector assertions
> and pagination proof before writing reusable infrastructure around it.

---

## P1 — Must Fix перед T7.2

### 1. `_goto(..., wait_until="networkidle")` ≠ challenge solved

План трактует `networkidle` как доказательство, что servicepipe-challenge
прошёл и cookies установлены. **Это слабое допущение.** WAF challenge может:
- успокоить сеть, всё ещё показывая challenge-страницу
- молча fail
- отложить redirect / cookie work

**T7.2 acceptance должен явно проверять после warmup:**
- `context.cookies()` содержит `spsn` и `spid`
- Текущая страница НЕ 1703-байтная servicepipe challenge-страница
- В HTML нет markers `servicepipe.ru` или spinner-классов
- Видим селектор target listing'а

Без этого «warmup прошёл» может на самом деле быть «парсим challenge-страницу
как пустой listing».

### 2. Headless Playwright success — центральное недоказанное допущение

Recon доказал только то, что httpx **fails**. Не доказал что **default
Playwright headless passes** servicepipe.

Default headless Chromium fingerprintable достаточно, чтобы fail был
правдоподобен. T7.2 должен иметь конкретную **contingency**, не просто «STOP
and escalate»:

- Прогнать матрицу: `chromium headless`, `chromium headed`, опц. `firefox
  headed/headless`
- Фиксировать real Chrome UA, locale=ru-RU, timezone
- Записать **точный** failure mode для каждой комбинации
- **НЕ начинать T7.3/T7.4** пока один режим reliably открывает X5 listing

### 3. Lifecycle leak если `__enter__` fails halfway

```python
def __enter__(self):
    self._pw = sync_playwright().start()       # ← ОК
    self._browser = self._pw.chromium.launch()  # ← если raise здесь
    self._bcontext = self._browser.new_context()
    ...
```

Если Chromium launch или `new_context()` raises, `with cls(...)` никогда не
доходит до `_fetch_one`, `__exit__` не вызывается → Playwright server process
**остаётся жить**.

**Fix:** обернуть `__enter__` в `try/except: self.close(); raise`. Это
**обязательная hygiene** для reusable Playwright sources.

### 4. `_url_in_db()` pre-check inefficient и underspecified

```python
def _url_in_db(db_path: Path, source_id: int, url: str) -> bool:
    # per-URL открытие SQLite ← ПЛОХО
```

Существующая архитектура уже делает дедуп через `UNIQUE(source_id, url)` в
`db.py:30` + `INSERT OR IGNORE` в `fetcher.py:144`.

Если хочешь pre-check чтобы избежать дорогих page.goto:
- **Bulk-load** все known URLs для `(source_id)` в `set` **один раз** перед
  event loop
- Или используй существующий fetcher connection (передать в context)
- И явно прими: **changed disclosure content игнорируется** (раскрытия обычно
  не меняются, но проговори это)

### 5. Pagination нельзя оставлять как «probably one page»

Источник — **регуляторные раскрытия**. **Silent truncation хуже чем no source.**

T7.2 acceptance должен **явно доказать**:
- Page count для X5 за период с 2026-05-01
- Page-size (events per page)
- Ordering (newest first?)
- Date-filter behavior (можно ли фильтровать на стороне сервера?)
- Next-page URL параметры

T7.4 должен **реализовать pagination до `since` или hard max**, как `x5_ir`
делает с `MAX_PAGES`. Не полагаться на оценку «~30 events за май».

### 6. Failure semantics нужна source-level классификация

`fetcher.py:126` ловит все `Exception` → один source error. Это OK, но
**недостаточно**.

EDisclosureSource должен в логах **различать**:
- `PlaywrightTimeoutError` на warmup (servicepipe медленный / IP throttle)
- Challenge failure (cookies не получены)
- Browser crash (process died mid-fetch)
- Parse failure (HTML структура изменилась)

Это разные операционные фиксы. 30s timeout на warmup — не то же самое, что
одна битая event-страница.

---

## P2 — Should Address

### 1. `Source.__init__(base_url, user_agent, context)` — план путано описывает

`Source` хранит `user_agent` **generically**, не специфично для httpx
(`base.py:113`). Использовать его для `browser.new_context(user_agent=...)` —
ОК. План **не игнорирует** user_agent — пример кода его использует. **ABC
refactor не нужен.**

Но: `PlaywrightSource.__init__(..., user_agent=None)` меняет default с
non-None `Source` на None + substitute `_DEFAULT_UA`. **Сделать явным**:
определить `_DEFAULT_UA` как module constant.

### 2. `super().__init__` — keyword args для читаемости

Mechanically OK. Использовать keyword args как в `RBCSource`:
```python
super().__init__(base_url=base_url, user_agent=user_agent, context=context)
```

### 3. No keyword filter — intentional, но нужно проговорить

`FetchContext.load_keywords()` есть для широких feeds (RBC, where company
relevance is unknown). E-disclosure listing **уже выбран по issuer_id** —
keyword filter был бы избыточен или неверен.

Добавить одно предложение в план: «No keyword filter; issuer ID is the
relevance filter».

### 4. Cookie persistence — under-modeled

In-memory `BrowserContext` → каждая fetch session **снова решает challenge**.
4-часовая частота → **~6 challenges/day**. Может быть OK, но **план должен
это явно сказать** + логировать challenge solve time.

Если challenge failures начнут расти — persistent context это **первый
fallback**, не vague «later mini-spec».

### 5. `CompanyCfg.e_disclosure_id` нужна валидация

`str | None` без валидации → `"TODO"`, `"1380 "`, full URL могут fail позже
внутри Playwright.

**Валидация на config load или EDisclosureSource.__init__:**
- Non-empty
- Digit-only string
- Normalized `.strip()`
- Понятная error message

### 6. Mock-only Playwright tests недостаточно для инфраструктуры

Mocking `sync_playwright().start().chromium.launch().new_context().new_page()`
только proves что моки match код.

**Добавить:** один **опциональный/manual smoke test** или probe acceptance,
который открывает `https://example.com` локально после install. Parsing tests
на сохранённом HTML остаются browser-free — это OK.

### 7. Close order — обнулять fields + быть идемпотентным

Closing page/context/browser/pw with swallow errors — OK, но **set
`_page = _bcontext = _browser = _pw = None` после попыток**. Иначе повторный
`close()` может вызвать stale objects и скрыть real lifecycle bugs.

### 8. Sync Playwright on Windows — pinning lazy

`playwright>=1.40` слишком loose для Windows-only personal tool. **Pin
known-tested minor после T7.2**, например `playwright==<tested version>`.

Документировать `python -m playwright install chromium` в README.

---

## P3 — Nice To Have

### 1. Browser-crash recovery policy

Если один event `goto` убьёт page/browser, current logic вероятно сделает все
последующие `_goto` fail. Detect browser/page closed + либо recreate once,
либо abort source с clear «browser dead» error.

### 2. Challenge telemetry

Логировать: warmup duration, cookies present/missing, headed/headless mode,
Playwright browser name, event count, page count, skipped duplicates. Это
**окупится** при первом изменении servicepipe behavior.

### 3. PDFs — явно acknowledge

План говорит «PDF download out of scope». OK. Но для регуляторных раскрытий
**attachments may contain the real payload**.

Помечать получаемые items как «event page text only; attachments ignored»,
чтобы downstream analysis не over-trusted.

### 4. Estimate optimistic

3.5 / 6 дней верибельно **только если** T7.2 пройдёт default Playwright
быстро. С servicepipe, Windows install friction, selector iteration, process
cleanup — **honest range 4-5 дней optimistic, 8-10 дней p90**.

Для personal tool это **всё ещё acceptable**, но не претендуй что это
сравнимо с RBC RSS shortcut.

### 5. DB schema — без изменений (confirmed)

Current `news` table уже имеет URL, headline, body, published_at, source_id,
company_id, uniqueness (`db.py:30`). **Migration не нужна.** Если решишь
хранить attachment metadata или disclosure type отдельно — это уже отдельная
правка.

---

## Reaction (заполняется пользователем перед v2 плана)

Каждый пункт — accept / reject / defer. Под маркером **Решение:** — твой ответ.

**P1.1** (networkidle ≠ challenge solved) — Решение: **accept**. T7.2 acceptance: после warmup проверяем `spsn`+`spid` в cookies, HTML не 1703B challenge, нет `servicepipe.ru` маркеров, виден listing-селектор. В PlaywrightSource добавим helper `_verify_warmup_success(expected_selector)`.

**P1.2** (headless plays unproven — matrix) — Решение: **accept**. T7.2 — матрица: `chromium headless`, `chromium headed`, `firefox headed`. Фиксируем точный failure mode для каждой комбинации. T7.3+ не стартует пока **один режим reliably открывает X5 listing**.

**P1.3** (lifecycle leak в __enter__) — Решение: **accept**. `__enter__` оборачивается в `try/except: self.close(); raise`. Это infrastructure hygiene.

**P1.4** (`_url_in_db` bulk-load) — Решение: **accept**. Один SQL запрос `SELECT url FROM news WHERE source_id=?` в `set` на старте fetch, потом O(1) lookup'ы. Фиксируем: «changed disclosure content игнорируется» — для регуляторных раскрытий это OK, они не редактируются.

**P1.5** (pagination explicit proof) — Решение: **accept**. T7.2 acceptance расширяется: page count за период, page-size, ordering, next-page URL pattern. T7.4 реализует `MAX_PAGES = 20` (~200-400 events) с break при достижении `since` или hard limit.

**P1.6** (failure classification) — Решение: **accept**. В fetch loop ловим отдельно: `PlaywrightTimeoutError`, `_ChallengeFailure` (custom — cookies не получены), `_BrowserDead` (custom — page/browser closed), `_ParseError`. Каждый — свой counter в stats + log message. Single source error (как у fetcher.py) не меняем.

---

P2 — мои решения (взято на себя):

**P2.1** (`_DEFAULT_UA` constant) — Решение: **accept**. Module-level `_DEFAULT_UA` в `playwright_base.py`. Актуальный Chrome 131 desktop, как у RBC.

**P2.2** (super().__init__ keyword args) — Решение: **accept**. Косметика для консистентности с `RBCSource`.

**P2.3** (фраза «no keyword filter, issuer ID is filter») — Решение: **accept**. Добавляю явный комментарий в `EDisclosureSource.fetch` + один раздел в plan'е.

**P2.4** (cookie persistence proговорить + telemetry) — Решение: **accept**. В план: «in-memory context → 6 challenges/day. Если challenge solve time > 5s или failures растут — persistent context это first fallback». Telemetry — см. P3.2.

**P2.5** (e_disclosure_id validation) — Решение: **accept**. В `EDisclosureSource.__init__` после context check: `if not (s := str(id).strip()).isdigit(): raise ValueError(...)`. Понятная error message.

**P2.6** (smoke test на example.com) — Решение: **accept**. Опциональный test `test_playwright_smoke_example_com` помечен `@pytest.mark.smoke` — не бежит в дефолтном `pytest tests/`, бежит `pytest tests/ -m smoke`. Это manual verification после install.

**P2.7** (close обнуляет fields) — Решение: **accept**. После каждого закрытия `self._<x> = None`.

**P2.8** (pin playwright==) — Решение: **accept conditionally**. Сейчас `playwright>=1.40`, после T7.2 на реальной машине пользователя — pin'нем `playwright==<tested>` в отдельном мини-коммите.

---

P3 — мои решения:

**P3.1** (browser-crash recovery) — Решение: **accept (lightweight)**. Detect через `try: self._page.url; except`: → log + raise `_BrowserDead` → fetcher catches как source error. **НЕ** recreate browser (сложно, неоднозначно). Простая abort-стратегия.

**P3.2** (challenge telemetry) — Решение: **accept**. Логируем: `warmup_ms`, `cookies_set: [spsn, spid]`, `mode: headless`, `browser: chromium`, `pages_seen`, `events_per_page`. Структурный log.info.

**P3.3** (явная пометка «event text only») — Решение: **defer**. В `EDisclosureSource` docstring + сводный log: «N events fetched, attachments not extracted». Не помечаем каждый item — это избыточно.

**P3.4** (estimate 4-5 / 8-10 дней) — Решение: **accept**. Обновляю табличку оценок в плане.

**P3.5** (DB без изменений) — Решение: **accept (confirmed)**. Подтверждено codex'ом и моей проверкой db.py. Никаких миграций.

---

Статус: **READY** — план v2 пишется с этими решениями.
