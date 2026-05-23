# Plan 04 — finam.ru source + Playwright + item_type classification

Статус: READY (v2 — после `/codex consult` критики, 7 P1 + 10 P2 + 5 P3 приняты)
Дата: 2026-05-21
Версия: **v2** (см. секцию «Изменения v1 → v2» ниже)
Связанная спека: `specs/04_finam_spec.md` (APPROVED)
Связанная оценка: `estimates/04_codex_finam_est.md` (все P1 закрыты)
Связанный recon: `tests/fixtures/EDISCLOSURE_RECON.md` §6 (Playwright + stealth lessons из T7.2 — reusable 1:1)
Заменяет: `plans/03_claude_e_disclosure_plan.md` (SUPERSEDED — pivot на finam)
Ветка: `finam_news` (переименована из `e_disclosure_news` после P1.7)

---

## Изменения v1 → v2 (по codex P1+P2)

| # | Тема | Изменение |
|---|---|---|
| P1.1 | Pre-filter listing (variant A) | Добавлен `_FINAM_RELEVANT_SLUG_PARTS` allowlist + `_slug_relevant()` helper; фильтр на slug ДО per-article `_goto`. Резко режет SpaceX/ETH/золото из broad-market listing'а. |
| P1.2 | item_type criterion crisp | SYSTEM_PROMPT расширен с явной definition + 5 negative examples (macro note, earnings preview, article про другой issuer без X5 stance, generic «дорого», «держать» не про stock). |
| P1.3 | Golden set для classification | T8.4 acceptance: 5 fixture-based examples с expected `item_type`; pass если ≥4/5 правильно. |
| P1.4 | DB migration design fix | column-presence-aware migration через `PRAGMA table_info(news)` check + separate fresh-DB vs upgrade paths. |
| P1.5 | URL date vs meta verification | T8.2 acceptance расширен: sample 5 URLs, compare URL date vs article visible date, document tolerance. |
| P1.6 | Backfill coverage proof | T8.2 acceptance: parse все listing dates, sort newest-first, доказать oldest ≤ 2026-05-01. |
| P1.7 | Rename branch | ✅ DONE (`finam_news`) |
| P2.x | Import PlaywrightTimeoutError, challenge_failures counter, WARMUP_SELECTOR specific, short-body default, re-analysis policy, header-order test, Honest perf estimate | Применено по тексту плана. |
| P3.x | Install sanity earlier (T8.3), estimate 6-8/11-14 days | Применено. |

---

---

## Что строим

В одном PR (вариант **A bundle**):

1. **Инфраструктура:** `PlaywrightSource(Source)` — базовый класс для anti-bot защищённых источников. Архитектура из плана 03 v3, с правками из T7.2 recon:
   - `playwright-stealth` оборачивает `sync_playwright()` через `Stealth().use_sync(...)`
   - `_goto(url, wait="domcontentloaded")` + `time.sleep(WARMUP_SLEEP_S)` (NOT `networkidle`)
   - `_verify_warmup_success(expected_selector)` — проверка html size, отсутствия servicepipe markers, видимости селектора
   - try/except в `__enter__`, close() обнуляет fields, custom exceptions

2. **Первый консьюмер:** `FinamSource(PlaywrightSource)` — берёт публикации с `/quote/moex/<ticker>/publications/`.
   - 70 items на странице (без pagination для MVP)
   - Дата извлекается **из URL** (`<slug>-YYYYMMDD-HHMM/`) — фильтрация по `since` без открытия каждой статьи
   - **Без keyword filter** (P2 spec — берём всё)

3. **Item type классификация** (P9 spec — новая архитектура):
   - `news.item_type TEXT NOT NULL DEFAULT 'news'` (миграция через PRAGMA user_version)
   - Analyzer LLM JSON-output расширяется: `+item_type: 'news' | 'recommendation'`
   - Reporter пишет в **разные папки** `output/<COMPANY>/{news,recommendations}/`
   - Excel — одна таблица + новая колонка `item_type`

---

## Архитектурное место

```
src/sources/base.py                       ← без изменений
src/sources/playwright_base.py            ← НОВЫЙ — PlaywrightSource(Source) ABC
src/sources/finam.py                      ← НОВЫЙ — FinamSource(PlaywrightSource)
src/sources/x5_ir.py, rbc.py              ← без изменений
src/fetcher.py                            ← +1 строка SOURCE_REGISTRY
src/config.py                             ← +CompanyCfg.finam_ticker (с валидацией)
src/db.py                                 ← +ALTER TABLE news ADD COLUMN item_type (миграция)
src/analyzer.py                           ← SYSTEM_PROMPT расширяется, JSON parsing +item_type
src/reporter.py                           ← разделение записи на news/ vs recommendations/

config.yaml + .example                    ← +sources.finam, +companies[X5].finam_ticker, +companies[X5].sources += [finam]
requirements.txt                          ← +playwright>=1.40, +playwright-stealth>=2.0

tools/probe_edisclosure.py                ← оставляем как историю (deprecated; полезный пример Playwright probe)
tools/probe_finam.py                      ← НОВЫЙ — для T8.2 (открытие одной статьи)

tests/fixtures/finam_x5_publications.html ← УЖЕ СОХРАНЁН (T8.1)
tests/fixtures/finam_article.html         ← НОВЫЙ — снимок одной статьи (T8.2)
tests/fixtures/FINAM_RECON.md             ← НОВЫЙ — выделить findings отдельно от EDISCLOSURE

tests/test_playwright_base.py             ← НОВЫЙ — unit-тесты ABC
tests/test_finam.py                       ← НОВЫЙ — parsing + fetch с моком
tests/test_db_migrations.py               ← НОВЫЙ — миграция item_type корректна

tests/test_analyzer.py, test_reporter.py  ← РАСШИРЯЮТСЯ под item_type (новые тесты, старые остаются)
```

