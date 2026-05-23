# Plan 03 — e-disclosure.ru source + Playwright инфраструктура (v2)

Статус: READY (v3 — после T7.2)
Дата: 2026-05-21
Версия: **v3** (поправки из T7.2 live recon — см. EDISCLOSURE_RECON.md §6)

## Главные изменения v2 → v3 (из T7.2 живого recon)

1. **playwright-stealth обязателен.** Default Playwright (4 режима) не пробивает servicepipe. С `Stealth().use_sync(sync_playwright())` chromium headless проходит.
2. **`wait_until="networkidle"` не работает** — XHR'ы (Яндекс.Метрика, реклама) никогда не fire. Используем `wait_until="domcontentloaded" + sleep(5-8s)`.
3. **Listing = company page**, не отдельный `files.aspx?page=N`. URL: `/portal/company.aspx?id={id}`, на главной 19 events.
4. **Pagination не нужна** для MVP — 19 events покрывают backfill с 2026-05-01.
5. **EventId opaque string** (`YDJnAYBvL0udnyKj1zuduA-B-B`), не digit. Хранится в `news.url`, отдельной валидации не требует.
6. **`e_disclosure_id` всё ещё digit** (X5 id=`39008`), валидация в EDisclosureSource.__init__ остаётся.
7. **Servicepipe selectively** включается на `/Company/Search?query=*` GET URLs — обходим через form submit (`input[name="query"]` + Enter). Но для production не нужно — мы идём сразу на `/portal/company.aspx?id=X5_ID`, минуя search.
8. **Cookies servicepipe (spsn/spid)** под stealth могут НЕ устанавливаться — `_verify_warmup_success()` проверяет только `is_challenge` markers (html size > 2500, no `servicepipe.ru`/`id_spinner`) + expected selector.

---

Статус: READY
Дата: 2026-05-21
Версия: **v2** (учтены 6 P1 + 8 P2 + 5 P3 из `estimates/03_codex_e_disclosure_est.md`)
Связанная спека: `specs/03_e_disclosure_spec.md` (APPROVED)
Связанная оценка: `estimates/03_codex_e_disclosure_est.md`
Связанный recon: `tests/fixtures/EDISCLOSURE_RECON.md` (T7.1, ПРОЙДЕН)
Ветка: `e_disclosure_news`

---

## Что изменилось v1 → v2

| # | Тема | Изменение |
|---|---|---|
| P1.1 | networkidle ≠ challenge solved | T7.2 acceptance: explicit cookie/content/selector checks. Helper `_verify_warmup_success()` в `PlaywrightSource`. |
| P1.2 | Headless plays unproven | T7.2 — матрица режимов; T7.3+ не стартует пока один режим reliably работает. |
| P1.3 | Lifecycle leak | `__enter__` обёрнут в `try/except: self.close(); raise`. |
| P1.4 | URL dedup inefficient | Bulk-load known URLs одним SQL в `set` на старте fetch. |
| P1.5 | Pagination silent truncation | T7.2 explicit pagination proof; T7.4 `MAX_PAGES=20` loop до `since`. |
| P1.6 | Failure classification | Custom exceptions: `_ChallengeFailure`, `_BrowserDead`, `_ParseError`. Per-class counters в stats. |
| P2.1-2.8 | UA constant, super() kw, no-keyword note, cookie persistence telemetry, e_disclosure_id validation, smoke test, close idempotency, playwright pin | См. ниже по фазам |
| P3.1-3.4 | Browser-crash abort, telemetry, estimate update | См. ниже |

---

## Что v2 НЕ меняет vs v1

- Архитектурное место (PlaywrightSource как отдельный модуль)
- Bundle approach (T7.3+T7.4 в одном PR)
- All disclosure types (P2 из spec)
- Backfill с 2026-05-01 (P3 из spec)
- 4-часовая cadence + `auto_run: false` (P4 из spec)
- Headless=True default + `PLAYWRIGHT_HEADLESS=false` env override (P5 из spec)
- In-memory BrowserContext (P6 из spec)

---

## Архитектурное место (без изменений vs v1)

