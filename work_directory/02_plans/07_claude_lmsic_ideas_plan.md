# Plan 07 — `lmsic_ideas` (имплементация LMSIC source) — **v2**

Автор: Claude
Дата: 2026-05-25 (v1), 2026-05-25 (v2 — после codex critique)
Ветка: `lmsic_ideas` (от master @ `77fe8f3`)
Спек: `01_specs/07_lmsic_ideas_spec.md` (APPROVED)
Recon: `tests/fixtures/LMSIC_RECON.md` (Phase 3 завершён)
Codex critique: `03_estimates/07_codex_lmsic_ideas_est.md` (5 P1 + 8 P2 ACCEPTED)

**v2 changes vs v1** (все codex findings приняты):
- T1: убрано упоминание `config.example.yaml` (его нет в репо); добавлен `FetchContext`-required guard в `__init__`
- T2: explicit redirect/status handling (3xx skip, 4xx terminal, 5xx tenacity); brands берутся из `FetchContext.load_keywords().strong` (не `CompanyCfg.brands`)
- T3: расширен regex и `_ACTION_MAP` под `не рекомендуем приобретать` / `не советуем «покупать»`; target regex с optional `в\s+`; убран бажный английский тест
- T4: `_build_body` НЕ prepend thesis (он уже первой строкой в body); `<br>` → `\n` ДО text() — preserve abzac
- T5: synthetic URL всегда содержит thesis-slug-prefix (3 первых слова), не «if needed»
- T6: pseudocode acceptance пересчитан (date filter ПЕРЕД issuer filter → `since=2026-05-20` даёт `filtered_date=10`, не `1`)
- T7: тесты на новые edge cases (ММК «не рекомендуем», Северсталь «не советуем», Россети «целевой цены в»)
- T8: live smoke с реальной SQL-проверкой, не tautology `kept ≥ 0`

---

## Контекст