---

## Контракт `PlaywrightSource` (как в плане 03 v3, с T7.2-правками)

```python
# src/sources/playwright_base.py
import os, random, time, logging
from abc import ABC
from typing import Iterable

from playwright.sync_api import sync_playwright, Error as PlaywrightError
from playwright_stealth import Stealth

from src.sources.base import RawItem, Source, FetchContext

log = logging.getLogger(__name__)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
WARMUP_SLEEP_S = 8        # T7.2 finding: domcontentloaded не дожидается JS challenge resolve
ARTICLE_SLEEP_S = 3       # короче для последующих per-article goto


class _ChallengeFailure(Exception):
    """Warmup завершился без признаков прохождения WAF challenge."""


class _BrowserDead(Exception):
    """Browser/page закрылся вне нашего control."""


class PlaywrightSource(Source, ABC):
    DEFAULT_TIMEOUT_MS = 30_000
    MIN_PAUSE_S, MAX_PAUSE_S = 1.0, 3.0

    def __init__(self, base_url, user_agent=None, context=None):
        super().__init__(base_url=base_url, user_agent=user_agent, context=context)
        self._sp = None       # sync_playwright() context manager
        self._stealth_cm = None
        self._pw = None
        self._browser = None
        self._bcontext = None
        self._page = None
        self._requests_made = 0

    def __enter__(self) -> "PlaywrightSource":
        try:
            headless = os.environ.get("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
            self._sp = sync_playwright()
            self._stealth_cm = Stealth().use_sync(self._sp)
            self._pw = self._stealth_cm.__enter__()
            self._browser = self._pw.chromium.launch(headless=headless)
            self._bcontext = self._browser.new_context(
                user_agent=self.user_agent or _DEFAULT_UA,
                viewport={"width": 1280, "height": 800},
                locale="ru-RU",
                timezone_id="Europe/Moscow",
            )
            self._page = self._bcontext.new_page()
            log.info("playwright: launched chromium headless=%s", headless)
            return self
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        for attr in ("_page", "_bcontext", "_browser"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try: obj.close()
                except Exception: pass
                setattr(self, attr, None)
        if self._stealth_cm is not None:
            try: self._stealth_cm.__exit__(None, None, None)
            except Exception: pass
            self._stealth_cm = None
            self._pw = None
            self._sp = None

    def _polite_pause(self) -> None:
        time.sleep(random.uniform(self.MIN_PAUSE_S, self.MAX_PAUSE_S))

    def _goto(self, url: str, sleep_s: float = ARTICLE_SLEEP_S) -> str:
        if self._page is None or self._browser is None:
            raise _BrowserDead("browser/page is None")
        try:
            if self._requests_made > 0:
                self._polite_pause()
            self._requests_made += 1
            self._page.goto(url, timeout=self.DEFAULT_TIMEOUT_MS, wait_until="domcontentloaded")
            # T7.2 finding: networkidle не работает, домкл + sleep — рабочий паттерн
            time.sleep(sleep_s)
            return self._page.content()
        except PlaywrightError as exc:
            msg = str(exc).lower()
            if "closed" in msg or "target page" in msg:
                raise _BrowserDead(str(exc)) from exc
            raise

    def _verify_warmup_success(self, expected_selector: str) -> dict:
        """После warmup goto — убедиться что не challenge.
        Returns telemetry. Raises _ChallengeFailure если challenge виден.
        """
        t = time.monotonic()
        cookies = sorted({c["name"] for c in self._bcontext.cookies()})
        html = self._page.content()
        is_challenge = (
            "servicepipe.ru" in html or "id_spinner" in html or len(html) < 2500
        )
        try:
            selector_visible = self._page.is_visible(expected_selector, timeout=5000)
        except PlaywrightError:
            selector_visible = False
        telemetry = {
            "warmup_check_ms": int((time.monotonic() - t) * 1000),
            "cookies_count": len(cookies),
            "cookies_sample": cookies[:6],
            "is_challenge": is_challenge,
            "selector_visible": selector_visible,
            "html_size": len(html),
        }
        log.info("playwright warmup verify: %s", telemetry)
        if is_challenge or not selector_visible:
            raise _ChallengeFailure(
                f"warmup failed: challenge={is_challenge} selector={selector_visible}"
            )
        return telemetry
```