```
src/sources/base.py                      ← без изменений
src/sources/playwright_base.py           ← НОВЫЙ — PlaywrightSource(Source) ABC
src/sources/e_disclosure.py              ← НОВЫЙ — EDisclosureSource(PlaywrightSource)
src/fetcher.py                           ← +SOURCE_REGISTRY entry
src/config.py                            ← +CompanyCfg.e_disclosure_id с валидацией
config.yaml + .example                   ← +sources.e_disclosure, +companies[X5].e_disclosure_id
requirements.txt                         ← +playwright>=1.40 (pin'нем после T7.2)
tools/probe_edisclosure.py               ← НОВЫЙ — T7.2 manual probe script
tests/fixtures/EDISCLOSURE_RECON.md      ← дополняется в T7.2
tests/fixtures/edisclosure_listing.html  ← НОВЫЙ — из T7.2
tests/fixtures/edisclosure_event.html    ← НОВЫЙ — из T7.2
tests/test_playwright_base.py            ← НОВЫЙ — unit + smoke (на example.com, opt-in)
tests/test_e_disclosure.py               ← НОВЫЙ — parsing + fetch с моком
```

БД без изменений (подтверждено P3.5).

---

## Контракт `PlaywrightSource` (v2)

```python
# src/sources/playwright_base.py

import os, random, time
from abc import ABC
from typing import Iterable

from playwright.sync_api import sync_playwright

from src.sources.base import RawItem, Source, FetchContext

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class _ChallengeFailure(Exception):
    """Warmup завершился без признаков прохождения WAF challenge."""


class _BrowserDead(Exception):
    """Browser/page закрылся вне нашего control (crash, остановка пользователем)."""


class PlaywrightSource(Source, ABC):
    """Base для источников за JS-challenge anti-bot WAF.

    Контракт subclasses:
    - Реализовать ``fetch(since)`` (из Source)
    - Использовать ``self._goto(url, expected_selector=...)`` вместо raw httpx
    - Вызывать ``self._verify_warmup_success(...)`` после первого goto
    - Корректно ловить и классифицировать `_ChallengeFailure` / `_BrowserDead`
    """

    DEFAULT_TIMEOUT_MS = 30_000
    MIN_PAUSE_S, MAX_PAUSE_S = 1.0, 3.0

    def __init__(self, base_url, user_agent=None, context=None):
        super().__init__(base_url=base_url, user_agent=user_agent, context=context)
        self._pw = None
        self._browser = None
        self._bcontext = None
        self._page = None
        self._requests_made = 0

    def __enter__(self) -> "PlaywrightSource":
        try:
            headless = os.environ.get("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=headless)
            self._bcontext = self._browser.new_context(
                user_agent=self.user_agent or _DEFAULT_UA,
                viewport={"width": 1280, "height": 800},
                locale="ru-RU",
                timezone_id="Europe/Moscow",
            )
            self._page = self._bcontext.new_page()
            log.info("playwright: launched %s headless=%s",
                     "chromium", headless)
            return self
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        for attr in ("_page", "_bcontext", "_browser", "_pw"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    if attr == "_pw":
                        obj.stop()
                    else:
                        obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)

    def _polite_pause(self) -> None:
        time.sleep(random.uniform(self.MIN_PAUSE_S, self.MAX_PAUSE_S))

    def _goto(self, url: str, wait: str = "networkidle") -> str:
        """Navigate to url, wait for full load. Returns HTML.

        Может raise _BrowserDead если страница/браузер закрыты.
        """
        if self._page is None or self._browser is None:
            raise _BrowserDead("browser/page is None — likely closed externally")
        try:
            if self._requests_made > 0:
                self._polite_pause()
            self._requests_made += 1
            self._page.goto(url, timeout=self.DEFAULT_TIMEOUT_MS, wait_until=wait)
            return self._page.content()
        except PlaywrightError as exc:
            # Различаем "browser closed" от других ошибок
            if "closed" in str(exc).lower() or "Target page" in str(exc):
                raise _BrowserDead(str(exc)) from exc
            raise

    def _verify_warmup_success(self, expected_selector: str) -> dict:
        """После первого goto на главную — убедиться что challenge прошёл.

        Проверяет:
        - cookies содержат spsn + spid (servicepipe markers)
        - HTML не challenge-страница (не 1703B, нет 'servicepipe.ru' маркера)
        - expected_selector виден на странице

        Возвращает telemetry dict. Raises _ChallengeFailure если что-то не так.
        """
        t_start = time.monotonic()
        cookies = {c["name"] for c in self._bcontext.cookies()}
        html = self._page.content()
        has_spsn = "spsn" in cookies
        has_spid = "spid" in cookies
        is_challenge = ("servicepipe.ru" in html
                        or "id_spinner" in html
                        or len(html) < 2000)
        try:
            selector_visible = self._page.is_visible(expected_selector, timeout=5000)
        except PlaywrightError:
            selector_visible = False
        warmup_ms = int((time.monotonic() - t_start) * 1000)
        telemetry = {
            "warmup_ms": warmup_ms,
            "cookies_set": sorted(cookies & {"spsn", "spid"}),
            "is_challenge_page": is_challenge,
            "selector_visible": selector_visible,
            "html_size": len(html),
        }
        log.info("playwright warmup: %s", telemetry)
        if not (has_spsn and has_spid and not is_challenge and selector_visible):
            raise _ChallengeFailure(
                f"warmup failed: spsn={has_spsn} spid={has_spid} "
                f"challenge={is_challenge} selector={selector_visible}"
            )
        return telemetry
```

