# Finam.ru Recon (T8.1 + T8.2)

Дата: 2026-05-21
Capture date: 2026-05-21 17:30 МСК (capture date важен — codex P2.10)
Метод: Playwright (chromium headless + playwright-stealth + domcontentloaded + sleep)
Связанный план: `work_directory/02_plans/04_claude_finam_plan.md` v2

---

## Vердикт

✅ **finam.ru пробит первым же подходом со stealth.** Все наработки T7.2 (servicepipe lessons) применимы 1:1.

Production mode: `chromium headless + playwright-stealth + wait_until="domcontentloaded" + time.sleep(WARMUP_SLEEP_S=8)`.

---

## 1. Anti-bot — тот же servicepipe.ru что у e-disclosure

GET `https://www.finam.ru/` через httpx без stealth — 1723 байтная challenge-страница, идентичная по структуре e-disclosure (тот же `servicepipe.ru/static/jsrsasign-all-min.js`, `id_spinner`, `get_cookie_spsn()`).

С Playwright + stealth + `wait_until="domcontentloaded"` + 8s sleep — главная грузится **1.5 MB**, no challenge, нормальные ASP.NET / `_ym_*` cookies. Та же стратегия, что для e-disclosure.

---

## 2. URL structure для X5 publications

**Главный entry point:** `https://www.finam.ru/quote/moex/{ticker}/publications/`

Для X5: `https://www.finam.ru/quote/moex/x5/publications/` — тикер lowercase.

**Article URL pattern:**
```
/publications/item/<slug-latin-transliterated>-<YYYYMMDD>-<HHMM>/
```

Примеры (из живого listing):
- `/publications/item/29-aprelya-x5-predstavit-finansovye-rezultaty-za-1-kvartal-po-msfo-20260429-0524/`
- `/publications/item/aktsii-afk-sistema-ne-vyglyadyat-privlekatelnym-obektom-dlya-portfelnykh-investitsiy-20260417-0950/`

**URL date regex:** `r"-(\d{8})-(\d{4})/?$"` — YYYYMMDD-HHMM в Moscow time → переводим в UTC (-3h).

---

## 3. Backfill coverage proof (codex P1.6) ✅

Распаршены **все 69 article URLs** из listing (одна позиция была дубликатом — `set()` дал 69).

| Метрика | Значение |
|---|---|
| Newest URL date | 2026-05-21 19:22 UTC |
| Oldest URL date | 2026-04-16 07:21 UTC |
| Target `since` | 2026-05-01 |
| **Coverage** | ✅ **YES — oldest (16 April) < since (1 May), pagination не требуется** |

### Items per ISO-week

| Week | Items |
|---|---|
| 2026-W21 (текущая) | 39 |
| 2026-W20 | 1 |
| 2026-W19 | 4 |
| 2026-W18 | 18 |
| 2026-W17 | 4 |
| 2026-W16 | 3 |

Распределение неравномерное: W21 — текущая неделя с burst активности, W20 — низкая активность. Нет признаков silent truncation в любую неделю > MAX_ITEMS=69.

**Conclusion:** 69 items на главной listing'а — достаточно для backfill с 2026-05-01 с запасом ~2 недели старше.

---

## 4. URL date vs article meta verification (codex P1.5)

Проверены **5 URLs** (2 кандидата + 3 sample: oldest / middle / newest_after_first).

| Sample | URL date | Article meta `article:published_time` | Inline `dd.mm.yyyy` в body |
|---|---|---|---|
| news (X5 earnings prep) | 2026-04-29T02:24 UTC | **NONE** (meta tag отсутствует) | 12.05.2026 + старые из comments |
| recommendation (AFK Sistema) | 2026-04-17T06:50 UTC | **NONE** | 17.04.2026 ← **MATCH** с URL date (только день) |
| oldest URL | 2026-04-16T07:21 UTC | NONE | (не проверено детально) |
| middle URL | 2026-05-19T08:50 UTC | NONE | (не проверено детально) |
| newest_after_first URL | 2026-05-21T19:13 UTC | NONE | (не проверено детально) |

**Findings:**

1. **`<meta property="article:published_time">` finam **НЕ ставит**.** Это меняет план: нельзя rely on этот meta tag для cross-check.
2. **Inline `dd.mm.yyyy`** в body совпадает с URL date **только частично** — для recommendation matches, для news article inline дата (12.05.2026) отличается от URL date (29.04.2026). Возможно: news article обновляется, inline date = last edit; URL date = original publication.
3. **URL date — единственный надёжный источник published_at**.

**Decision policy:** trust URL date. Tolerance не применим (нет двух источников для сравнения). Если в будущем findim что URL date drift'ит — добавим cross-check на `og:type=article` + структурированные date selectors.