---

## Контракт `FinamSource`

```python
# src/sources/finam.py
"""finam.ru — публикации по тикеру через Playwright (servicepipe WAF).

Pre-filter (codex P1.1): listing содержит broad-market noise (SpaceX, ETH, золото).
Применяем slug-level allowlist `_FINAM_RELEVANT_SLUG_PARTS` до per-article goto.
Classification news vs recommendation — LLM analyzer (по item_type в JSON).

URL pattern: /publications/item/<slug>-YYYYMMDD-HHMM/
Дата извлекается из URL — фильтр по since без открытия статьи.
"""

import re, sqlite3
from datetime import datetime, timezone
from typing import Iterable, NamedTuple
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # codex P2.6

from src.sources.base import RawItem
from src.sources.playwright_base import (
    PlaywrightSource, _ChallengeFailure, _BrowserDead, WARMUP_SLEEP_S, ARTICLE_SLEEP_S,
)

LISTING_PATH = "/quote/moex/{ticker}/publications/"
WARMUP_SELECTOR = '[itemtype*="ItemList"], .publications, article'  # уточняется в T8.2 — selector привязанный к listing'у, не generic "h1"
_URL_DATE_RE = re.compile(r"-(\d{8})-(\d{4})/?$")  # YYYYMMDD-HHMM

# Slug relevance filter (codex P1.1):
# Finam slugs — Latin-transliterated. AFK Sistema = "afk-sistema", Пятёрочка = "pyaterochka", etc.
# Без фильтра 70 items включают SpaceX/Amazon/Ethereum/золото — noise.
# Этот список — strong allowlist на slug-level. Word boundaries через "-" (slug separator).
_FINAM_RELEVANT_SLUG_PARTS = [
    # X5 direct identifiers
    "x5", "iks-5", "iks5", "korporativnyy-centr-iks",
    # X5 brand names (Latin)
    "pyatero", "pyaterochk", "perekr", "perekrestok", "perekrjostok", "chizhik",
    # Russian retail competitors (для context — competitor news часто влияет на X5 котировки)
    "magnit", "lenta", "okey", "o-key", "fix-price", "fixprice", "vkusvill",
    # X5 ownership / holdings (codex отметил AFK Sistema → X5 recommendation case)
    "afk-sistema", "sistema",
    # Retail-sector general keywords
    "ritejl", "ritail", "prodovolstvenn", "fmcg",
]


def _slug_relevant(slug: str) -> bool:
    """True if slug содержит one of _FINAM_RELEVANT_SLUG_PARTS as a hyphen-bounded token."""
    s = slug.lower()
    for part in _FINAM_RELEVANT_SLUG_PARTS:
        # Word boundary через "-" или начало/конец строки
        pattern = r'(?:^|-)' + re.escape(part) + r'(?:-|$)'
        if re.search(pattern, s):
            return True
    return False


class _ParseError(Exception):
    pass


class ListingHit(NamedTuple):
    url: str
    slug: str
    published_at: datetime  # parsed from URL — Moscow time → UTC


class FinamSource(PlaywrightSource):
    code = "finam"

    def __init__(self, base_url, user_agent=None, context=None):
        if context is None:
            raise ValueError("FinamSource requires FetchContext")
        ticker = context.company_cfg.finam_ticker
        if not ticker:
            raise ValueError(
                f"company {context.company_cfg.name!r} has no finam_ticker "
                "configured (set companies[X].finam_ticker in config.yaml)"
            )
        ticker = str(ticker).strip().lower()
        if not re.fullmatch(r"[a-z0-9]+", ticker):
            raise ValueError(
                f"finam_ticker must be [a-z0-9]+ (got {context.company_cfg.finam_ticker!r})"
            )
        super().__init__(base_url=base_url, user_agent=user_agent, context=context)
        self._ticker = ticker

    def fetch(self, since):
        # 1. Warmup на главной (servicepipe challenge через stealth)
        self._goto(self.base_url, sleep_s=WARMUP_SLEEP_S)
        warmup = self._verify_warmup_success(WARMUP_SELECTOR)

        # 2. Bulk-load known URLs (P1.4 из codex est)
        with sqlite3.connect(self.context.db_path) as conn:
            known_urls = {row[0] for row in conn.execute(
                "SELECT url FROM news WHERE source_id=?",
                (self.context.source_id,),
            )}

        # 3. Открываем listing
        listing_url = urljoin(
            self.base_url, LISTING_PATH.format(ticker=self._ticker),
        )
        html_listing = self._goto(listing_url, sleep_s=WARMUP_SLEEP_S)
        hits = list(_parse_listing(html_listing, base_url=self.base_url))

        stats = {
            "listing_hits": len(hits), "relevance_filtered": 0,
            "date_filtered": 0, "already_in_db": 0,
            "fetched": 0, "kept": 0,
            "timeout_errors": 0, "browser_dead": 0,
            "challenge_failures": 0, "parse_errors": 0,
        }

        # 4. Per-article fetch — filter by relevance, date, dedup BEFORE goto
        results = []
        for hit in hits:
            # 4a. Slug relevance (codex P1.1) — режет broad-market noise до per-article goto
            if not _slug_relevant(hit.slug):
                stats["relevance_filtered"] += 1
                continue
            # 4b. Date filter (из URL — без открытия статьи)
            if hit.published_at < since:
                stats["date_filtered"] += 1
                continue
            # 4c. Bulk dedup
            if hit.url in known_urls:
                stats["already_in_db"] += 1
                continue
            try:
                article_html = self._goto(hit.url, sleep_s=ARTICLE_SLEEP_S)
                item = _parse_article(hit.url, article_html, listing_meta=hit)
                results.append(item)
                stats["fetched"] += 1
                stats["kept"] += 1
            except _BrowserDead:
                stats["browser_dead"] += 1
                raise
            except _ChallengeFailure:
                stats["challenge_failures"] += 1
                # bubble up — challenge на per-article это сигнал что сессия умерла
                raise
            except PlaywrightTimeoutError:
                stats["timeout_errors"] += 1
            except _ParseError:
                stats["parse_errors"] += 1

        log.info("finam fetch summary: %s | warmup=%s", stats, warmup)
        return results


# ---- module-level helpers (testable on fixtures, no browser needed) ----

def _parse_listing(html: str, base_url: str) -> Iterable[ListingHit]:
    """Из HTML страницы компании извлечь все /publications/item/<slug>-YYYYMMDD-HHMM/.
    Селектор + parsing — уточняются в T8.2.
    """
    # Прототип; реальные селекторы из tests/fixtures/finam_x5_publications.html
    pattern = r'href="(/publications/item/[a-z0-9\-_]+-(\d{8})-(\d{4})/)"'
    for m in re.finditer(pattern, html):
        path, ymd, hm = m.group(1), m.group(2), m.group(3)
        # YYYYMMDD-HHMM в Moscow time → UTC (-3 hours)
        try:
            dt_msk = datetime.strptime(f"{ymd}{hm}", "%Y%m%d%H%M")
        except ValueError:
            continue
        # Moscow time (UTC+3) → UTC
        from datetime import timedelta
        dt_utc = (dt_msk - timedelta(hours=3)).replace(tzinfo=timezone.utc)
        url = urljoin(base_url, path)
        slug = path.split("/")[-2] if path.endswith("/") else path.rsplit("/", 1)[-1]
        yield ListingHit(url=url, slug=slug, published_at=dt_utc)


def _parse_article(url: str, html: str, listing_meta: ListingHit) -> RawItem:
    """Парсинг одной статьи finam.
    Селекторы (заголовок, тело, дата) — уточняются в T8.2.
    """
    # ... (прототип, уточняется в T8.2)
    ...
```