---

## Контракт `EDisclosureSource` (v2)

```python
# src/sources/e_disclosure.py
"""e-disclosure.ru — регуляторные раскрытия эмитентов через Playwright.

No keyword filter: issuer ID (`e_disclosure_id` в config.yaml) уже выбирает
конкретного эмитента; weak surname / brand match здесь избыточны.

Out of scope: PDF/document attachments; берётся только text из event-page.
"""

import sqlite3
from src.sources.base import FetchContext, RawItem
from src.sources.playwright_base import (
    PlaywrightSource, _ChallengeFailure, _BrowserDead,
)

MAX_PAGES = 20  # ~200-400 events ceiling per fetch cycle
LISTING_PATH = "/portal/files.aspx?id={id}&page={page}"  # уточняется в T7.2
WARMUP_SELECTOR = ".company-list, table.events"          # уточняется в T7.2


class _ParseError(Exception):
    """HTML структура изменилась — селекторы не нашли expected данные."""


class EDisclosureSource(PlaywrightSource):
    code = "e_disclosure"

    def __init__(self, base_url, user_agent=None, context=None):
        if context is None:
            raise ValueError("EDisclosureSource requires FetchContext")
        raw_id = context.company_cfg.e_disclosure_id
        if not raw_id:
            raise ValueError(
                f"company {context.company_cfg.name!r} has no e_disclosure_id "
                "configured (set companies[X].e_disclosure_id in config.yaml; "
                "use T7.2 recon procedure to find it)"
            )
        clean_id = str(raw_id).strip()
        if not clean_id.isdigit():
            raise ValueError(
                f"e_disclosure_id for {context.company_cfg.name!r} must be a "
                f"digit-only string, got {raw_id!r}"
            )
        super().__init__(base_url=base_url, user_agent=user_agent, context=context)
        self._company_eid = clean_id

    def fetch(self, since):
        # 1. Warmup на главной — solve servicepipe challenge.
        self._goto(self.base_url)
        warmup_telemetry = self._verify_warmup_success(WARMUP_SELECTOR)

        # 2. Bulk-load known URLs из БД (P1.4) — один SQL, не per-URL.
        with sqlite3.connect(self.context.db_path) as conn:
            known_urls = {
                row[0] for row in conn.execute(
                    "SELECT url FROM news WHERE source_id=?",
                    (self.context.source_id,),
                )
            }
        log.info("e_disclosure: %d known URLs pre-loaded for dedup", len(known_urls))

        # 3. Пагинация (P1.5) — гулять до `since` или MAX_PAGES.
        stats = {"pages_seen": 0, "events_listed": 0, "older_than_since": 0,
                 "already_in_db": 0, "kept": 0,
                 "timeout_errors": 0, "challenge_errors": 0,
                 "browser_dead": 0, "parse_errors": 0}
        results = []
        for page in range(1, MAX_PAGES + 1):
            listing_url = urljoin(
                self.base_url,
                LISTING_PATH.format(id=self._company_eid, page=page),
            )
            try:
                html = self._goto(listing_url)
                events = list(_parse_listing(html))
            except _BrowserDead:
                stats["browser_dead"] += 1
                log.error("e_disclosure: browser dead on page %d, aborting fetch", page)
                raise
            except PlaywrightTimeoutError:
                stats["timeout_errors"] += 1
                log.warning("e_disclosure: page %d timed out", page)
                break  # listing pagination не критична — выходим
            stats["pages_seen"] += 1
            stats["events_listed"] += len(events)
            if not events:
                break

            # Проверяем: вышли ли за `since`. Если самый старый event старше
            # since — следующие страницы будут ещё старше, выходим.
            if events[-1].published_at < since:
                pass  # отфильтруем дальше per-event

            page_kept = 0
            for ev in events:
                if ev.published_at < since:
                    stats["older_than_since"] += 1
                    continue
                if ev.url in known_urls:
                    stats["already_in_db"] += 1
                    continue
                try:
                    event_html = self._goto(ev.url)
                    item = _parse_event(ev.url, event_html, listing_meta=ev)
                    results.append(item)
                    stats["kept"] += 1
                    page_kept += 1
                except _BrowserDead:
                    stats["browser_dead"] += 1
                    raise
                except PlaywrightTimeoutError:
                    stats["timeout_errors"] += 1
                except _ParseError:
                    stats["parse_errors"] += 1

            # Если на этой странице все события старше since — стоп.
            if all(ev.published_at < since for ev in events):
                break

        log.info("e_disclosure fetch summary: %s | warmup=%s",
                 stats, warmup_telemetry)
        return results
```

