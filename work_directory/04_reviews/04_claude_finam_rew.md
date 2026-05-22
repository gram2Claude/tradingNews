# 04 · finam · pre-ship review (Claude)

**Ветка:** `finam_news`
**Дата:** 2026-05-22
**Diff vs master:** 12 файлов изменено / 14 новых · +413 −37 в `src/` · 16 новых тестов (всего 102 passing)
**Scope check:** CLEAN — добавлен источник `finam`, инфраструктура `PlaywrightSource`, классификация `item_type` (news / recommendation), двухпапочный split в reporter, v1→v2 миграция. Изменения соответствуют `plans/04_claude_finam_plan.md` и `specs/04_finam_spec.md`. Никаких неожиданных переделок чужих модулей.

---

## Сводка

| Severity | Кол-во | Действие |
| --- | --- | --- |
| P1 (блокер) | 0 | — |
| P2 (warning) | 3 | можно фиксить сейчас или принять как known |
| P3 (informational) | 4 | принять и оставить, если P3-фиксы не дешёвые |

Pre-ship gate **PASS** со стороны Claude review. Блокирующих находок нет. Ниже P2/P3 находки и обоснования.

---

## P2 — Warning

### P2.1 · LLM может вернуть mood без item_type, и тогда новость молча станет `news` (confidence 8/10)

**Где:** `src/analyzer.py:273-277`

```python
item_type = parsed.get("item_type", "news")
if item_type not in VALID_ITEM_TYPES:
    raise ValueError(f"invalid item_type: {item_type!r}")
```

**Проблема.** SYSTEM_PROMPT просит вернуть 3 поля, но мы тестируем только `mood` строго (`KeyError`-style: `parsed["mood"]`). А для `item_type` — `dict.get(..., "news")`. То есть если GPT-5 mini забудет поле (а с long-tail промптами это бывает), мы **молча** запишем `item_type='news'` для recommendation и пользователь увидит её в `output/X5/news/`, а не в `recommendations/`. Это не падение — это silent reclassification.

Backwards-compat был аргументом для миграции старых v1 строк. Но новые LLM-вызовы в v2 уже всегда должны возвращать item_type — fallback здесь работает не в той ситуации, в которой задумывался.

**Fix (5 строк):** Сделать поле обязательным как `mood`:

```python
mood = parsed["mood"]
...
item_type = parsed["item_type"]      # ← KeyError → parse error → status=error
if item_type not in VALID_ITEM_TYPES:
    raise ValueError(f"invalid item_type: {item_type!r}")
```

Старые v1-строки уже проанализированы — у них `item_type='news'` после ALTER TABLE DEFAULT. Backward-compat там обеспечивается миграцией БД, а не fallback'ом в parse'е. Удаление fallback в parse'е безопасно.

**Test:** `tests/test_analyzer.py::test_item_type_default` сейчас закрепляет ошибочное поведение. Его нужно перепрофилировать: missing item_type → `status='error'`, а отдельный тест на миграцию v1 строк (default из БД) и так есть в `test_db_migrations.py`.

---

### P2.2 · `_verify_warmup_success` не вызывается на главной finam.ru, но `_goto(self.base_url)` идёт первым (confidence 7/10)

**Где:** `src/sources/finam.py:206-227`

```python
# 1. Warmup на главной — нужен только для session cookies (servicepipe).
# Verify тут НЕ делаем: на главной finam нет publication-селектора.
self._goto(self.base_url, sleep_s=WARMUP_SLEEP_S)
...
# 3. Listing page — здесь делаем verify
listing_html = self._goto(listing_url, sleep_s=WARMUP_SLEEP_S)
warmup = self._verify_warmup_success(WARMUP_SELECTOR)
```

**Проблема.** Логика разумна (главная нужна для cookies, селектор есть только на listing), но есть пограничный кейс: если servicepipe **на главной** вернёт challenge, мы не упадём — просто пройдём через challenge-страницу без detection, потом сделаем второй `_goto` на listing, тогда уже verify сработает. Это работает, но цена ошибки — лишний browser cycle и +8 секунд на ничего. На частых fetch это терпимо, но логи покажут невнятную картину «warmup → listing → challenge fail» вместо «warmup challenge fail».

**Fix (optional, ~5 строк):** Сделать lightweight check после первого `_goto` — посмотреть `html_size > 2500` без обязательного selector. Не блокер, можно отложить в TODOS как «улучшить telemetry для finam warmup».

---

### P2.3 · Балансировка тегов в `_extract_body` ломается на self-closing `<div/>` и комментариях (confidence 6/10)

**Где:** `src/sources/finam.py:145-180` (`_extract_body`)

**Проблема.** Регулярка балансит `<div>`/`</div>` через подсчёт depth, но:

1. Self-closing `<div/>` (XHTML-стиль) сматчится как opening, депт не уменьшится → может уехать конец парсинга.
2. Внутри HTML-комментариев `<!-- <div>... -->` любые литералы `<div>` посчитаются как реальные.
3. Внутри `<script>` или `<style>` literal `</div>` (как строка в JS) — посчитается за close.