---

## Изменения в `analyzer.py` (item_type classification — codex P1.2 crisp criterion)

```python
# Расширение SYSTEM_PROMPT — crisp definition + negative examples
SYSTEM_PROMPT = """... existing prompt ...

Дополнительно классифицируй item по полю "item_type" (строго одно из двух):

"recommendation" — публикация с **EXPLICIT investment recommendation** про конкретную
ценную бумагу или эмитента, СОДЕРЖАЩАЯ И stance (buy/sell/hold), И rationale (target
price / upside / downside / явная аргументация перспективы).

Примеры "recommendation":
- «Брокер X повысил target по акциям X5 до 5000 руб, рекомендация — buy»
- «Акции AFK Sistema не выглядят привлекательным объектом для портфельных инвестиций
   из-за <причина>; рекомендация — sell» (даже если статья не про X5 напрямую)
- «Покупать X5: компания недооценена, потенциал роста 30%»

"news" — всё остальное. В частности (negative examples — это НЕ recommendation):
- Earnings preview без рекомендации («X5 опубликует отчёт 29 апреля»)
- Macro note / макрообзор («ЦБ обсуждает ставку»)
- Article про другого эмитента БЕЗ X5 рекомендации внутри («AFK Sistema отчиталась за квартал»)
- Generic valuation comment без stance («акции выглядят дорого»)
- «Держать» вне stock context («ЦБ решил держать ставку», «инвестор держит позицию»)
- Просто пресс-релиз компании, корпоративное событие

Если сомневаешься — "news". Только actionable recommendation = "recommendation".

В JSON-output добавь поле "item_type" со значением "news" или "recommendation".
"""

# JSON-validation
VALID_ITEM_TYPES = {"news", "recommendation"}

def _analyze_one(...):
    ...
    parsed = json.loads(content)
    mood = parsed["mood"]
    item_type = parsed.get("item_type", "news")  # default safe
    if mood not in VALID_MOODS: raise ParseError(...)
    if item_type not in VALID_ITEM_TYPES: raise ParseError(...)
    conn.execute(
        "UPDATE news SET status='analyzed', mood=?, mood_reason=?, item_type=?, tokens_used=? WHERE id=?",
        (mood, reason, item_type, tokens, news_id),
    )
```