Подключаем **lmsic.com/analytics/ideas/** как третий source — recommendation-only.
Архитектура `recommendations`-таблицы готова после task 06: lmsic просто объявляет
`item_destination = ItemDestination.RECOMMENDATIONS`, fetcher диспатчит сам, analyzer
обрабатывает через готовый `_analyze_recommendation` path. Без изменений в db.py,
analyzer.py, reporter.py, cloud_sync — только новый source + конфиг + тесты.

**Recon-выводы (детально в `tests/fixtures/LMSIC_RECON.md`):**
- SSR, нет anti-bot → httpx + selectolax (не Playwright)
- 10 items / страница; X5 на топе → MVP без AJAX-pagination
- Нет per-idea URL → синтетический `…/?#YYYYMMDD-<slug>`
- Структурные поля (`target_price`, `recommendation_action`, multipliers) — **в теле текстом**, парсятся regex'ом в source-модуле (не LLM)

---

## Архитектурное место

```
src/sources/lmsic.py          ← НОВЫЙ
src/fetcher.py                ← +1 строка SOURCE_REGISTRY
config.yaml                   ← +sources.lmsic, +companies[X5].sources += [lmsic]
config.example.yaml           ← same
tests/fixtures/lmsic_listing.html  ← уже снят в recon (126 КБ)
tests/test_lmsic.py           ← НОВЫЙ — 8-10 тестов
```

Без изменений: db.py, analyzer.py, name_matcher.py, reporter.py, cloud_sync/.

---

## Поток данных

```
fetch_all → SOURCE_REGISTRY['lmsic'] → LmsicSource(ctx)
  ┌─ __enter__: httpx.Client(timeout=20, follow_redirects=False)
  │
  ├─ fetch(since):
  │     ├─ _fetch_listing()           — GET /analytics/ideas/, parse 10 items
  │     ├─ filter by .ideas-card__date >= since
  │     ├─ for each item:
  │     │     ├─ _parse_card(node)    → IdeaRaw{date, issuer, body_html}
  │     │     ├─ _match_company(issuer, aliases+brands)  → keep/skip
  │     │     ├─ _extract_fields(body) → {target_price, action, potential, multipliers}
  │     │     ├─ _clean_text(body)    → плоский текст (newline, без HTML)
  │     │     ├─ _build_body(thesis, fields, body) → preformatted (P9.B)
  │     │     ├─ _build_url(date, issuer) → синтетический #YYYYMMDD-<slug>
  │     │     └─ yield RawItem(...)
  │     └─ stats log: {fetched, date_filtered, issuer_filtered, kept}
  │
  └─ __exit__: close httpx.Client

fetcher._insert(destination=RECOMMENDATIONS) → INSERT INTO recommendations
  (UNIQUE(source_id, url) дедуп через синтетический URL)

analyzer._analyze_recommendation(rec) → LLM по уже готовому body с header'ом
  → UPDATE recommendations SET mood=..., mood_reason=..., status='analyzed'
  → INSERT INTO recommendation_persons (если в body упомянуты persons)

reporter._write_recommendations() — уже работает по UNION'у news+recommendations.
Lmsic-recs появятся в `output/X5/recommendations/YYYY_MM/dd_<slug>.md`.
```

---

## T-фазы

### T1 — Скаффолд `LmsicSource` + конфиг (~30 мин)

**Изменения:**
- `src/sources/lmsic.py` — class с `code="lmsic"`, `item_destination=ItemDestination.RECOMMENDATIONS`, `fetch()` возвращает `[]` (placeholder).
- В `__init__`: **explicit guard** `if context is None: raise ValueError("LmsicSource requires FetchContext (need company aliases for issuer matching)")` — наследуем паттерн finam (codex P2.8).
- `src/fetcher.py` — `SOURCE_REGISTRY["lmsic"] = LmsicSource`.
- `config.yaml` — добавить `sources.lmsic` блок (parser=`lmsic`, enabled=true, base_url=`https://www.lmsic.com/`); `companies[X5].sources` += `lmsic`. **`config.example.yaml` не трогаем — его нет в репо** (codex P1.4).

**Acceptance:**
- `pytest tests/ -q` — 178/178 проходят (ничего не сломали).
- `python -m src init-db` — `sources` table содержит row `code='lmsic'`.
- `python -m src fetch --company X5` — отрабатывает без ошибок, lmsic fetched=0.
- `LmsicSource(base_url="x", context=None)` → `ValueError`.

---

### T2 — Парсер листинга + HTTP layer (~1.5 ч)

**Изменения:**
- `LmsicSource._fetch_listing()` — GET `/analytics/ideas/` через `httpx.Client`.
  - User-Agent: default из base.py.
  - `follow_redirects=False`, `timeout=20`.
  - **Explicit status handling** (codex P2.5, наследуем x5_ir._http_get pattern):
    - 200 → ok
    - 3xx → log warning + raise `RuntimeError("unexpected redirect")` (НЕ skip — listing page не должна редиректить, это аномалия)
    - 4xx → terminal raise (НЕ retry — broken endpoint)
    - 5xx → tenacity retry (`stop_after_attempt(3)`, `wait_exponential`)
    - `httpx.ConnectError / ReadTimeout / WriteTimeout / PoolTimeout / ConnectTimeout / RemoteProtocolError` → tenacity retry
- `LmsicSource._parse_card(node)` — для одного `<li class="ideas-page__list-item">`:
  - selectolax: `.ideas-card__date` → text `DD.MM.YYYY`
  - `.ideas-card__title` → text issuer
  - `.ideas-card__preview-text` → `node.html` (raw innerHTML, нужен для `<br>` preservation в T4)
- `LmsicSource._match_company(issuer)` — case-insensitive substring против `FetchContext.load_keywords().strong` (содержит aliases + brands из `persons.brand`; **НЕ из `CompanyCfg.brands` — такого поля нет**, codex P1.3). Возвращает bool. **Known limitation MVP**: матчинг только по title; если lmsic переименует X5-issuer на «КЦ ИКС 5» а title-aliases не покроют — пропустим. Future task — body-fallback (см. spec P2 альт.B).
- `LmsicSource._published_at(date_str)` — `DD.MM.YYYY` → UTC datetime через `time(23, 59)` Europe/Moscow (recon §12).

**Acceptance:**
- Fixture-тест: парсит `tests/fixtures/lmsic_listing.html` → 10 items.
- Из 10 items ровно 1 матчит X5 — issuer строка `"X5 Retail group"` содержит substring `"X5"` (из aliases в `config.yaml`).
- Все даты parsed, все UTC datetime correct.
- 4xx тест: моковый `respx` ответ 404 → `httpx.HTTPStatusError`, без retry.
- 5xx тест: моковый respx ответ 503 → 3 попытки tenacity, потом raise.
- httpx_client closed после `__exit__`.

---

### T3 — Extract структурных полей `_extract_fields` (~2 ч, +30 мин из v1)

**Codex P1.1+P1.2 ACCEPTED — regex переписаны под реальный fixture.**

В fixture'е встречаются как минимум **четыре формы** рекомендации:

1. "рекомендацию «держать»" — X5 (positive cite)
2. "подтверждаем нашу рекомендацию «<X>»" — generic positive citation
3. "Мы не рекомендуем приобретать" — ММК (negation без кавычек, без слова «рекомендация»)
4. "не советуем «покупать»" — Северсталь (negation с кавычками)

И **две формы** target:
- "целевой цены 2800 руб" — X5
- "целевой цены в 400 руб" — Россети (с `в`)

**Изменения:**

```python
import re, json

# Recommendation action: ORDER MATTERS — negation проверяется ПЕРВОЙ,
# иначе позитивный паттерн заматчит «покупать» внутри «не рекомендуем покупать».
_NEGATION_PATTERNS = [
    # "Мы не рекомендуем приобретать/покупать" / "не рекомендуется покупать"
    re.compile(r'не\s+рекомендуе[мт]?с?я?\s+(?:приобрет|покуп)', re.I),
    # "не советуем «покупать»" / "не советуем приобретать"
    re.compile(r'не\s+советуе[мт]\s+[«"]?(?:покуп|приобрет)', re.I),
]
# Positive citation: "рекомендацию «<X>»" — X ∈ {покупать, держать, продавать}
_POSITIVE_RE = re.compile(r'рекомендац\w+\s+[«"]([^»"
]+)[»"]', re.I)

_ACTION_MAP = {
    "покупать": "buy", "купить": "buy", "приобретать": "buy",
    "держать": "hold",
    "продавать": "sell", "продать": "sell",
}

def _extract_action(body: str) -> str | None:
    for neg in _NEGATION_PATTERNS:
        if neg.search(body):
            return "sell"
    m = _POSITIVE_RE.search(body)
    if m:
        return _ACTION_MAP.get(m.group(1).strip().lower())
    return None

# Target price: "целевой цены [в ]2800 руб" — codex P1.2: optional `в\s+`
_TARGET_RE = re.compile(
    r'целев\w+\s+цен\w+\s+(?:в\s+)?([\d\s.,]+?)\s*руб',
    re.I,
)

_POTENTIAL_RE = re.compile(r'(\d+(?:[.,]\d+)?)\s*%\s*потенциал', re.I)

_MULTIPLIERS = [
    (re.compile(r'EV\s*/\s*EBITDA\s*=\s*(-?\d+(?:[.,]\d+)?)', re.I), 'ev_ebitda'),
    (re.compile(r'P\s*/\s*E\s*=\s*(-?\d+(?:[.,]\d+)?)', re.I),       'p_e'),
    (re.compile(r'Net\s+debt\s*/\s*EBITDA\s*=\s*(-?\d+(?:[.,]\d+)?)', re.I), 'nd_ebitda'),
]

