# Spec 04 — Источник публикаций finam.ru + Playwright инфраструктура

Статус: APPROVED
Дата: 2026-05-21
Ветка: `e_disclosure_news` (продолжаем на ней, переименуем при ship)
Заменяет: `specs/03_e_disclosure_spec.md` (SUPERSEDED — e-disclosure избыточно сложен; finam даёт быстрее результат)
Зависит от: `specs/01_*_spec.md` (архитектура Source ABC, БД, конфиг)
Связанный recon: `tests/fixtures/EDISCLOSURE_RECON.md` (T7.1+T7.2 — все наработки про Playwright + stealth + servicepipe применимы 1:1)
Связанный план: `plans/04_*_plan.md` (создаётся после APPROVED)

---

## Контекст замены источника

Сначала спланировали e-disclosure.ru (spec 03). Дошли до T7.2 — установили Playwright, нашли способ обхода servicepipe (stealth + domcontentloaded + sleep), нашли X5 ID (39008). Затем пользователь предложил **finam.ru** как альтернативу.

**Почему finam лучше для нашей задачи:**
- ✅ Та же servicepipe WAF — обход уже отработан в T7.2
- ✅ **Дата прямо в URL** статьи (`...20260429-0524/`) → можно фильтровать по дате без открытия каждой статьи (огромная экономия Playwright времени)
- ✅ Структура URL — чистая (`/publications/item/<slug>-YYYYMMDD-HHMM/`), легко парсить
- ✅ На странице компании уже **70 публикаций** видно → backfill с 2026-05-01 точно покрыт
- ✅ Контент шире чем e-disclosure: новости + аналитика брокеров + макро-контекст + competitor news (всё что трейдеру нужно)
- ✅ Стандартный H1, title, meta — обычный SSR-сайт после прохождения challenge'а

**Что сохраняем из spec 03 (полезные наработки):**
- Playwright + playwright-stealth + Chromium binary установлены в `.venv`
- Лессоны про `wait_until="domcontentloaded" + sleep(5-8)` вместо `networkidle`
- `_verify_warmup_success` подход с проверкой html size + servicepipe markers + expected selector
- Servicepipe IP throttle понимание (после 5-10 быстрых подходов — блок на 15-60 мин; 4-часовая cadence далеко за threshold)
- Структура `PlaywrightSource(Source)` ABC из плана 03 v3

**Что отбрасываем из spec 03:**
- e_disclosure_id концепция (finam использует ticker в URL, не digit id)
- Поиск через form submit (finam — прямой URL по тикеру)
- Категорийный `files.aspx?type=N` (finam не нужен)

---

## Исходное описание (от пользователя)

Подключить **finam.ru** как источник публикаций по конкретному эмитенту.
Пример URL для X5:
```
https://www.finam.ru/quote/moex/x5/publications/
```

URL pattern для других компаний: `/quote/moex/<TICKER>/publications/` (тикер на MOEX).

В рамках MVP — только X5. Архитектура должна позволять подключение других тикеров через config.yaml.

---

## Что вскрыл живой probe (T8.1)

Прогнал `playwright-stealth + chromium headless + domcontentloaded + sleep(8)` на:
- `https://www.finam.ru/` — 1.5 MB, реальная главная, `is_challenge=False`
- `https://www.finam.ru/quote/moex/x5/publications/` — 1.2 MB, **H1 «КЦ ИКС 5: новости»**, 70 article links, `is_challenge=False`

**Структура URL статьи:**
```
/publications/item/<slug>-<YYYYMMDD>-<HHMM>/
                                  ↑8 цифр  ↑4 цифры
```

Примеры реальных URL:
- `/publications/item/29-aprelya-x5-predstavit-finansovye-rezultaty-za-1-kvartal-po-msfo-20260429-0524/`
- `/publications/item/aktsii-afk-sistema-ne-vyglyadyat-privlekatelnym-obektom-dlya-portfelnykh-investitsiy-20260417-0950/`

**Дата извлекается из URL regex'ом** — `r'(\d{8})-(\d{4})/$'`. Это значит:
- Filter по `since` можно сделать **до открытия статьи** — open page only for items с датой ≥ since
- Огромная экономия Playwright времени