---

## Изменения в `db.py` (миграция item_type — codex P1.4 architecture)

**Текущая проблема:** существующий `init_db` всегда выполняет
`conn.executescript(SCHEMA_SQL) + PRAGMA user_version = SCHEMA_VERSION`.
Если просто добавить `item_type` в SCHEMA_SQL **И** параллельно ALTER при `user_version < 2`,
fresh v0 БД получит колонку через `executescript` → потом попытка ALTER → **error «column already exists»**.

**Правильная архитектура (column-presence-aware):**

```python
SCHEMA_VERSION = 2  # bump from 1

# SCHEMA_SQL для FRESH DB уже содержит item_type
# (нужно добавить в CREATE TABLE news блок:
#    item_type TEXT NOT NULL DEFAULT 'news',
#  )

def init_db(cfg):
    conn = db.connect(cfg.db_path)
    try:
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if user_version == 0:
            # Fresh DB — SCHEMA_SQL уже v2 (содержит item_type column)
            conn.executescript(SCHEMA_SQL)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            log.info("db: created fresh v%d schema", SCHEMA_VERSION)
        else:
            # Existing DB — apply migrations incrementally
            if user_version < 2:
                _migrate_v1_to_v2(conn)
                conn.execute("PRAGMA user_version = 2")
                log.info("db: migrated v1 → v2")
        # ... остальная init_db логика (seed import, etc.) идёт после миграции ...
        _import_companies(conn, cfg)
        _import_seed_persons(conn, cfg)
        conn.commit()
    finally:
        conn.close()


def _migrate_v1_to_v2(conn):
    """Add news.item_type column if absent. Idempotent."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(news)")}
    if "item_type" not in columns:
        conn.execute(
            "ALTER TABLE news ADD COLUMN item_type TEXT NOT NULL DEFAULT 'news'"
        )
        log.info("db: added news.item_type column (v1 → v2)")
```

**Свойства миграции:**
- ✅ Idempotent — повторный init_db на v2 БД no-op
- ✅ Fresh DB → straight v2 schema через executescript
- ✅ Existing v1 → column-presence check + ALTER только если absent
- ✅ Existing v2 → no migration, just (re-)import seeds

Существующие `x5_ir` / `rbc` rows дефолтятся как `'news'` — safe (всё DEFAULT 'news').

---

## Изменения в `reporter.py` (две папки)

```python
def _write_news_md_files(conn, company_name, output_root, timezone_):
    # Wipe both subdirs
    news_dir = output_root / company_name / "news"
    rec_dir = output_root / company_name / "recommendations"
    for d in (news_dir, rec_dir):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    rows = conn.execute(
        "SELECT ... item_type ... FROM news WHERE company_id=? AND status='analyzed' "
        "ORDER BY published_at"
    ).fetchall()
    for row in rows:
        target_root = news_dir if row["item_type"] == "news" else rec_dir
        target = target_root / yyyy / yyyy_mm / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(frontmatter + body, encoding="utf-8")
```

Excel — добавляется колонка `item_type` рядом с `mood`.

---

## Этапы

### T8.1 — ПРОЙДЕН
Live probe finam.ru со stealth — пробивает servicepipe, X5 publications page открыт, 70 article links видны, URL date pattern зафиксирован. См. `tests/fixtures/finam_x5_publications.html`.

### T8.2 — Live recon одной статьи + RECON.md (0.5 дня)

1. Написать `tools/probe_finam.py` — открывает одну статью X5 и одну «не-X5» (recommendation candidate):
   - `https://www.finam.ru/publications/item/29-aprelya-x5-predstavit-finansovye-rezultaty-za-1-kvartal-po-msfo-20260429-0524/`
   - `https://www.finam.ru/publications/item/aktsii-afk-sistema-ne-vyglyadyat-privlekatelnym-obektom-dlya-portfelnykh-investitsiy-20260417-0950/`
2. Сохранить HTML обеих в `tests/fixtures/finam_article_news.html`, `finam_article_recommendation.html`
3. Зафиксировать в `tests/fixtures/FINAM_RECON.md`:
   - Селектор заголовка (`<h1>`)
   - Селектор тела статьи (вероятно `.article__body` / `.content` / `[itemprop=articleBody]` — уточнить в живом probe)
   - Селектор даты публикации (meta tag или inline)
   - Селекторы для удаления (баннеры, реклама, «Похожие статьи»)
   - WARMUP_SELECTOR — выбрать стабильный (например, `h1` на listing page работает)
   - Время прохождения challenge на типичной странице (warmup_ms)
   - Cookies set после warmup

