# Plan 02 — Источник новостей РБК (v3)

Статус: READY
Дата: 2026-05-21
Версия: **v3** — после T6.1 recon полностью переработана архитектура (RSS-only)
Связанная спека: `specs/02_rbc_news_spec.md` (APPROVED)
Связанная оценка: `estimates/02_codex_rbc_news_est.md`
Recon-артефакт: `tests/fixtures/RBC_RECON.md` (DONE)
Ветка: `rbc_news`

---

## История версий

- **v1** — поиск по rbc.ru/search + двухэтапная фильтрация + anti-bot. Сломан codex'ом.
- **v2** — учтены 7 P1 от codex (Source interface, Qrator detection, persistent cursor, etc). 5-9 дней работы.
- **v3 (текущая)** — **T6.1 recon вскрыла что rbc.ru закрыт Qrator JS-challenge, но RSS-канал отдаёт `<full-text>` без защиты.** Архитектура переработана: RSS-only, без backfill. **Решение пользователя: Variant A.**

---

## Что v3 убирает из v2

- ❌ Поиск с date range
- ❌ Двухэтапная фильтрация (listing → article)
- ❌ Anti-bot слой (Qrator detection, cooldown, challenge HTML markers)
- ❌ Persistent cursor / truncation handling
- ❌ Warmup + cookie jar
- ❌ `_polite_sleep` 3-7 секунд (один HTTP-запрос на cycle вместо 30+)
- ❌ Playwright (deferred ушёл в архив — RSS обходит проблему)
- ❌ Per-article fetch с парсингом DOM статьи
- ❌ Manual follow_redirects + host allow-list (один endpoint, без редиректов)
- ❌ FetchContext.overlap_days (для RSS не имеет смысла)

## Что v3 оставляет из v2

- ✅ `FetchContext` для keyword loading (P1.1, P1.2, P2.2)
- ✅ Token-boundary keyword matching (P2.4)
- ✅ `CompanyCfg.aliases` в конец dataclass (P2.1)
- ✅ Structured summary logging (P3.4)
- ✅ T6.1 (recon) — как hard gate, ПРОЙДЕН

## Что v3 откладывает

- ⏸ `_resolve_since` с `MAX(published_at)` — для RSS не нужно, RBC берёт всегда последние 30. Можем вернуться когда появится источник с date-based query.
- ⏸ Backfill старых RBC-новостей — отдельная мини-спека позже, если понадобится (через Playwright или web.archive.org).

---

## Архитектурное место (v3)

```
src/sources/base.py        ← ИЗМЕНЕНИЕ: добавляем context: FetchContext | None в __init__
src/sources/x5_ir.py       ← без изменений (context принимается, игнорируется)
src/sources/rbc.py         ← НОВЫЙ, ~150 строк
src/fetcher.py             ← ИЗМЕНЕНИЕ: собирает FetchContext, передаёт в Source
                              dataclass FetchContext (или в src/context.py)
src/config.py              ← ИЗМЕНЕНИЕ: CompanyCfg.aliases в конец (default_factory=list)
config.yaml, .example      ← +rbc секция, +aliases для X5
tests/fixtures/RBC_RECON.md          ← УЖЕ СОЗДАН (T6.1)
tests/fixtures/rbc_rss_sample.xml    ← НОВЫЙ (real RSS dump для тестов)
tests/test_rbc_parser.py             ← НОВЫЙ (~8 тестов)
```

БД, analyzer, name_matcher, reporter — **без изменений** (подтверждено codex P3.1).

---

## Контракт Source ABC

`src/sources/base.py`:

```python
@dataclass(frozen=True)
class FetchContext:
    """Контекст fetch: компания + источник + БД-доступ для keyword-фильтра."""
    company_cfg: CompanyCfg
    company_id: int
    source_id: int
    db_path: Path

    def load_keywords(self) -> list[str]:
        """Алиасы компании + бренды + фамилии лиц из seed. Загружается один раз."""
        keywords = list(self.company_cfg.aliases or [self.company_cfg.name])
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            for row in conn.execute(
                "SELECT DISTINCT brand FROM persons WHERE company_id=? AND brand IS NOT NULL",
                (self.company_id,),
            ):
                keywords.append(row["brand"])
            for row in conn.execute(
                "SELECT full_name FROM persons WHERE company_id=?",
                (self.company_id,),
            ):
                # Берём только фамилию (последнее слово), как в name_matcher
                surname = row["full_name"].rsplit(maxsplit=1)[-1]
                keywords.append(surname)
        return keywords

class Source(ABC):
    def __init__(
        self,
        base_url: str,
        user_agent: str | None = None,
        context: FetchContext | None = None,   # НОВОЕ
    ) -> None: ...

    @abstractmethod
    def fetch(self, since: datetime) -> list[RawItem]: ...
```