Заметки:
- **No keyword filter** (P2.3) — issuer ID в URL уже выбирает X5, любые
  раскрытия там по определению про X5.
- **Bulk dedup** (P1.4) — один SQL на старте, потом O(1) set lookup. Игнорирует
  changed content (для регуляторных раскрытий это OK — они не редактируются).
- **MAX_PAGES** (P1.5) — hard cap чтобы избежать бесконечной пагинации; реальный
  предел определяется условиями `events[-1].published_at < since` или пустой страницей.
- **Per-class error counters** (P1.6) — видно что именно сломалось.
- **`_BrowserDead` raise propagates** — fetcher ловит как source error.

---

## Этапы

### T7.1 — ПРОЙДЕН (recon httpx)
См. `tests/fixtures/EDISCLOSURE_RECON.md`.

### T7.2 — Playwright живой recon + WAF acceptance gate (0.5-1 день)

**Это центральная gate task v2.** До тех пор пока T7.2 не прошёл — никакой
архитектурный код не пишется.

Шаги:
1. `pip install playwright` (без pin'а пока — поставим то что приедет, потом
   зафиксируем по результатам)
2. `python -m playwright install chromium firefox` (P1.2: матрица — нужны оба)
3. Написать `tools/probe_edisclosure.py` — script который **запускает матрицу**:
   ```
   for browser in [chromium, firefox]:
       for mode in [headless, headed]:
           try:
               открыть главную, ждать networkidle
               проверить spsn+spid в cookies
               проверить is_challenge_page (HTML size, маркеры)
               открыть /Company/Search?query=Корпоративный+центр+ИКС+5
               проверить что виден result selector
               открыть listing X5
               проверить пагинацию (есть ли page=2, page=N)
               открыть один event-page
               сохранить cookies + warmup_ms + точный failure mode (если есть)
           except <всё> as exc:
               log "[{browser}/{mode}] FAIL: {type(exc).__name__}: {exc}"
   ```
4. **Выбрать первый working (browser, mode)** и зафиксировать в плане /
   `EDISCLOSURE_RECON.md` как «production mode».
5. Сохранить HTML снимки:
   - `tests/fixtures/edisclosure_listing.html` (один лист X5 раскрытий)
   - `tests/fixtures/edisclosure_event.html` (одна страница события)
6. Зафиксировать в `EDISCLOSURE_RECON.md` (append):
   - **X5 ID** (digit-only): `<число>`
   - **X5 ИНН**: `<число>` (для верификации)
   - **X5 ОГРН**: `<число>`
   - **Production mode**: `chromium headless` (или другое — что прошло)
   - **Failure modes** для других режимов
   - **Listing URL pattern** (подтверждённый): `/portal/files.aspx?id={id}&page={N}`
   - **Pagination behavior**: page count для X5 за 2026-05-01..now, page-size, ordering
   - **Селекторы listing**: rows, links, dates, event types
   - **Селекторы event-page**: title, date, type, body
   - **Warmup latency**: warmup_ms median
   - **Cookies set**: spsn + spid (или другие маркеры)
   - **WARMUP_SELECTOR**: какой селектор использовать для verify_warmup_success