Сохранил `tests/fixtures/finam_x5_publications.html` для офлайн-тестов парсера.

**Открытые вопросы recon'а (для T8.2 — следующий уровень):**
- Pagination: 70 видно сразу, но как загрузить старее? SPA scroll? URL `?page=N`? Не видно в HTML — нужна интерактивная проверка
- Per-article HTML structure: пока не открывал ни одну статью, нужны селекторы заголовка / даты / тела
- Контент-фильтр: 70 публикаций про X5-сектор включают competitor news; нужно решить — пропускаем всё или фильтруем

---

## Premise Challenge

Поставь ответ рядом с каждым пунктом.

### P1. Bundle vs split (как в spec 03)

Та же дилемма: `PlaywrightSource` infra + `FinamSource` в одном PR или раздельно?

**Рекомендация: bundle (как было в spec 03 P1).** Personal-tool MVP — скорость важнее.

Твой ответ: bundle (как было в spec 03 P1)


### P2. Filter — что пропускать через `/quote/moex/x5/publications/`

Эта страница содержит публикации **связанные с X5-сектором**, не только про X5 саму. Примеры (из probe 70 items):
- ✅ X5 квартальные отчёты («29-aprelya-x5-predstavit-finansovye-rezultaty...»)
- ⚠️ AFK Sistema (другая компания, но связана через сектор / макро)
- ⚠️ Квантовые компании (полностью посторонний контекст)

**Варианты:**

**A) Без фильтра, всё подряд (как finam решил)**
- ✅ Получаем context-broader analyst material (competitor news часто двигает наши акции)
- ✅ Просто реализовать
- ❌ Шум: посторонние истории про несвязанные сектора попадают в БД
- ❌ Раздувает LLM-затраты на нерелевантный анализ

**B) Strong-only filter (как у RBC после правок task 02)**
- Применяем `Keywords.strong` (X5 / Пятёрочка / Перекрёсток / Чижик / Корпоративный центр ИКС 5 / ИКС 5 etc.) к headline или slug
- ✅ Только прямые упоминания X5 проходят
- ❌ Теряем competitor / sector context

**C) Strong OR sector-related (комбо)**
- Strong (X5 + бренды) пропускают всегда
- Sector-tagged публикации (которые finam привязал к X5 сектору) — пропускают через weak-filter на основе известных competitor names: Магнит, Лента, Окей, Fix Price
- ⚠️ Усложнение для marginal value

**Рекомендация: B (strong-only)** — для MVP. C можно сделать отдельной мини-спекой если выяснится что context-broader полезен.

Твой ответ: A) Без фильтра, всё подряд (как finam решил). Если посмотреть внимательнее новость 
про AFK Sistema, то там внутри текста есть торговая рекомендация относительно X5.
т.е. по сути это не новость, а рекомендция. 
- создай в папке X5 новую папке recomendations
- сохраняй в нее такие файлы-рекомендации в таком же формате, как новости
- на других этапах вернемся к этим файлам, когда будем анализировать торговые рекомандации

### P3. Pagination и backfill

В первом probe видно **70 публикаций** на главной странице компании. Я не проверил можно ли загрузить более старые.

**Гипотезы:**
- 70 — это «все за последние ~6-12 месяцев» (тогда backfill с 2026-05-01 покрыт)
- 70 — это «последние 70» (тогда нужно понять как идти дальше)

T8.1 (recon) уточнит — но scope-decision на сейчас:

**A) MVP: только то что в первом HTML (70 items)**
- ✅ Просто, не нужна pagination логика
- ✅ Достаточно для backfill с 2026-05-01 при условии что 70 публикаций покрывают период

**B) Если 70 < нужный период — реализовать pagination в T8.2**
- ⚠️ +0.5-1 день на код

**Рекомендация: A для MVP**, если в T8.1 окажется что 70 недостаточно — пересмотрим.

Твой ответ: A для MVP


### P4. Частота fetch

Та же логика что у spec 03 P4: 4-часовая cadence, `auto_run: false`, запуск по команде.

**Рекомендация: 4 часа + manual** (как у RBC).

Твой ответ: 4 часа + manual** (как у RBC) - запуск по команде