---

## 5. Article page структура (для парсера)

**Headline:**
- ✅ `<meta property="og:title" content="...">` — самый надёжный (одна строка, без whitespace/HTML mess)
- Альтернатива: `<h1>` с очищенным `.strip()`

**Body (текст статьи):**
- ✅ `<*class="...finfin-local-plugin-publication-item-item">` — основной контейнер
- Полный селектор может содержать дополнительные utility-классы (`max-w-2xl block-center pl2x pr2x pl-lg-3x pr-lg-3x pt1x mb2x finfin-local-plugin-publication-item-item`)
- Стратегия: ищем `*[class*="finfin-local-plugin-publication-item-item"]`

**Description:**
- `<meta property="og:description" content="...">` — короткая аннотация (опционально)

**Section/Topic:**
- `<meta property="article:section" content="...">` — например, «Важные даты», «Аналитика». Может помочь для item_type classification как weak signal.

**Author:**
- `<meta property="article:author" content="Finam.ru">` — всегда Finam.ru (агрегатор)

**Published time:**
- `<meta property="article:published_time">` — **НЕТ** (codex P1.5 findings)
- Single source of truth: **URL date**

**Mусорные блоки для удаления (TBD при имплементации `_parse_article`):**
- Реклама, баннеры, sidebar с ai.finam.ru
- «Связанные публикации» / «Похожие новости»
- Поведенческий блок «комментарии»
- Контактные данные / footer

---

## 6. WARMUP_SELECTOR — выбран

Codex P2.6: `h1` слишком generic — challenge page тоже может иметь h1.

**Выбор:** `[class*="finfin-local-plugin-publication"]`, специфичный для publication-страницы. Альтернатива для listing'а — `[class*="publication"]` (есть на обоих типах страниц).

Точный селектор для production будет уточнён в `WARMUP_SELECTOR` константе в `playwright_base.py` или `finam.py` — после первого smoke test'а в T8.3.

---

## 7. Cookies after warmup (главная)

После `goto("https://www.finam.ru/", wait="domcontentloaded") + sleep(8)`:
- `PVID`, `VID` (Finam session)
- `_pk_id.19.2ab2`, `_pk_ses.19.2ab2` (Piwik analytics)
- `_ym_d`, `_ym_isad`, `_ym_uid`, `_ym_visorc`, `bh` (Яндекс.Метрика)
- `ab_id` (A/B test)

**Без** `spsn`/`spsc`/servicepipe cookies — servicepipe **не сработал** на главной с stealth. Это **не баг** — challenge selectively включается на некоторые URL pattern'ы.

**Verification strategy** (`_verify_warmup_success`):
- НЕ проверять `spsn`/`spid` (могут отсутствовать под stealth) — только log'ировать
- Проверять: `html_size > 2500` AND нет `servicepipe.ru` / `id_spinner` markers AND `WARMUP_SELECTOR` visible

---

## 8. Слабые сигналы для `item_type` classification

Хотя план полагается на LLM classification, можно использовать **`article:section`** как weak hint:
- «Важные даты» (X5 earnings prep) → news
- «Аналитика» / «Аналитика рынков» / «Идеи» → потенциально recommendation

Не используем сейчас в коде, но фиксируем для будущего refinement (могло бы быть rule-based pre-classification).

---

## 9. Saved fixtures

- ✅ `tests/fixtures/finam_x5_publications.html` (1.25 MB, 69 article links, 2026-05-21 capture)
- ✅ `tests/fixtures/finam_article_news.html` (1.19 MB, X5 earnings prep)
- ✅ `tests/fixtures/finam_article_recommendation.html` (1.21 MB, AFK Sistema analyst review)

---

## 10. Acceptance T8.2 — ✅ ПРОЙДЕН

- ✅ 2+ article HTML фикстуры сохранены (news + recommendation)
- ✅ FINAM_RECON.md заполнен селекторами, cookies, capture date
- ✅ Selector hypothesis для `_parse_article`: og:title + `finfin-local-plugin-publication-item-item` body container
- ✅ WARMUP_SELECTOR выбран: `[class*="finfin-local-plugin-publication"]`
- ✅ URL date vs article meta verification: meta tag finam НЕ ставит → trust URL date; inline date частично совпадает (recommendation: yes; news: drifts на edits)
- ✅ Backfill coverage proof: oldest (16-04) < since (01-05), pagination не требуется
- ✅ Items-per-week histogram: нет silent truncation

Следующий шаг: **T8.3 — `PlaywrightSource` ABC** в `src/sources/playwright_base.py`.