def _parse_float(s: str) -> float | None:
    # strip NBSP (U+00A0), narrow-NBSP (U+202F), figure-space (U+2007), regular space
    for ch in (" ", " ", " ", " "):
        s = s.replace(ch, "")
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None

def _extract_fields(body: str) -> dict:
    fields = {
        "target_price": None,
        "recommendation_action": _extract_action(body),
        "potential_pct": None,
        "multipliers_json": None,
    }
    if (m := _TARGET_RE.search(body)):
        fields["target_price"] = _parse_float(m.group(1))
    if (m := _POTENTIAL_RE.search(body)):
        fields["potential_pct"] = _parse_float(m.group(1))
    found = {}
    for rx, key in _MULTIPLIERS:
        if (m := rx.search(body)):
            found[key] = _parse_float(m.group(1))
    if found:
        fields["multipliers_json"] = json.dumps(found, ensure_ascii=False)
    return fields
```

**Acceptance (codex P1.1+P1.2+P2.2):**
- X5 (19.05.2026): `target=2800, action='hold', potential=14`, multipliers все три.
- **ММК (05.05.2026): `action='sell'` через NEGATION «не рекомендуем приобретать»** (был bug в v1: вернул бы `None`). `target=None, potential=None`, multipliers partial.
- **Северсталь (04.05.2026): `action='sell'` через NEGATION «не советуем «покупать»»** (был bug в v1). `target=None`, multipliers полные.
- **Россети (14.04.2026): `target=400.0` через regex с `в`** (был bug в v1: вернул бы `None`).
- Сегежа (20.04.2026): all None — нет рекомендационной фразы, нет target, нет multipliers.
- ФосАгро (07.05.2026): `target=8000, action='hold', potential=19`, multipliers полные.
- `_parse_float('2 800,5')` → 2800.5 (regular space, NBSP, narrow-NBSP, figure-space, comma).
- **Удалён bogus тест** `_map_action('BUY')` (codex P2.2 — карта только русская).
- Ordering test: "Мы не рекомендуем покупать ... подтверждаем рекомендацию «держать»" (искусственный) → `'sell'` (negation побеждает).

---

### T4 — Body cleaning + preformatted header `_build_body` (~1 ч)

**Codex P2.1 ACCEPTED — НЕ prepend thesis (body уже им начинается).**
**Codex P2.6 ACCEPTED — `<br>` → `
` ДО `selectolax.text()`, иначе abzac flattened.**
**Codex P2.7 ACCEPTED — `_clean_text` живёт ЛОКАЛЬНО в `src/sources/lmsic.py`** (зеркалирует паттерн x5_ir/finam — каждый source-модуль имеет свой локальный helper, см. CLAUDE.md «Body cleaning convention»).

**Изменения:**

```python
import re
import html as html_module
import unicodedata
from selectolax.parser import HTMLParser