**Acceptance T8.2:**
- ✅ 2+ article HTML фикстуры сохранены (news-type + recommendation-type)
- ✅ FINAM_RECON.md заполнен селекторами, cookies, capture date
- ✅ Selector hypothesis для `_parse_article` подтверждена на двух разных типах статей
- ✅ **WARMUP_SELECTOR выбран** — не «h1» (codex P2.6 — слабый), а selector привязанный к listing'у (например, `[itemtype*="ItemList"]` или специфичный class страницы)
- ✅ **URL date vs article meta verification** (codex P1.5): sample 5 URLs (oldest, newest, AFK Sistema, X5 direct, средний по позиции в listing). Для каждого compare URL `YYYYMMDD-HHMM` vs visible date в article body / meta tag. Document **timezone** (Moscow inferred) и **tolerance** (например, ≤60 мин mismatch = OK; больше → пересмотр)
- ✅ **Backfill coverage proof** (codex P1.6): parse все 70 listing dates, sort newest-first, доказать что oldest URL date ≤ 2026-05-01. Если нет — STOP, требуется pagination (отдельная мини-спека)
- ✅ Items-per-week histogram сохранён в FINAM_RECON.md — confirm нет silent truncation в busy weeks

### T8.3 — `PlaywrightSource` ABC + smoke (1-1.5 дня)

`src/sources/playwright_base.py` (см. контракт выше).

`tests/test_playwright_base.py`:
- `test_playwright_base_requires_subclass_to_implement_fetch`
- `test_close_idempotent_after_partial_init` — `__enter__` падает посередине → `close()` cleans up, fields = None
- `test_close_swallows_exceptions`
- `test_polite_pause_called_between_gotos` — mock time.sleep
- `test_headless_env_override` — `PLAYWRIGHT_HEADLESS=false`
- `test_verify_warmup_success_passes_on_good_state`
- `test_verify_warmup_success_raises_on_challenge_page`
- `test_verify_warmup_success_raises_on_invisible_selector`
- `test_goto_raises_browser_dead_on_closed`
- `test_goto_sleeps_after_domcontentloaded` — mock time.sleep, assert called with sleep_s

Smoke (opt-in `@pytest.mark.smoke`):
- `test_playwright_smoke_example_com` — реальный `page.goto("https://example.com")`

**Acceptance T8.3:**
- ~10 unit + 1 smoke зелёные
- `ruff` + `mypy` чисто
- Старые 59 тестов проходят
- **Install sanity (codex P3.4):** на старте T8.3 — `pip install -r requirements.txt` + `python -m playwright install chromium` + smoke (`pytest tests/ -m smoke`) — всё работает on fresh `.venv` checkpoint

### T8.4 — `db.py` migration + `analyzer.py` extension (0.5 дня)

**db.py:**
- `SCHEMA_VERSION = 2`, миграция `ALTER TABLE news ADD COLUMN item_type ...`
- Idempotent (повторный init-db не падает)

**analyzer.py:**
- Расширение `SYSTEM_PROMPT` (см. выше)
- `_analyze_one`: парсит `item_type` из JSON, валидирует, апдейтит row
- `VALID_ITEM_TYPES = {"news", "recommendation"}`

`tests/test_db_migrations.py`:
- `test_init_db_creates_item_type_column_for_new_db`
- `test_init_db_migrates_existing_v1_db` (создаём БД с PRAGMA user_version=1 без колонки, потом init_db, проверяем что колонка добавилась + дефолт 'news')
- `test_init_db_idempotent_on_v2` (двойной init-db не падает)

`tests/test_analyzer.py` (расширение):
- `test_analyzer_extracts_item_type_recommendation` — мок LLM возвращает `"item_type": "recommendation"`, проверяем что записалось в БД
- `test_analyzer_defaults_item_type_to_news_if_missing` — мок LLM возвращает без `item_type`, fallback to 'news'
- `test_analyzer_rejects_invalid_item_type` — мок возвращает `"item_type": "garbage"`, row → status='error'