- `x5_ir` игнорирует `context` (все 33 старых теста зелёные).
- `rbc` падает при `context is None`.

---

## Контракт fetcher

```python
def run_fetch(cfg: Config, company: CompanyCfg | None = None) -> list[FetchResult]:
    ...
    for company_cfg in companies:
        with sqlite3.connect(cfg.db_path) as conn:
            company_id = _get_company_id(conn, company_cfg.name)
            for source_code in company_cfg.sources:
                source_id = _get_source_id(conn, source_code)
                src_cfg = cfg.sources[source_code]
                source_cls = SOURCE_REGISTRY[src_cfg.parser]
                context = FetchContext(
                    company_cfg=company_cfg,
                    company_id=company_id,
                    source_id=source_id,
                    db_path=cfg.db_path,
                )
                with source_cls(base_url=src_cfg.base_url, context=context) as src:
                    since = _resolve_since(company_cfg, cfg.global_cfg)
                    raw_items = src.fetch(since)
                    _insert_items(conn, company_id, source_id, raw_items)
```

`_resolve_since` остаётся в текущем виде (config-only, без `MAX(published_at)` — этого оптимизатора нет в v3).

---

## RBCSource — поток данных

```
RBCSource.__init__(base_url, user_agent, context):
  if context is None: raise ValueError("RBCSource requires FetchContext")
  super().__init__(...)

RBCSource.__enter__():
  self._client = httpx.Client(timeout=30)
  return self

RBCSource.fetch(since: datetime) -> list[RawItem]:
  keywords = self.context.load_keywords()
  response = self._http_get(_RSS_URL)             # retry'и через tenacity
  items = list(_parse_rss(response.text))         # ~30 RawItem'ов
  results = []
  stats = {fetched: len(items), older_than_since: 0, keyword_rejects: 0, kept: 0}

  for item in items:
    if item.published_at < since:
      stats['older_than_since'] += 1
      continue
    if not _keyword_match(f"{item.headline}\n{item.body}", keywords):
      stats['keyword_rejects'] += 1
      continue
    results.append(item)
    stats['kept'] += 1

  logger.info("rbc fetch summary: %s", stats)
  return results
```

Никаких циклов, никаких per-article запросов, никакого anti-bot. Один HTTP-запрос, один XML-парсинг.

---

## Этапы

### T6.1 — Recon ✅ ПРОЙДЕНА

Артефакт: `tests/fixtures/RBC_RECON.md`. Вердикт: RSS-only, без backfill.

### T6.2 — Конфиг + Source ABC + fetcher (0.5 дня)

**Изменения в `src/config.py`:**
```python
@dataclass(frozen=True)
class CompanyCfg:
    name: str
    start_date: str | None
    sources: list[str]
    seed_persons: str
    aliases: list[str] = field(default_factory=list)   # ← в КОНЕЦ
```
В `load_config`: `aliases=c.get("aliases", [])`.

**Изменения в `src/sources/base.py`:**
- Импорт `FetchContext` (или dataclass прямо тут).
- `Source.__init__(base_url, user_agent=None, context=None)`.

**Изменения в `src/fetcher.py`:**
- `FetchContext` dataclass с `load_keywords()`.
- `run_fetch` собирает `FetchContext` per `(company × source)` и передаёт в конструктор.
- `SOURCE_REGISTRY["rbc"] = RBCSource`.

**Изменения в `config.yaml` + `.example.yaml`:**
```yaml
companies:
  - name: X5
    ...
    aliases: [X5, Х5, "X5 Retail Group", "X5 Group"]
    sources: [x5_ir, rbc]

sources:
  rbc:
    code: rbc
    name: РБК
    base_url: https://rssexport.rbc.ru/   # обратите внимание — RSS-host, не www.rbc.ru
    parser: rbc
    enabled: true
```

**Acceptance T6.2:**
- `pytest tests/ -q` — все 33 старых теста зелёные.
- `python -m src status` показывает `rbc` enabled для X5.

### T6.3 — Реализация `src/sources/rbc.py` (1 день)