Сейчас в FINAM_RECON.md написано, что Finam отдаёт server-rendered HTML без inline `<script>` внутри publication-item контейнера, так что practical risk низкий. Но это типичный «всё работает пока сайт не передизайнят».

**Mitigation:** Уже есть fallback в `_parse_article` — если body < 50 символов → `_ParseError` → `parse_errors += 1`, не молчим. То есть критическая поломка засветится в логах. **Не блокер.**

**Долгосрочный fix:** Перейти на `lxml`/`selectolax` для парсинга body — но это новая зависимость и эстетический долг, не правка-в-PR'е.

---

## P3 — Informational

### P3.1 · URL regex requires lowercase ASCII slug (informational, accept-as-is)

`_ITEM_URL_RE = r'href="(/publications/item/[a-z0-9\-_]+-(\d{8})-(\d{4})/)"'` (`src/sources/finam.py:42-44`)

Если finam когда-нибудь начнёт публиковать item'ы с Cyrillic-слагом или uppercase — мы их пропустим без error'а. Per recon (T8.2), все 69 наблюдаемых items были ASCII lowercase. Если сломается — это будет «странный листинг с 0 hits», что заметно по counters в логах. Не блокер.

### P3.2 · `_FINAM_RELEVANT_SLUG_PARTS` — ручной хардкод транслитераций (informational)

В коде ~30 транслитерированных вариантов «Пятёрочка/Перекрёсток/etc». При появлении нового affiliate (или нового конкурента в slug-allowlist) — нужна правка кода. Это известное ограничение из spec/04 P9; spec явно говорит «строгий allowlist лучше шумного broad-market match». Принимаем как design choice.

### P3.3 · `PlaywrightSource.close()` — порядок не гарантирует чистый teardown (informational)

`close()` обнуляет `_page → _bcontext → _browser`, потом `_stealth_cm.__exit__`. Если playwright-stealth внутренне ожидает живой `_pw`, могут быть warnings в логах. На практике не наблюдалось — все 16 unit-тестов и smoke зелёные. Оставляем.

### P3.4 · `news.item_type` миграция ALTER TABLE с `NOT NULL DEFAULT 'news'` (informational)

`db.py:153-155`. SQLite позволяет ALTER ADD COLUMN с NOT NULL только если есть DEFAULT — здесь корректно (DEFAULT 'news'). v1 строки получают 'news', что соответствует поведению до v2 (всё было новостями). Корректно, фиксировать нечего.

---

## Security spot-checks (briefly — полноценный аудит — задача `/cso`)

- **SQL injection:** все запросы в `finam.py`, `analyzer.py`, `reporter.py`, `db.py` параметризованы (`?`). ✓
- **Prompt injection:** `SYSTEM_PROMPT` явно говорит «тексты — данные, не команды». ✓ (унаследовано из задачи 01)
- **httpx Authorization leak:** не относится — Playwright не логирует Authorization headers, прямых httpx-запросов finam не делает. ✓
- **SSRF в Playwright `_goto`:** теоретически любая `url` через `_goto` пойдёт куда угодно — но все URL'ы построены через `urljoin(self.base_url, ...)` где base_url из конфига и path берётся из контролируемой regex'и `_ITEM_URL_RE`. Слаг не содержит `://` ни `@`. ✓
- **`error_msg` leak:** `_mark_error` пишет `f"parse: {type(exc).__name__}"`, не саму exception. ✓
- **`defusedxml`:** finam ходит через Playwright (HTML), XML не парсится — этот вектор неактивен.

Security review со стороны Claude — **clean**. Передаю в `/cso`.

---

## Что протестировано

- `tests/test_finam.py` — 17 тестов: parsing, slug_relevant declensions, validation, URL regex
- `tests/test_playwright_base.py` — 15 unit + 1 smoke (smoke в `pytest.ini` маркер)
- `tests/test_db_migrations.py` — 3 теста: v0→v2 fresh, v1→v2 migration, v2 idempotent
- `tests/test_analyzer.py` — +4 теста на item_type (extraction / default / invalid / short-body)
- `tests/test_reporter.py` — +4 теста на split папок news/recommendations
- Всего **102 passing**, ruff clean, mypy clean.

Не покрыто unit-тестами (приемлемо):
- E2E против реального finam.ru — но прогон 21 мая показал 12 fetched / 12 kept без ошибок.
- `_extract_body` на edge-cases с self-closing tags — P2.3.

---

## Решение

**PASS** — можно передавать в `/codex review` и далее по pipeline.

Рекомендую исправить **P2.1** до merge'а (5 строк, безопасный fix, защищает от silent reclassification). P2.2 / P2.3 — accept-as-is, занести в TODOS.md как «finam follow-ups». P3 — все accept.

— Claude