**Acceptance T7.2 (hard gate):**

- [ ] Playwright + Chromium + Firefox установлены на Windows-машине пользователя
- [ ] Один (browser, mode) **reliably** открывает X5 listing — три прогона подряд успешны
- [ ] cookies `spsn` + `spid` устанавливаются после warmup
- [ ] HTML после warmup ≠ 1703B challenge-страница, без `servicepipe.ru` маркеров
- [ ] WARMUP_SELECTOR виден после `wait_until="networkidle"`
- [ ] Pagination behavior **подтверждена** (есть ли page=2, какой format URL)
- [ ] X5 ID найден (digit-only string)
- [ ] HTML фикстуры сохранены
- [ ] `EDISCLOSURE_RECON.md` дополнен **всеми** ответами выше

**Если ни один режим не работает:** STOP, эскалируем варианты —
`playwright-stealth`, real Chrome profile, или **переход на
disclosure.skrin.ru** (альтернативный диссеминатор, отдельный recon).

### T7.3 — `PlaywrightSource` ABC + smoke (1-1.5 дня)

`src/sources/playwright_base.py`:
- Класс с lifecycle, `_goto`, `_polite_pause`, `_verify_warmup_success`
- Try/except в `__enter__` (P1.3)
- close() обнуляет fields (P2.7)
- `_DEFAULT_UA` module constant (P2.1)
- Custom exceptions `_ChallengeFailure`, `_BrowserDead`

`tests/test_playwright_base.py`:
- `test_playwright_base_requires_subclass_to_implement_fetch` — ABC контракт
- `test_close_idempotent_after_partial_init` — `__enter__` падает после
  `pw.start()`, до `chromium.launch()` → `close()` корректно завершается, fields = None
- `test_polite_pause_called_between_gotos` — мок time.sleep
- `test_headless_env_override` — `PLAYWRIGHT_HEADLESS=false` → headed
- `test_close_swallows_exceptions` — page.close() raises → close() не падает
- `test_verify_warmup_success_passes_on_good_state` — моки cookies + content + selector
- `test_verify_warmup_success_raises_on_challenge_page` — HTML маленький, маркер `id_spinner`
- `test_verify_warmup_success_raises_on_missing_cookies` — нет spsn
- `test_goto_raises_browser_dead_on_closed` — `_page = None` → `_BrowserDead`

**Smoke test (opt-in)** — `pytest tests/ -m smoke`:
- `@pytest.mark.smoke`
- `test_playwright_smoke_example_com` — реальный page.goto на https://example.com,
  asserts `"Example Domain"` в HTML. Manual verification что Playwright живёт.

**Acceptance T7.3:**
- ~10 unit-тестов + 1 smoke (opt-in) зелёные
- `ruff` + `mypy` чисто на новом файле
- Старые 59 тестов проходят
- `pytest tests/ -m smoke -q` запускается вручную и проходит

### T7.4 — `EDisclosureSource` impl (1-1.5 дня)

`src/sources/e_disclosure.py`:
- Класс с контрактом выше (см. секция «Контракт `EDisclosureSource`»)
- e_disclosure_id валидация в `__init__` (P2.5)
- Bulk URL dedup в fetch (P1.4)
- Pagination loop с MAX_PAGES (P1.5)
- Per-class error counters (P1.6)
- Module-level helpers (тестируемые без браузера):
  - `_parse_listing(html) -> Iterator[ListingHit]`
  - `_parse_event(url, html, listing_meta) -> RawItem`

`src/config.py` (P2.5):
- В `CompanyCfg` добавляем `e_disclosure_id: str | None = None`
- В `load_config`: `e_disclosure_id=c.get("e_disclosure_id")` (валидация ленивая —
  в Source `__init__`)

`src/fetcher.py`:
- `SOURCE_REGISTRY["e_disclosure"] = EDisclosureSource`
- `from src.sources.e_disclosure import EDisclosureSource`

`config.yaml` + `.example`:
```yaml
companies:
  - name: X5
    ...
    e_disclosure_id: "<из T7.2>"
    sources: [x5_ir, rbc, e_disclosure]

sources:
  ...
  e_disclosure:
    code: e_disclosure
    name: e-disclosure.ru
    base_url: https://www.e-disclosure.ru/
    parser: e_disclosure
    enabled: true
```