**Acceptance T8.4:**
- Новые тесты зелёные, старые 33 (test_analyzer) проходят
- Миграция работает на свежей БД (v0) и на старой v1 (idempotent)
- **Golden set classification eval (codex P1.3):** 5 fixture-based примеров с expected `item_type`:
  1. X5 earnings preview → `news`
  2. AFK Sistema article с recommendation про X5 → `recommendation`
  3. Quantum/SpaceX/gold noise (без X5 stance) → `news` (но в fetch отсеется фильтром)
  4. Broker target-price article про X5 → `recommendation`
  5. Article с «держать» НЕ про stock stance (например, «ЦБ держит ставку») → `news`
  
  **Pass criterion:** ≥4/5 правильно при прогоне через реальный analyzer (моки OpenAI с заготовленными response'ами, тестирующими только parsing/validation; реальный LLM call — manual eval один раз).
- Short-body analyzer path сохраняет `item_type='news'` дефолт + есть тест на это (codex P2.8)

### T8.5 — `reporter.py` two-folder split + Excel column (0.5-1 день)

`reporter.py`:
- Wipe + recreate `news/` AND `recommendations/` subdirs per company
- Route MD-файлы по `item_type` в правильную папку
- Excel: добавить колонку `item_type` (после `mood`)

`tests/test_reporter.py` (расширение):
- `test_report_writes_news_to_news_folder`
- `test_report_writes_recommendation_to_recommendations_folder`
- `test_report_wipes_both_folders_before_regen`
- `test_report_excel_has_item_type_column`

**Acceptance T8.5:** тесты зелёные; ручной запуск `report` создаёт обе папки если есть items обоих типов.

### T8.6 — `FinamSource` impl + config + registry (1 день)

`src/sources/finam.py` (см. контракт выше).

`src/config.py`:
- `CompanyCfg.finam_ticker: str | None = None`
- В `load_config`: `finam_ticker=c.get("finam_ticker")`

`src/fetcher.py`:
- `SOURCE_REGISTRY["finam"] = FinamSource`

`config.yaml` + `.example`:
```yaml
companies:
  - name: X5
    ...
    finam_ticker: "x5"
    sources: [x5_ir, rbc, finam]

sources:
  finam:
    code: finam
    name: Финам
    base_url: https://www.finam.ru/
    parser: finam
    enabled: true
```

`requirements.txt`: `+playwright>=1.40`, `+playwright-stealth>=2.0`.

**Acceptance T8.6:** тесты T8.7 проходят; e2e ручной запуск отрабатывает.

### T8.7 — Тесты finam (0.5-1 день)

`tests/test_finam.py`:

**Парсинг listing (fixture finam_x5_publications.html):**
- `test_parse_listing_extracts_70_items`
- `test_parse_listing_url_date_to_utc` — `20260429-0524` Moscow → 2026-04-29 02:24 UTC
- `test_parse_listing_skips_invalid_date_in_url`
- `test_parse_listing_dedups`

**Парсинг article (fixtures из T8.2):**
- `test_parse_article_news_extracts_fields`
- `test_parse_article_recommendation_extracts_fields`
- `test_parse_article_missing_body_raises`

**Validation:**
- `test_finam_raises_without_finam_ticker`
- `test_finam_raises_on_invalid_ticker` (uppercase / spaces / cyrillic)
- `test_finam_normalizes_ticker_to_lower`

**Fetch flow с моком Playwright:**
- `test_fetch_filters_by_url_date_before_goto` — items older than `since` не вызывают page.goto
- `test_fetch_bulk_dedup_skips_known_urls`
- `test_fetch_classifies_errors`

**Acceptance T8.7:** ~12 новых тестов зелёные; coverage finam.py ≥ 85%; общий пул тестов ~80+ зелёных.

### T8.8 — E2E + backfill + pin (0.5-1 день)

1. `pip install -r requirements.txt && python -m playwright install chromium`
2. `python -m src init-db` — миграция item_type применяется (если БД была v1)
3. `python -m src fetch --company X5` — все три источника + finam:
   - finam: warmup → listing → date filter → bulk dedup → ~5-10 articles
4. `python -m src analyze --company X5` — каждый item получает `item_type` из LLM
5. `python -m src report --company X5`:
   - `output/X5/news/<YYYY>/<YYYY_MM>/*.md` — новости (x5_ir + rbc + finam.news)
   - `output/X5/recommendations/<YYYY>/<YYYY_MM>/*.md` — finam.recommendation
6. Глазами проверить:
   - В `output/X5/news_list/data.xlsx` колонка `item_type` присутствует
   - Несколько finam-items классифицированы корректно (визуально открыть `output/X5/recommendations/`, прочитать MD — действительно рекомендация?)
7. `ruff` + `mypy` чисто
8. **Pin playwright / playwright-stealth** версии в `requirements.txt`
9. README дополнен инструкцией про `playwright install chromium`

**Acceptance T8.8:**
- e2e без ошибок
- В БД ≥5 finam-items за период с 2026-05-01
- Минимум 1 item классифицирован как `recommendation` (из 70 на странице — высокая вероятность)
- В `output/X5/recommendations/` есть хотя бы один MD-файл
- ruff + mypy чисто, pin в requirements

---

## Что НЕ делаем (явно отложено)

- ❌ Pagination для finam — если 70 items недостаточно, отдельная мини-спека
- ❌ Другие секции finam (`/news/`, `/analytics/`, `/quote/moex/<ticker>/analytics/`) — отдельные задачи
- ❌ Recommendations-specific analysis (target price extraction, buy/sell aggregation) — отдельная задача
- ❌ Browser-crash recovery (P3.1 из спеки 03) — detect+abort, не recreate
- ❌ Persistent BrowserContext — P6=in-memory остаётся
- ❌ `playwright-stealth` updates / versioning policy — pin'нем по результатам T8.8

---

## Риски и митигации

| Риск | Митигация |
|---|---|
| Servicepipe адаптируется к stealth | Telemetry в `_verify_warmup_success` отлавливает; fallback на persistent context (отдельная мини-спека) |
| Servicepipe IP throttle (~15-60 мин после burst) | 4-часовая cadence далеко за threshold; warmup_ms > 10s = сигнал |
| URL date pattern меняется finam | T8.7 тест на listing fixture поймает регрессию |
| LLM возвращает гарбадж в `item_type` | Whitelisting + fallback to `'news'` + invalid value → status='error' (как для mood) |
| Миграция item_type ломает старые тесты | Default 'news' + idempotent migration; новые тесты на миграцию |
| Reporter ломается на пустой `recommendations/` папке | wipe-then-create idempotent; пустая папка не создаётся |
| Browser-crash посередине fetch | `_BrowserDead` → source aborts; fetcher logs error counter |
| Время цикла (Playwright медленный) | ~30-60s на cycle (как ожидаемо); date pre-filter + bulk dedup минимизируют per-article goto |
| Memory: Chromium 200MB | Acceptable; in-memory context закрывается после fetch |
| Конфликт ветки | ✅ DONE — ветка переименована из `e_disclosure_news` в `finam_news` (codex P1.7) |
| Broad-market noise в listing'е | ✅ Closed via slug-level filter (`_FINAM_RELEVANT_SLUG_PARTS`); ~80% мусора отсекается до per-article goto |
| Cross-source: x5_ir/rbc rows получат `item_type='news'` default | Документировано: no auto re-analysis в MVP. Если потом нужен historical recommendation split — one-off reset script (отдельная задача) |
| Slug filter может miss legitimate cross-company recommendation | Mitigation: «sistema» в allowlist (X5 ownership context); если другие edge cases — расширяем allowlist |
| LLM mis-classifies | Golden set 5 examples в T8.4; pass ≥4/5; invalid value → row status='error' (не silent misfile) |

---

## Acceptance (план в целом)

После T8.8:
1. `pytest tests/ -q` — ~80+ тестов зелёные (59 старых + ~20+ новых)
2. `ruff check src/ tests/` — clean
3. `mypy src/ --ignore-missing-imports` — no issues
4. `python -m src fetch --company X5` — четыре источника (x5_ir, rbc, finam — plus e_disclosure registered но disabled в config)
5. В БД ≥5 finam items за период с 2026-05-01
6. ≥1 item классифицирован как `recommendation`
7. В `output/X5/news/` MD-файлы; в `output/X5/recommendations/` MD-файлы; обе папки — самодостаточны (frontmatter + body)
8. `output/X5/news_list/data.xlsx` содержит колонку `item_type`
9. `FINAM_RECON.md` зафиксирован после T8.2
10. `playwright==<tested>` и `playwright-stealth==<tested>` в `requirements.txt`
11. README дополнен `python -m playwright install chromium` step

---

## Оценка времени (v2 — codex P3.5 honest update)

| Фаза | Оптимистично | 90-й перцентиль |
|---|---|---|
| T8.1 listing recon | ✅ DONE | |
| T8.2 article recon + RECON.md + URL/meta verify + backfill proof | 0.5-1 день | 1.5 дня |
| T8.3 PlaywrightSource ABC + 10 unit + 1 smoke + install sanity | 1.5-2 дня | 2.5 дня |
| T8.4 db migration + analyzer item_type + golden set eval | 1-1.5 дня | 2 дня |
| T8.5 reporter two-folder split + Excel header-order test | 0.5-1 день | 1.5 дня |
| T8.6 FinamSource impl + config + slug filter | 1-1.5 дня | 2 дня |
| T8.7 Тесты (~12-15) | 1 день | 1.5 дня |
| T8.8 E2E + backfill (3-5 мин) + pin + README | 0.5-1 день | 1.5 дня |
| **ИТОГО** | **6-8 дней** | **11-14 дней** |

Vs v1: +1.5-2 дня — это honest acknowledgement codex'а:
- Playwright infra **никогда не реализована** (нет real-friction data)
- Finam селекторы / article parsing — **unknown** до T8.2
- `item_type` вносит coupling prompt/eval/reporter/schema — больше edge cases

Первый backfill реально займёт **3-5 минут** (warmup 8s + listing 8s + ~70 items × goto + sleep + pause), не 30-60s как было в v1. Это acceptable для 4-часовой cadence.

---

## Что после T8.8

1. `/review` Claude → `reviews/04_claude_finam_rew.md`
2. `/codex review` → `reviews/04_codex_finam_rew.md`
3. `/cso` → `security/04_finam_sec.md`
4. `/health` — финальный composite dashboard (tests + lint + types + deps + smoke)
5. PR `finam_news → master` (ветку переименуем перед PR)
5. После merge:
   - Решаем по spec 03 (e-disclosure): дропать SUPERSEDED или вернуться через Playwright инфра + новый spec
   - Задача 05: следующий источник или recommendations-specific analysis