### P5-P6 — Playwright параметры (наследуются из spec 03)

P5: headless по умолчанию + `PLAYWRIGHT_HEADLESS=false` env override — принято.
P6: in-memory BrowserContext (servicepipe видим решает challenge при первом goto через stealth) — принято.

Твой ответ: подтверждаешь как в spec 03? (если да — переписывать в спеке не нужно, просто «да»)
да

### P7. Тикеры — поддержка multiple companies

Архитектура источника предполагает, что URL компании = `/quote/moex/<TICKER>/publications/`.

Для **X5 тикер на MOEX = `x5`** (нижний регистр в URL — проверено в живом запросе).

В config.yaml нужно новое поле для каждой компании:
```yaml
companies:
  - name: X5
    finam_ticker: "x5"
```

Это аналог `e_disclosure_id` из spec 03, но семантически проще (тикер не digit, обычно 3-4 буквы).

**Рекомендация:** добавляем `finam_ticker: str | None = None` в `CompanyCfg` (default None, опциональное). Валидация: непустая строка из латинских букв/цифр (`re.fullmatch(r'[a-z0-9]+', ticker)`).

Твой ответ: принимаю твою рекомендацию


### P8. Конфиг и регистрация

В `config.yaml.companies[X5]`:
```yaml
finam_ticker: "x5"
sources: [x5_ir, rbc, finam]
```

В `config.yaml.sources`:
```yaml
finam:
  code: finam
  name: Финам
  base_url: https://www.finam.ru/
  parser: finam
  enabled: true
```

Твой ответ: выбери оптимальный вариант сам


---

## Архитектура (предварительно — детали в plan)

```
src/sources/base.py                       ← без изменений
src/sources/playwright_base.py            ← НОВЫЙ (как было в плане 03)
src/sources/finam.py                      ← НОВЫЙ
src/sources/x5_ir.py, rbc.py              ← без изменений
src/fetcher.py                            ← +1 строка SOURCE_REGISTRY
src/config.py                             ← +CompanyCfg.finam_ticker
config.yaml, .example                     ← +sources.finam, +companies[X5].finam_ticker
requirements.txt                          ← +playwright>=1.40, +playwright-stealth>=2.0
tests/fixtures/finam_x5_publications.html ← УЖЕ СОХРАНЕН (T8.1 probe)
tests/fixtures/finam_article.html         ← НОВЫЙ — снимок одной статьи (T8.2)
tests/fixtures/FINAM_RECON.md             ← НОВЫЙ — выделить отдельно от EDISCLOSURE_RECON
tests/test_playwright_base.py             ← НОВЫЙ
tests/test_finam.py                       ← НОВЫЙ
```

БД, analyzer, name_matcher, reporter, существующие источники — без изменений.

---

## Поток данных (для понимания scope)

```
FinamSource.__enter__(): stealth chromium headless
  → goto https://www.finam.ru/ (warmup challenge)
  → verify_warmup_success (no challenge markers, expected selector)

FinamSource.fetch(since):
  → ticker = self.context.company_cfg.finam_ticker  # "x5"
  → goto https://www.finam.ru/quote/moex/{ticker}/publications/
  → _parse_listing(html) → ~70 (url, date_from_url) tuples
  → filter by date >= since BEFORE открытия (большой выигрыш)
  → filter by keyword (strong only — P2.B) на slug
  → bulk-load known URLs из БД (один SQL — как было в плане 03 P1.4)
  → для каждого new:
      → goto article URL
      → _parse_article(html) → RawItem
  → log structured stats {fetched, date_filtered, keyword_filtered, kept, errors_by_type}
```

---

## Безопасность

| Риск | Митигация |
|---|---|
| servicepipe challenge — IP throttle | 4-часовая cadence далеко за threshold; telemetry в logs |
| Playwright headless detection | playwright-stealth (отработано в T7.2) |
| SSRF через user-controlled URLs | Не применимо — все URL формируются из hardcoded base_url + ticker (validated) + slug (просто path) |
| XSS через body статьи в reporter | Наследуется `_yaml_quote()` из reporter |
| Prompt injection через body | Наследуется system prompt из analyzer |
| SQL injection через ticker | Не доходит до SQL — ticker используется только в URL pattern |
| Memory: Chromium 200MB | Acceptable (закрывается после fetch) |
| Slug в URL — может содержать unicode / special chars | escapeURL при формировании URL; на парсинге — `urllib.parse.urlparse` |
| Дата в URL поддельная (источник врёт) | Cross-check с published_at meta tag из article HTML (если есть) |