`requirements.txt`:
- `playwright>=1.40` (P2.8 — после T7.2 заменим на точный pin)

**Acceptance T7.4:**
- Тесты T7.5 проходят
- e2e ручной запуск отрабатывает, в БД появляются e_disclosure-строки

### T7.5 — Тесты (0.5-1 день)

`tests/test_e_disclosure.py`:

**Парсинг listing (на фикстуре из T7.2):**
- `test_parse_listing_extracts_events`
- `test_parse_listing_dates_to_utc` (Moscow → UTC)
- `test_parse_listing_event_types`
- `test_parse_listing_skips_malformed_row`

**Парсинг event-page:**
- `test_parse_event_extracts_fields`
- `test_parse_event_body_strips_navigation`
- `test_parse_event_missing_date_raises_parse_error`

**`EDisclosureSource` lifecycle / валидация:**
- `test_edisclosure_raises_without_context`
- `test_edisclosure_raises_without_e_disclosure_id`
- `test_edisclosure_raises_on_non_digit_id` (`"TODO"`, `"1380 "`, full URL)
- `test_edisclosure_normalizes_id_with_strip`

**Fetch flow с моком Playwright:**
- `test_edisclosure_fetch_warmup_verifies_cookies` — мок cookies без spsn → `_ChallengeFailure`
- `test_edisclosure_fetch_bulk_dedup_skips_known_urls` — known_urls set заполнен,
  event с url в set не приводит к page.goto
- `test_edisclosure_fetch_paginates_until_since` — мок 3 страницы, последняя
  старше since → loop останавливается
- `test_edisclosure_fetch_max_pages_hard_cap` — мок бесконечной пагинации,
  loop останавливается на MAX_PAGES
- `test_edisclosure_fetch_classifies_errors` — мок page.goto raise разные ошибки,
  каждая попадает в свой counter

**Acceptance T7.5:**
- ~15 новых тестов зелёные
- coverage `e_disclosure.py` ≥ 85%
- `pytest tests/ -q` — все ~74 теста зелёные
- `ruff` + `mypy` чисто

### T7.6 — E2E + backfill (0.5-1 день)

1. Установить Playwright если не сделано: `pip install -r requirements.txt && python -m playwright install chromium`
2. `python -m src init-db` (sources.e_disclosure появится в БД)
3. `python -m src fetch --company X5` — backfill с 2026-05-01:
   - x5_ir отрабатывает как раньше
   - rbc отрабатывает как раньше
   - **e_disclosure** делает: warmup → bulk dedup → pagination → 5-15 раскрытий
4. `python -m src analyze --company X5` — все новые items через GPT-5 mini
5. `python -m src report --company X5` — пересборка `output/X5/`
6. Глазами проверить:
   - В `output/X5/news_list/data.xlsx` строки с `source=e_disclosure`
   - 5-10 MD-файлов в Obsidian — тон / mood_reason адекватны
   - Сравнить с реальными раскрытиями X5 на e-disclosure.ru (визуально через
     browser, заодно проверить что мы ничего не потеряли)
7. `ruff` + `mypy` на полном проекте — чисто
8. `python -m src status` — `e_disclosure: N` для X5
9. **Pin playwright version** (P2.8): обновить `requirements.txt` на точный pin
   из `pip show playwright | grep Version`

**Acceptance T7.6:**
- e2e без ошибок
- N (количество X5-раскрытий с 2026-05-01) ≥ 5
- `e_disclosure fetch summary` log с разумными числами (warmup_ms, kept,
  pages_seen, error counters)
- README дополнен `python -m playwright install chromium` step
- `playwright==<tested>` в `requirements.txt`

---

## Что НЕ делаем (явно отложено)

- ❌ Async Playwright — наш проект синхронный
- ❌ Playwright для www.rbc.ru — отдельная мини-спека после ship 03
- ❌ Persistent BrowserContext (на диск) — отложено до **P2.4 fallback** при
  росте challenge solve time или failures
- ❌ playwright-stealth — добавим если T7.2 покажет что servicepipe detect headless
- ❌ Скачивание PDF/attachment'ов из раскрытий — только text из event-page
  (см. docstring `EDisclosureSource`)