_NBSP_CHARS = "   "
_TELEGRAM_FOOTER_RE = re.compile(r'
+Тел[еэ]гра[мн]+\-?канал\s*:[^
]*$', re.I)
_BR_RE = re.compile(r'<br\s*/?>', re.I)
_MULTI_NEWLINE_RE = re.compile(r'
{3,}')

def _clean_text(s: str) -> str:
    """Local source-helper (см. CLAUDE.md). HTML-unescape, NBSP, control, multi-newline."""
    s = html_module.unescape(s)
    for ch in _NBSP_CHARS:
        s = s.replace(ch, " ")
    # Drop control characters except 
 and 	
    s = "".join(c for c in s if c == "
" or c == "	" or unicodedata.category(c)[0] != "C")
    # Trim trailing spaces per line, collapse 3+ newlines to 2
    s = "
".join(line.rstrip() for line in s.split("
"))
    s = _MULTI_NEWLINE_RE.sub("

", s)
    return s.strip()

def _clean_body(self, html_fragment: str) -> str:
    # Step 1: <br> → 
 BEFORE selectolax (P2.6 — preserve abzac)
    with_newlines = _BR_RE.sub("
", html_fragment)
    # Step 2: parse and extract text
    text = HTMLParser(with_newlines).text(strip=False)
    # Step 3: strip Telegram footer
    text = _TELEGRAM_FOOTER_RE.sub("", text)
    # Step 4: normalize whitespace
    return _clean_text(text)

def _extract_thesis(body_text: str) -> str:
    """First non-empty paragraph (before first blank line)."""
    for para in body_text.split("

"):
        para = para.strip()
        if para:
            return para
    return ""

def _build_headline(self, issuer: str, thesis: str) -> str:
    if thesis and issuer.lower() not in thesis.lower():
        return f"{issuer}: {thesis}"
    return thesis or issuer

def _build_body(self, fields: dict, body_text: str) -> str:
    """P2.1: body_text already starts with thesis — do NOT prepend it again."""
    parts: list[str] = []
    if fields["recommendation_action"]:
        parts.append(f"Рекомендация: {fields['recommendation_action']}")
    if fields["target_price"] is not None:
        line = f"Целевая цена: {fields['target_price']:g} ₽"
        if fields["potential_pct"] is not None:
            line += f" ({fields['potential_pct']:+g}%)"
        parts.append(line)
    if fields["multipliers_json"]:
        m = json.loads(fields["multipliers_json"])
        bits = []
        if "ev_ebitda" in m: bits.append(f"EV/EBITDA={m['ev_ebitda']:g}")
        if "p_e" in m:       bits.append(f"P/E={m['p_e']:g}")
        if "nd_ebitda" in m: bits.append(f"Net debt/EBITDA={m['nd_ebitda']:g}")
        if bits:
            parts.append("Мультипликаторы: " + " · ".join(bits))
    header = "
".join(parts)
    return f"{header}

{body_text}" if header else body_text
```

**Acceptance:**
- Юнит на X5-fixture: header содержит `Рекомендация: hold`, `Целевая цена: 2800 ₽ (+14%)`, `Мультипликаторы: EV/EBITDA=3.31 · P/E=7.43 · Net debt/EBITDA=1.12`. Затем blank line, затем thesis-строка (НЕ дублируется). Telegram-footer отсутствует.
- Юнит на ММК: header — только `Рекомендация: sell` + multipliers без P/E.
- Юнит на Сегеже: header пустой, body начинается сразу с thesis.
- **Юнит на abzac preservation (P2.6):** в body минимум 4 `

`-разделителя (как в fixture'е).
- Юнит на footer-strip: regex покрывает «Телеграмм-канал», «Телеграм-канал», «Телеграмм канал» (опечатки).

---

### T5 — Синтетический URL `_build_url` (~45 мин, +15 мин из v1)

**Codex P2.4 ACCEPTED — thesis-slug-prefix добавлен с самого старта**, не «if needed».

**Изменения:**

```python
import re

_CYR_TO_LAT = {
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z",
    "и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r",
    "с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"ts","ч":"ch","ш":"sh","щ":"sch",
    "ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
}
_SLUG_KEEP_RE = re.compile(r"[^a-z0-9\-]")
_SLUG_DASHES_RE = re.compile(r"-+")

def _slugify(s: str) -> str:
    s = s.lower()
    s = "".join(_CYR_TO_LAT.get(ch, ch) for ch in s)
    s = s.replace(" ", "-")
    s = _SLUG_KEEP_RE.sub("", s)
    s = _SLUG_DASHES_RE.sub("-", s).strip("-")
    return s

def _build_url(self, published_at: datetime, issuer: str, thesis: str) -> str:
    issuer_slug = _slugify(issuer)
    # Thesis-slug — first 3 words after slugification (P2.4 — disambiguator from start)
    thesis_words = _slugify(thesis).split("-")[:3]
    thesis_slug = "-".join(w for w in thesis_words if w)
    date_part = published_at.strftime("%Y%m%d")
    fragment = f"{date_part}-{issuer_slug}"
    if thesis_slug:
        fragment += f"-{thesis_slug}"
    return f"https://www.lmsic.com/analytics/ideas/#{fragment}"
```

**Acceptance:**
- X5 (19.05.2026, thesis="КЦ Икс 5: падение трафика во всех форматах сети"):
  → `…/#20260519-x5-retail-group-kts-iks-5` (первые 3 значащих слова после slugify)
- ММК (05.05.2026): `…/#20260519-mmk-rezultaty-stanovyatsya-vse` (или подобное — 3 слова)
- При повторном fetch на следующий день — `INSERT OR IGNORE` отрабатывает (тот же URL).
- Edge: два разных issuer'а в один день → разные URL'ы.
- Edge: один issuer, тот же день, разный thesis (искусственный тест) → разные URL'ы.
- `_slugify("X5 Retail group")` → `"x5-retail-group"`.
- `_slugify("Эл5-Энерго")` → `"el5-energo"`.
- `_slugify("Россети Ленэнерго")` → `"rosseti-lenenergo"`.

**Risk note:** v1 имел weak dedup на `(date, issuer)` без thesis-slug — теоретически коллизия при двух X5-идеях в один день. v2 (P2.4) добавляет thesis-prefix → коллизия только если lmsic пересохранит идею с тем же ticker, тем же днём И теми же 3 первыми словами thesis'а. На practice — невозможно.

### T6 — Integration: `LmsicSource.fetch()` собирает всё (~1 ч)

**Codex P1.5 ACCEPTED — pseudocode acceptance ПЕРЕСЧИТАН.** Date-filter применяется ПЕРВЫМ, потому что date — структурное поле в листинге и фильтр дешевле issuer-match'а. Это значит: если `since > newest_idea_date_on_page` — все 10 items уходят в `filtered_date`, ни одного в `filtered_issuer`.

**Изменения:**

```python
def fetch(self, since: datetime) -> Iterable[RawItem]:
    html = self._fetch_listing()
    tree = HTMLParser(html)
    kept = filtered_date = filtered_issuer = 0
    for node in tree.css("li.ideas-page__list-item"):
        date_str = node.css_first("span.ideas-card__date").text(strip=True)
        published_at = self._published_at(date_str)
        # FILTER 1: date (cheap, applied first)
        if published_at < since:
            filtered_date += 1
            continue
        issuer = node.css_first("div.ideas-card__title").text(strip=True)
        # FILTER 2: issuer (substring against aliases+brands)
        if not self._match_company(issuer):
            filtered_issuer += 1
            continue
        body_html = node.css_first("div.ideas-card__preview-text").html or ""
        body_text = self._clean_body(body_html)
        fields = self._extract_fields(body_text)
        thesis = self._extract_thesis(body_text)
        full_body = self._build_body(fields, body_text)  # P2.1: no thesis arg
        headline = self._build_headline(issuer, thesis)
        url = self._build_url(published_at, issuer, thesis)  # P2.4: thesis arg
        kept += 1
        yield RawItem(
            url=url,
            headline=clean_text(headline),
            body=clean_text(full_body),
            published_at=published_at,
            target_price=fields["target_price"],
            recommendation_action=fields["recommendation_action"],
            potential_pct=fields["potential_pct"],
            multipliers_json=fields["multipliers_json"],
        )
    log.info("lmsic fetch: kept=%d filtered_date=%d filtered_issuer=%d",
             kept, filtered_date, filtered_issuer)
```

**Acceptance (codex P1.5 — corrected math):**

| Сценарий | since | filtered_date | filtered_issuer | kept |
|---|---|---|---|---|
| Backfill всё (default) | 2026-05-01 | 0 | 9 | 1 (X5) |
| Cutoff после самой свежей | 2026-05-20 | **10** | **0** | 0 |
| Cutoff между X5 и ФосАгро | 2026-05-15 | 9 | 0 | 1 (X5 from 19.05) |
| Cutoff до всех | 2025-01-01 | 0 | 9 | 1 (X5) |

(Старая v1-математика `since=2026-05-20 → filtered_date=1, filtered_issuer=9` была физически невозможной.)

- Returned `RawItem` (since=2026-05-01): url содержит `#20260519-x5-retail-group-`, structural fields заполнены, body начинается с header'а.
- Integration test (мок только httpx.Client.get) — full pipeline до RawItem'а.

### T7 — Тесты `tests/test_lmsic.py` финализация (~1 ч)

**Структура файла:**

```python
# tests/test_lmsic.py
import json
import pytest
from datetime import datetime, timezone, date
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.sources.base import ItemDestination, FetchContext
from src.sources.lmsic import LmsicSource

FIXTURE = Path(__file__).parent / "fixtures" / "lmsic_listing.html"

@pytest.fixture
def html_fixture():
    return FIXTURE.read_text(encoding="utf-8")

@pytest.fixture
def x5_ctx(tmp_path):
    """FetchContext для X5 с aliases."""
    ...

# T1 tests:
def test_init_without_context_raises(): ...           # P2.8

# T2 tests:
def test_parse_listing_10_items(html_fixture): ...
def test_published_at_moscow_to_utc(): ...
def test_match_company_x5_aliases(x5_ctx): ...
def test_fetch_listing_3xx_raises(): ...              # P2.5
def test_fetch_listing_4xx_no_retry(): ...            # P2.5
def test_fetch_listing_5xx_retries(): ...             # P2.5

# T3 tests:
def test_extract_fields_x5(): ...                     # happy path
def test_extract_fields_mmk_negation_sell(): ...      # P1.1 — "не рекомендуем приобретать"
def test_extract_fields_severstal_negation_sell(): ...# P1.1 — "не советуем «покупать»"
def test_extract_fields_rosseti_target_v(): ...       # P1.2 — "целевой цены в 400 руб"
def test_extract_fields_segezha_all_none(): ...
def test_extract_fields_fosagro(): ...
def test_extract_action_negation_wins_over_positive(): ...  # P1.1 ordering
def test_map_action_only_russian(): ...               # P2.2 — нет BUY/SELL английского
def test_parse_float_unicode_spaces(): ...

# T4 tests:
def test_build_body_no_thesis_duplication(): ...      # P2.1
def test_build_body_telegram_footer_stripped(): ...
def test_clean_body_preserves_paragraphs(): ...       # P2.6 — abzac
def test_clean_text_normalizes_nbsp_and_control(): ...

# T5 tests:
def test_build_url_includes_thesis_slug(): ...        # P2.4
def test_build_url_dedup_same_idea_twice(): ...
def test_build_url_disambiguates_same_day_same_issuer(): ...  # P2.4
def test_slugify_cyrillic_to_latin(): ...
def test_slugify_handles_el5_energo(): ...

# T6 tests:
def test_fetch_filters_by_date_first(): ...           # P1.5 — date before issuer
def test_fetch_since_after_newest_all_filtered_by_date(): ...  # P1.5
def test_fetch_yields_correct_rawitem(html_fixture, x5_ctx): ...

# Sanity:
def test_item_destination_is_recommendations(): ...
def test_source_closes_httpx_client(): ...
```

**Acceptance:** ~25 новых тестов, все green. Total: 178 + ~25 = ~203.

---

### T8 — Health stack + live smoke (~45 мин, +15 мин из v1)

**Codex P2.9 ACCEPTED — live smoke не tautology.**

**Acceptance:**
- `pytest tests/ -q` — все green (~193 tests).
- `python -m ruff check src/ tests/` — clean.
- `python -m mypy src/ --ignore-missing-imports` — clean.
- Coverage `src/sources/lmsic.py` ≥ 90%.
- **Live smoke** (требует интернет): `python -m src cycle --company X5` →
  - **Должно завершиться с exit 0** (это уже сильнее `kept ≥ 0`).
  - **В логах должна быть строка** `lmsic fetch: kept=N filtered_date=M filtered_issuer=K` с реальными числами — не пустыми.
  - **SQL check:**
    ```sql
    SELECT COUNT(*) FROM recommendations
    WHERE source_id = (SELECT id FROM sources WHERE code='lmsic')
      AND status IN ('analyzed', 'new');
    ```
    - Если на странице есть X5-идея → COUNT ≥ 1.
    - Если на странице X5-идеи нет (rare race — снепшот может стать старее за timing'ом) → COUNT = 0, **но в логах должна быть либо `kept=0` либо `filtered_date>0`**. Если оба = 0 при HTTP 200 — это парсер сломан, провал.
- `python -m src status --company X5` — выводит строку `lmsic recommendation new: N` или `analyzed: N`.

---

## Риски

| Риск | Митигация |
|---|---|
| Сайт меняет HTML структуру | Snapshot fixture в `tests/fixtures/lmsic_listing.html` — парсер прибит к ней. Изменение DOM ловится тестом, не production'ом. |
| Сайт начнёт ставить anti-bot | Recon показал что нет; если появится — добавим Playwright (как у finam). Не в скоупе MVP. |
| Regex extraction не покрыл новую формулировку («наш target = 2800») | Acceptable graceful degradation: `target_price=None`. Analyzer всё равно даст mood/mood_reason на основе body. Лучше None чем неверное значение. |
| `_slugify` не обработает экзотический ticker | Edge тест на не-X5 issuer'ах ловит. Fallback — оставить cyrillic в URL (РФ Postgres + Obsidian принимают). |
| Lmsic пересохраняет идею в тот же день | `UNIQUE(source_id, url)` дедуп — INSERT OR IGNORE, новый контент не подтянется. Принимаем для MVP. |
| `published_at = 23:59 Moscow` пересечёт UTC midnight | Тест на 31.12.YYYY → published_at в UTC = 21:59 того же дня, reporter раскладывает в правильный `YYYY_MM/dd` по `config.global.timezone=Europe/Moscow` — invariant из CLAUDE.md соблюдается. |

---

## Безопасность (наследуется из task 01-06)

- `_clean_text` обязательно на body и headline — есть.
- selectolax + whitelist по селекторам, не regex по HTML — есть.
- httpx `follow_redirects=False` — есть. (lmsic не редиректит, но защищаем SSRF-via-302.)
- SYSTEM_PROMPT_RECOMMENDATION уже имеет prompt-injection guard (из task 06).
- Все INSERT'ы через `?` placeholders (через fetcher dispatcher из task 06).
- `error_msg` — только класс ошибки.
- В body может прилететь Telegram URL `https://t.me/lmsstock` — это публичная ссылка автора, не secret/PII. Оставляем footer-strip как косметику (чище в Obsidian), а не как security-меру.

---

## Out of scope (отдельные задачи если понадобятся)

- AJAX-pagination (`PAGEN_1`) — backfill режим
- Year-filter backfill (`?year=YYYY`) — отдельная CLI команда `--backfill`
- Persons-extraction из lmsic body (наследуется через name_matcher — отдельной логики не надо)
- Multi-company (Магнит, Лента — просто добавить aliases в config.yaml после ship'а)
- Telegram-channel автора как отдельный source (out of scope)
- δ-completion (см. TODOS.md из task 06)

---

## Оценка времени (v2 — после codex critique)

| T-фаза | v1 | v2 | Изменение |
|---|---|---|---|
| T1 Scaffold + config | 30 мин | 30 мин | — |
| T2 Parse listing + HTTP | 1.5 ч | 1.5 ч | (status handling explicit, но реализация та же) |
| T3 Extract fields | 1.5 ч | **2 ч** | +30 мин: расширенные regex, negation-pattern, доп тесты |
| T4 Body + header | 1 ч | 1 ч | (P2.1+P2.6+P2.7 — минимальное изменение реализации) |
| T5 Synthetic URL | 30 мин | **45 мин** | +15 мин: thesis-slug-prefix с самого начала |
| T6 fetch() integration | 1 ч | 1 ч | (только pseudocode acceptance переcчитан) |
| T7 Tests finalize | 1 ч | **1.5 ч** | +30 мин: 25 тестов вместо 15 (P1.1+P1.2+P2.5+...) |
| T8 Health + smoke | 30 мин | **45 мин** | +15 мин: усиленный live smoke с SQL-check |
| **Итого** | **~7.5 ч** | **~9 ч** | +1.5 ч |

Плюс pre-ship гейты (review / codex review / cso / health) — ~1 ч.

---

## Acceptance целиком (v2)

- ✅ Новый source `lmsic` подключен, `item_destination=ItemDestination.RECOMMENDATIONS`
- ✅ `LmsicSource.__init__` raises `ValueError` без `FetchContext` (P2.8)
- ✅ X5-идеи из lmsic попадают в `recommendations` table с заполненными `target_price`, `recommendation_action`, `potential_pct`, `multipliers_json`
- ✅ ММК (negation «не рекомендуем») → `action='sell'` (P1.1)
- ✅ Северсталь (negation «не советуем») → `action='sell'` (P1.1)
- ✅ Россети («целевой цены в 400 руб») → `target=400` (P1.2)
- ✅ Дедуп через синтетический URL с thesis-slug-prefix (P2.4)
- ✅ Body содержит preformatted header без дубля thesis (P2.1)
- ✅ Abzac-разделители сохраняются после selectolax (P2.6)
- ✅ Telegram-footer удалён
- ✅ HTTP 3xx/4xx/5xx обработаны по тиерам (P2.5)
- ✅ Reporter рендерит idea в `output/X5/recommendations/YYYY_MM/dd_<slug>.md` (через готовую UNION-логику task 06)
- ✅ Cloud sync пушит в `trading_news.recommendations` (через готовый pusher task 06)
- ✅ ~25 новых тестов, все green; ruff + mypy clean
- ✅ Live smoke: cycle exit 0 + лог содержит конкретные kept/filtered числа + SQL count = 1 при наличии X5-идеи на странице

---

## После ship'а

- Запись в TODOS.md если что-то отложено
- Удалить fixture `lmsic_listing.html` если будет дрейфить от продакшна (или зафиксировать дату snapshot'а в комментарии файла)
- Следующая задача 08+ — по обсуждению с пользователем (другие source'ы / другие companies / δ-completion / extract-step)

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run |
| Codex Review | `/codex consult` | Independent 2nd opinion | 1 | DONE | 13 findings (5 P1, 8 P2), все ACCEPTED → plan v2 |
| Eng Review | `/plan-eng-review` | Architecture & tests | 0 | — | (внутренний self-review интегрирован в plan v2) |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | n/a (CLI tool) |
| DX Review | `/plan-devex-review` | DX gaps | 0 | — | n/a |

- **CODEX:** 13 findings (5 P1 + 8 P2) приняты, plan v1 → plan v2; см. `03_estimates/07_codex_lmsic_ideas_est.md`
- **UNRESOLVED:** 0
- **VERDICT:** plan v2 готов к Phase 4 (T1..T8 имплементация)