```python
"""РБК news source. RSS-only, см. tests/fixtures/RBC_RECON.md."""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterator

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .base import FetchContext, RawItem, Source

logger = logging.getLogger(__name__)

_RSS_URL = "https://rssexport.rbc.ru/rbcnews/news/30/full.rss"
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
_RBC_NS = "{https://www.rbc.ru}"


class _TransientHTTPError(Exception):
    pass


class RBCSource(Source):
    def __init__(self, base_url: str, user_agent: str | None = None,
                 context: FetchContext | None = None) -> None:
        if context is None:
            raise ValueError("RBCSource requires FetchContext")
        super().__init__(base_url, user_agent or _USER_AGENT, context)

    def __enter__(self) -> "RBCSource":
        self._client = httpx.Client(timeout=30, headers={"User-Agent": self.user_agent})
        return self

    def __exit__(self, *_: object) -> None:
        self._client.close()

    def fetch(self, since: datetime) -> list[RawItem]:
        keywords = self.context.load_keywords()
        response = self._http_get(_RSS_URL)
        items = list(_parse_rss(response.text))
        results, stats = [], {"fetched": len(items), "older_than_since": 0,
                              "keyword_rejects": 0, "kept": 0}
        for item in items:
            if item.published_at < since:
                stats["older_than_since"] += 1
                continue
            if not _keyword_match(f"{item.headline}\n{item.body}", keywords):
                stats["keyword_rejects"] += 1
                continue
            results.append(item)
            stats["kept"] += 1
        logger.info("rbc fetch summary: %s", stats)
        return results

    @retry(
        retry=retry_if_exception_type(_TransientHTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    )
    def _http_get(self, url: str) -> httpx.Response:
        response = self._client.get(url)
        if response.status_code in {429, 500, 502, 503, 504}:
            raise _TransientHTTPError(f"transient {response.status_code}")
        response.raise_for_status()
        return response


# --- module-level pure functions (тестируемые без сети) ---

def _parse_rss(xml_text: str) -> Iterator[RawItem]:
    """Парсинг RBC RSS → RawItem. См. RBC_RECON.md секция 2."""
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        return
    for item_el in channel.findall("item"):
        link = (item_el.findtext("link") or "").strip()
        title = (item_el.findtext("title") or "").strip()
        full_text = (item_el.findtext(f"{_RBC_NS}full-text") or "").strip()
        pub_date = (item_el.findtext("pubDate") or "").strip()
        if not (link and title and pub_date):
            logger.debug("rbc: skipping malformed item")
            continue
        try:
            published_at = parsedate_to_datetime(pub_date).astimezone(timezone.utc)
        except (TypeError, ValueError):
            logger.warning("rbc: cannot parse pubDate %r", pub_date)
            continue
        yield RawItem(
            url=link,
            headline=title,
            body=full_text or (item_el.findtext("description") or "").strip(),
            published_at=published_at,
        )


def _keyword_match(text: str, keywords: list[str]) -> bool:
    """Token-boundary match. Латиница — \\b. Кириллица — non-word wrapper."""
    text_lower = text.lower()
    for kw in keywords:
        kw_lower = kw.lower()
        if all(c.isascii() and (c.isalnum() or c == "_") for c in kw):
            pattern = r"\b" + re.escape(kw_lower) + r"\b"
        else:
            pattern = r"(?:^|[^\wа-яё])" + re.escape(kw_lower) + r"(?:$|[^\wа-яё])"
        if re.search(pattern, text_lower):
            return True
    return False
```

Никакого SSRF allow-list (один endpoint, без редиректов). Никакого `_polite_sleep` (один запрос на цикл, в худшем случае 3 retries = 3 запроса с экспоненциальным backoff).

**Acceptance T6.3:**
- Тесты T6.4 зелёные.
- `python -m src fetch --company X5` отрабатывает: 30 fetched, N kept (зависит от того, есть ли X5-новости в текущие 7 часов RBC).

### T6.4 — Тесты (0.5-1 день, ~8 тестов)

`tests/test_rbc_parser.py`:

```python
# Парсинг RSS (используем сохранённый дамп tests/fixtures/rbc_rss_sample.xml)
test_parse_rss_extracts_30_items
test_parse_rss_correct_fields                  # url, headline, body, published_at
test_parse_rss_pubdate_to_utc                  # +0300 → UTC
test_parse_rss_skips_malformed_item            # item без <link> → пропуск, не raise
test_parse_rss_uses_full_text_not_description  # body = <rbc_news:full-text>

# Keyword matching (token boundaries, P2.4)
test_keyword_match_latin_word_boundary         # "X5" не хитит "OX5"
test_keyword_match_cyrillic_surname_boundary   # "Шехтерман" хитит "Шехтермана"/"Шехтерману"
test_keyword_match_case_insensitive
test_keyword_match_rejects_no_overlap

# fetch() с моком httpx
test_fetch_filters_by_since                    # item старше since → отброшен
test_fetch_filters_by_keywords                 # item без ключевых слов → отброшен
test_fetch_returns_kept_items                  # happy path
test_fetch_raises_without_context              # __init__ без context → ValueError
```