- ❌ Multiple companies per fetch — одна за раз
- ❌ ML-классификация типов раскрытий — берём type из e-disclosure
- ❌ Browser-crash auto-recover (P3.1 lightweight) — детектим, log, raise.
  Recovery / recreate браузера — отдельная задача если понадобится.

---

## Риски и митигации

| Риск | Митигация (v2) |
|---|---|
| Servicepipe детектит headless | **T7.2 матрица** проверит; fallback `playwright-stealth` или `headless=False` |
| Playwright cross-OS install (Windows) | T7.2 первым делом проверяет реально |
| Browser launch fails partway | `__enter__ try/except: close(); raise` (P1.3) |
| Pagination silent truncation | MAX_PAGES + `events[-1].published_at < since` break (P1.5) |
| Challenge solve time растёт | Telemetry в `_verify_warmup_success` (P3.2); fallback на persistent context |
| `e_disclosure_id` опечатка / TODO | Validation в `EDisclosureSource.__init__` (P2.5) |
| Стале cookies между cycles | In-memory context — каждый cycle с нуля, 6 challenges/day acceptable |
| Browser crashes mid-fetch | Detect через `_BrowserDead`, source aborts, fetcher logs error |
| Listing HTML structure меняется | Офлайн тесты на фикстурах + structured fetch_summary log |
| Disclosure body changed после insert | Игнорируем (раскрытия не редактируются); UNIQUE constraint спасает от дублей |
| Время cycle (5-10× медленнее httpx) | Acceptable: 30-60s на cycle. 4-часовая cadence — нет давления на latency. |
| Memory ~200MB Chromium | Acceptable; browser закрывается после fetch (in-memory context P6) |

---

## Acceptance (план в целом)

После T7.6:
1. `pytest tests/ -q` — ~74+ теста зелёные (59 старых + ~15 новых)
2. `ruff check src/ tests/` — clean
3. `mypy src/ --ignore-missing-imports` — no issues
4. `python -m src fetch --company X5` — три источника отрабатывают
5. В БД ≥5 e_disclosure-раскрытий X5 за период с 2026-05-01
6. В `output/X5/news_list/data.xlsx` — строки с `source=e_disclosure`
7. `EDISCLOSURE_RECON.md` дополнен после T7.2 (X5 ID, ИНН, ОГРН, production mode, селекторы, pagination)
8. `tests/fixtures/edisclosure_listing.html` + `edisclosure_event.html` зафиксированы
9. `requirements.txt` содержит `playwright==<tested>` (точный pin после T7.2)
10. README дополнен `python -m playwright install chromium` step
11. Smoke test `pytest tests/ -m smoke` запускается вручную и проходит

---

## Оценка времени (v2 — расширена по P3.4)

| Фаза | Оптимистично | 90-й перцентиль |
|---|---|---|
| T7.1 Recon httpx | ✅ DONE | |
| T7.2 Playwright live recon + матрица + acceptance gate | 0.5-1 день | 1.5 дня (если stealth/firefox tuning) |
| T7.3 PlaywrightSource ABC + 10 unit + 1 smoke | 1-1.5 дня | 2 дня |
| T7.4 EDisclosureSource impl + config + registry | 1-1.5 дня | 2 дня |
| T7.5 Тесты (~15) | 0.5-1 день | 1.5 дня |
| T7.6 E2E backfill + pin + README | 0.5-1 день | 1.5 дня |
| **ИТОГО** | **4-5 дней** | **8-10 дней** |

90-й перцентиль: если в T7.2 ни один режим Playwright не пробивает servicepipe
→ STOP + альтернатива (skrin.ru / disclosure.skrin.ru / другой диссеминатор) → +3-5 дней recon.

---

## Что после T7.6

1. `/review` Claude → `reviews/03_claude_e_disclosure_rew.md`
2. `/codex review` → `reviews/03_codex_e_disclosure_rew.md`
3. `/cso` → `security/03_e_disclosure_sec.md`
4. `/health` — финальный composite dashboard
5. PR `e_disclosure_news → master`
6. После merge — решаем что дальше:
   - Задача 04 (Interfax / Kommersant — теперь через httpx если RSS, или
     PlaywrightSource если WAF)
   - www.rbc.ru через PlaywrightSource (инфра уже готова)
   - Telegram-каналы (другой класс источника)