---

## P9 (added) — Item type classification: news vs recommendation

**Контекст:** пользователь указал в P2, что некоторые публикации finam (например, «aktsii-afk-sistema-ne-vyglyadyat-privlekatelnym...») по факту содержат **торговую рекомендацию относительно X5**, а не новость. Их нужно отделять и хранить в `output/X5/recommendations/<YYYY>/<YYYY_MM>/...md` (тот же формат что у news).

**Архитектурные изменения** (принимаю как baseline для plan'а):

### 1. БД — новая колонка
```sql
ALTER TABLE news ADD COLUMN item_type TEXT NOT NULL DEFAULT 'news';
-- 'news' | 'recommendation'
```
Существующие x5_ir/rbc rows становятся 'news' (safe default, миграция через `db.init_db` PRAGMA user_version bump).

### 2. Классификация — LLM-based в analyzer
SYSTEM_PROMPT расширяется. JSON-output теперь:
```json
{
  "mood": "pos" | "neutral" | "neg",
  "mood_reason": "одно предложение",
  "item_type": "news" | "recommendation"
}
```
Критерий рекомендации (в prompt'е): «item классифицируется как recommendation если в тексте есть **конкретная торговая идея** — buy/sell/hold + ценовой ориентир (target price) ИЛИ явная оценка перспективы (`покупать`, `продавать`, `держать`, `недооценён`, `переоценён`, `целевая цена`)».

Для x5_ir/rbc items LLM будет почти всегда возвращать `news` (это пресс-релизы / новости, не аналитика). Для finam — будет смешение.

### 3. Reporter — два пути записи
- `news`-items → `output/<COMPANY>/news/<YYYY>/<YYYY_MM>/<filename>.md` (как сейчас)
- `recommendation`-items → `output/<COMPANY>/recommendations/<YYYY>/<YYYY_MM>/<filename>.md` (новое)
- Frontmatter / контент одинаковые
- Reporter wipes обе папки перед регенерацией (идемпотентность)

### 4. Excel — общая таблица, новая колонка
`output/<COMPANY>/news_list/data.xlsx` остаётся одной — но добавляется колонка `item_type` (рядом с `mood`). Это позволяет фильтровать в Excel + использовать ту же таблицу для downstream.

### 5. persons.csv — без изменений
Агрегирует упоминания персон **во всех** items, независимо от type. Recommendations тоже могут упоминать персон.

### 6. CLI — без изменений
`fetch` / `analyze` / `report` / `cycle` / `status` работают так же. `status` потенциально покажет breakdown по type'у — это P3 в plan'е.

Твой ответ: принимаешь архитектуру с item_type? (если да — план пишу с этим)
**Решение:** accept (предложено мной, нужно ack от пользователя)

---

## Out of scope

- ❌ Другие источники finam (`/news/`, `/analytics/`) — пока только `/quote/moex/<ticker>/publications/`
- ❌ ai.finam.ru (115 ссылок в HTML — это продукт) — out of scope
- ❌ Multiple companies в одном fetch — поддерживаем X5; другие компании просто требуют finam_ticker в config
- ❌ Pagination (если 70 items недостаточно) — отдельная задача
- ❌ PDF downloads, video, аудио — только текст статьи

---

## Открытые вопросы

- #P1 — Bundle vs split
- #P2 — Filter strategy: strong-only / strong+sector / no-filter
- #P3 — Pagination: MVP без или с
- #P4 — 4-часовая cadence + manual
- #P5+P6 — Playwright defaults (headless + in-memory) — подтвердить
- #P7 — `CompanyCfg.finam_ticker` подход
- #P8 — Имена секций конфига

После твоих ответов перевожу статус в **APPROVED** и пишу `plans/04_claude_finam_plan.md` с учётом наработок плана 03 v3 (Playwright lifecycle, _verify_warmup_success, error classification).