Фикстура `tests/fixtures/rbc_rss_sample.xml` — сохранённый сегодняшний RSS-дамп (230 КБ).

**Acceptance T6.4:** `pytest tests/ -q` — все тесты (33 старых + 12-13 новых) зелёные.

### T6.5 — E2E + visibility (0.5 дня)

- `python -m src cycle --company X5`:
  - x5_ir отрабатывает (как было).
  - rbc делает 1 HTTP-запрос, парсит 30 items, фильтрует.
  - analyze обрабатывает новые items через GPT-5 mini.
  - report пересоздаёт `output/X5/*`.
- В логах: `rbc fetch summary: {'fetched': 30, 'older_than_since': 0, 'keyword_rejects': 28, 'kept': 2}`.
- Если `kept > 0` — проверить глазами в `output/X5/news/.../*.md`.
- `ruff` + `mypy` на новых файлах — чисто.

**Acceptance T6.5:**
- e2e без ошибок.
- Если в RBC прямо сейчас нет новостей про X5 — это нормально (`kept == 0`). Подождать следующий цикл / проверить через 1-2 часа.
- В БД появилась запись в `sources` для `rbc` после T6.2 init-db.

---

## Что НЕ делаем (явно отложено)

- ❌ Backfill старых RBC-новостей через Playwright или web.archive.org — отдельная мини-спека если/когда понадобится.
- ❌ Поиск по RBC сайту (`/search/?query=...`) — закрыт Qrator'ом.
- ❌ Тематические RSS-каналы (business, economics) — их у RBC нет.
- ❌ Telegram-канал РБК — отдельный источник, отдельная спека.
- ❌ Платный API РБК.

---

## Риски и митигации

| Риск | Митигация |
|---|---|
| RSS endpoint меняет URL / формат | offline-тест на сохранённой фикстуре; e2e сразу заметит 404 |
| RBC переедет на Qrator-protected RSS | мониторим в e2e; fallback на отдельную мини-спеку (Playwright или RBC API) |
| 30 items в час не покрывают volume → пропуски | в day-mode cycle (раз/сутки) можем терять; решение — часовой cron |
| Keyword filter ложно положительный («X5» в коде самолёта) | token boundaries; ручная проверка первых 5-10 пойманных новостей |
| Keyword filter ложно отрицательный (статья про X5 в коде категории) | в v3 матчим `headline + full-text` (не только lead); меньше промахов |
| LLM-стоимость на 5+ статьях/час | summary-логи + `news.tokens_used` в DB |
| Дублирование при overlap cycle'ов | `UNIQUE(source_id, url)` ловит |

---

## Acceptance (план в целом)

После T6.5:
1. `pytest tests/ -q` — все тесты зелёные (33 + ~12).
2. `python -m src cycle --company X5` — оба источника отрабатывают.
3. В логах RBC появляется `fetch summary` с разумными числами.
4. Если RBC сегодня писал про X5 — новости в `output/X5/...` (если нет — ждём следующего цикла).
5. `ruff` + `mypy` на новых файлах — чисто.
6. `RBC_RECON.md` зафиксирован в репо.

---

## Оценка времени (v3, реалистичная)

| Фаза | Время |
|---|---|
| T6.1 Recon | ✅ ПРОЙДЕНА (30 минут вместо плановых 0.5-1 день) |
| T6.2 Конфиг + Source ABC + fetcher | 0.5 дня |
| T6.3 Impl RBCSource | 1 день |
| T6.4 Тесты (~12) | 0.5-1 день |
| T6.5 E2E + правки | 0.5 дня |
| **ИТОГО** | **2.5-3 дня** |

vs v2 (5-9 дней) — экономия 3-6 дней благодаря recon-открытию RSS feed.

---

## Что после T6.5

1. `/review` Claude → `reviews/02_claude_rbc_news_rew.md`.
2. `/codex review` → `reviews/02_codex_rbc_news_rew.md`.
3. `/cso` → `security/02_rbc_news_sec.md`.
4. PR `rbc_news → master`.
5. После merge — спека 03 (Interfax / Ведомости / Коммерсантъ, начиная с recon RSS-feed'ов у каждого).
