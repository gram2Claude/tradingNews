# Spec 07 — Источник торговых рекомендаций lmsic.com/analytics/ideas

Статус: **APPROVED** (все P1-P9 закрыты; task 06 merged 2026-05-25, разблокировано)
Дата: 2026-05-23 (создан), 2026-05-25 (unblocked)
Ветка: `lmsic_ideas` от master
Зависит от:
- `01_*_spec.md` (Source ABC, БД, конфиг)
- `06_recommendations_split_spec.md` (новая таблица `recommendations`, dispatcher в fetcher, reporter dual-source)
- `04_finam_spec.md` (item_type='recommendation' в news — legacy путь при γ)
Связанный recon: `tests/fixtures/LMSIC_RECON.md` (Phase 3 — после старта задачи 07)
Связанный план: `02_plans/07_claude_lmsic_ideas_plan.md` (создаётся после APPROVED задачи 06)

**История переименования:** изначально создан как `06_lmsic_ideas_spec.md`. В ходе обсуждения P3 решено вынести БД-refactor в отдельную задачу (NN=06, `recommendations_split`), чтобы избежать большого PR. Этот спек стал NN=07.

---

## Контекст задачи (от пользователя)

Подключить **lmsic.com** как третий источник в pipeline — после x5_ir (пресс-релизы X5) и finam (новости + рекомендации finam).

**URL аналитики:**
```
https://www.lmsic.com/analytics/ideas/
```

lmsic — инвестиционная компания (LMS Invest Company?), публикующая торговые идеи по российским публичным эмитентам. X5 — один из освещаемых эмитентов; в ленте идей есть свежий разбор от 19.05.2026 («падение трафика во всех форматах сети»).

Принципиальное отличие от уже подключённых источников:
- **x5_ir** — пресс-релизы X5 (всегда `item_type='news'`)
- **finam** — смешанная лента: новости + рекомендации (LLM классифицирует `item_type` per item)
- **lmsic** — **только торговые идеи** (всегда `item_type='recommendation'`, классификация не нужна)

---

## Что вскрыл первичный recon (WebFetch)

Страница `https://www.lmsic.com/analytics/ideas/`:

- **Лента идей** — хронологический список (свежие сверху)
- **Поля каждой идеи** (видны в листинге без открытия):
  - Дата (формат `DD.MM.YYYY`)
  - Эмитент / название компании
  - Заголовок-тезис
  - Текст разбора
  - Рекомендация (`buy` / `hold` / «не рекомендуется покупать»)
  - Целевая цена + потенциал
  - Мультипликаторы (EV/EBITDA, P/E, Net debt/EBITDA)
  - Ссылка на Telegram-канал автора
- **Пагинация:** кнопка "Загрузить ещё" → AJAX подгрузка. **JS обязателен.**
- **Year filter:** dropdown 2009–2026 (URL pattern `?year=YYYY`)
- **Детальная страница:** "Подробнее" раскрывает текст inline, отдельный URL — **под вопросом** (recon уточнит)

**Примеры свежих идей:**
- X5 Retail Group — 19.05.2026 — «падение трафика во всех форматах сети»
- ФосАгро — 07.05.2026 — «улучшение финансовых результатов при скромных дивидендах»
- ММК — 05.05.2026 — «Результаты ММК становятся всё хуже»

**Открытые вопросы recon'а (Phase 3):**
- Это SPA / SSR / гибрид? Нужен ли Playwright или хватит httpx + selectolax?
- Структура AJAX "Загрузить ещё" — есть ли JSON endpoint? Если да, можно обойтись без браузера.
- Есть ли anti-bot / WAF (Cloudflare / Qrator / servicepipe)?
- "Подробнее" — это раскрытие inline html в текущей странице, или fetch отдельного фрагмента?
- Стабильность URL: можно ли построить deep-link на конкретную идею (для дедупа)?
- Чем дедуплицировать строки в БД (`news.url` UNIQUE) если нет per-idea URL? Возможные кандидаты: `(date, ticker, headline)` композитный ключ как fallback.

---

## Premise Challenge

Поставь ответ рядом с каждым пунктом.

### P1. Item type — фиксируем `recommendation` без LLM-классификации

В отличие от finam, lmsic — **аналитический ресурс**, публикующий **только идеи**. Каждый item — торговая рекомендация.

**Предложение:** при fetch проставлять `item_type='recommendation'` на этапе вставки в БД (hardcoded в `LmsicSource`), а LLM (analyzer) **уже не классифицирует** — поле приходит готовым. Analyzer всё равно проходит по item'у для извлечения `mood` + `mood_reason`, но `item_type` не пересчитывает.

**Альтернатива:** оставить классификацию через LLM как у finam — для единообразия. Минус — лишний шанс ошибки LLM (например, прокрасит рекомендацию ММК как 'news').

**Рекомендация: A — hardcode `item_type='recommendation'` при fetch.**

Твой ответ: да, рекомендация A


### P2. Фильтр по эмитенту — какие идеи попадают в БД X5

lmsic освещает **много эмитентов**. Для X5-пайплайна нужны только идеи **про X5**.

Варианты фильтра (применяется на стадии fetch до вставки):

**A) Strict by эмитент-поле в листинге**
- Парсим название эмитента из листинга, сравниваем (lowercase, лемматизация surnames не нужна) против company aliases (`X5`, `Пятёрочка`, `Перекрёсток`, `Чижик`, `КЦ ИКС 5`, `ИКС 5`).
- ✅ Точно — никакой посторонней рекомендации в БД
- ❌ Если lmsic пишет про эмитента под нестандартным названием — пропустим

**B) Strong keywords match в headline + body (как у RBC)**
- Пропускаем strong-only (X5 aliases + brands), отбрасываем weak-only (surnames).
- ✅ Покрывает edge case с нестандартным именованием
- ❌ Шанс ложноположительных (рекомендация про другую сеть упоминает X5 как конкурента)

**C) Эмитент-поле AND keyword fallback**
- Сначала пытаемся match по эмитенту; если не сошлось — strong keywords в headline.
- Усложнение для marginal value.

**Рекомендация: A.** Эмитент-поле в lmsic явное и структурированное, не как у RBC где приходится разбирать тело. Если выяснится что lmsic пишет про X5 под кодовым именем — добавим fallback в B.

Твой ответ: да, рекомендация A



### P3. Структурированные поля — сохранять или нет

Идеи lmsic несут структуру: `target_price`, `recommendation` (buy/hold/sell), `multipliers` (EV/EBITDA, P/E, Net debt/EBITDA), `potential_pct`. Эти данные ценны сами по себе — их можно использовать в дальнейшем анализе (например, сравнить target по разным инвесткомпаниям).

Варианты:

**A) Только body text (как сейчас)**
- Кладём весь раскрытый текст в `news.body`, ничего не парсим отдельно.
- ✅ Минимум изменений в БД и парсере
- ❌ Структурированные поля теряются (нужно потом извлекать из тела LLM'ом или regex'ом)

**B) Парсим в БД отдельные колонки**
- Расширяем схему: `news.target_price REAL`, `news.recommendation TEXT`, `news.potential_pct REAL` и т.д.
- Migration через `PRAGMA user_version` bump (v2 → v3).
- ✅ Чисто, фильтрация / агрегация по target тривиальная
- ❌ Колонки нерелевантны для x5_ir / finam-news items (всегда NULL)
- ❌ Требует миграции, расширения reporter (новые колонки в xlsx)

**C) Парсим, но кладём в JSON-колонку**
- Одна новая `news.extra_json TEXT` для source-specific полей.
- ✅ Гибко — добавим поля для нового источника без миграций
- ❌ Запросы по target в SQL менее удобны (нужно JSON1 extension)
- ❌ Reporter всё равно нужно расширять для отображения

**Рекомендация: A для MVP** — кладём всё в body. Если в дальнейшем (отдельная задача 0?) понадобится агрегация по target/recommendation — extract step LLM'ом после задачи 06.

Твой ответ: этот вопрос давай обсудим

---

### P3-EXT. Refactor архитектуры: отдельная таблица `recommendations` (расширение P3)

**Контекст обсуждения:** пользователь предложил вынести рекомендации в отдельную таблицу БД с собственными полями `target_price`, `recommendation_action`, `potential_pct`. Это **архитектурный refactor**, не addon к task 06 — затрагивает БД, fetcher, analyzer, reporter, cloud_sync, persons-связь, тесты. Ниже — полный масштаб изменений и три уточняющих вопроса перед тем как лочить решение.

#### Что затрагивает разделение `news` ↔ `recommendations`

**1. БД (миграция v2 → v3)**
- Новая таблица `recommendations` со своей `id`, теми же базовыми полями (`company_id`, `source_id`, `url`, `headline`, `body`, `published_at`, `mood`, `mood_reason`, `status`, `error_msg`, `retry_count`, `tokens_used`) **плюс** `target_price REAL`, `recommendation_action TEXT`, `potential_pct REAL`.
- Свой `UNIQUE(source_id, url)`, свои индексы.
- Колонка `news.item_type` становится **не нужна** — можем убрать или оставить как legacy (`always 'news'`).
- **Что делать с существующими finam-recommendation строками в `news`** (уже накопились в БД)? Два варианта:
  - **a)** Migration переносит их: `INSERT INTO recommendations SELECT ... FROM news WHERE item_type='recommendation'; DELETE FROM news WHERE item_type='recommendation';` Чисто, но необратимо.
  - **b)** Оставить в `news` как «легаси»; новые finam-recs идут в `recommendations`. Грязно — split data.
  - **Рекомендую a).**

Ответ: вариант a

**2. Связь с `persons`**
- Сейчас: `news_persons (news_id, person_id)` — FK на `news.id`.
- Нужно либо: новая таблица `recommendation_persons (recommendation_id, person_id)`, либо обобщить в `item_persons(item_id, item_type, person_id)` (теряем FK-целостность).
- **Рекомендую отдельную таблицу** — единообразие с разделением news/recs.

Ответ: новую таблицу не создаем, если персона упоминается в рекомендациях, добавлем в существующую таблицу

**3. Код (затрагиваемые модули)**
- `db.py` — SCHEMA_SQL, `_migrate_to_v3`, `status_counts` (теперь UNION над двумя таблицами или отдельная агрегация)
- `fetcher.py` — `_insert_raw_item` становится dispatcher: news → таблица news, recommendation → таблица recommendations. Sources должны сообщать тип (lmsic — hardcode `recommendation`, x5_ir — `news`, finam — см. вопрос #2 ниже)
- `analyzer.py` — два прохода: `SELECT ... FROM news WHERE status='new'` И `SELECT ... FROM recommendations WHERE status='new'`. SYSTEM_PROMPT для recommendations может быть **другим** (фокус на торговую идею, не на новостной mood), но для MVP можно оставить один
- `name_matcher.py` — без изменений (pure function)
- `reporter.py` — читает обе таблицы, разводит по папкам как сейчас. `data.xlsx` — две таблицы на разных листах ИЛИ одна с колонкой type (как сейчас)
- `cloud_sync/` — две таблицы в Postgres вместо одной + `item_type` колонки; `schema.sql` и `pusher.py` меняются
- `models.py` — новый dataclass `Recommendation` (или общий `Item` с подтипами)

Ответ: реши сам, найди оптимальное решение

**4. Тесты**
- `test_db.py`, `test_fetcher.py`, `test_analyzer.py`, `test_reporter.py`, `test_cloud_sync.py` — fixtures и моки переделать на две таблицы
- Существующие тесты под finam-recommendation flow перепишутся

**5. finam logic — главный архитектурный choice**
- Сейчас: finam пишет в `news` с временным `item_type='news'`, после analyzer перевыставляет на `'recommendation'` если LLM так решил.
- После refactor'а возникает дилемма:
  - **(α)** Сначала LLM-классификация → потом insert в нужную таблицу. Меняет порядок: analyzer становится частью fetch-стадии. Серьёзный refactor pipeline'а.
  - **(β)** Insert в news, потом по analyze-результату **переносим строку** в recommendations (move row across tables). Некрасиво, риск рассогласования.
  - **(γ)** Компромисс: finam инсёртит в `news` как было; вторая таблица заполняется **ТОЛЬКО из источников, которые знают что они recommendation-only** (lmsic). finam-смешанная-лента остаётся в news + item_type. Избегает move-across-tables, но `item_type` остаётся в `news` (значит refactor не полный).
  - **(δ)** Радикальное упрощение: **finam = always news**, item_type вообще убираем, recommendations берём только из lmsic (и других специализированных источников в будущем). Логично — finam это news-aggregator, а торговые идеи отдельно через специализированные ресурсы. **Самый чистый вариант с точки зрения архитектуры.**

Ответ: **(γ)** Компромисс: finam инсёртит в `news` как было; вторая таблица заполняется **ТОЛЬКО из источников, которые знают что они recommendation-only** (lmsic). finam-смешанная-лента остаётся в news

#### Оценка объёма

- **~1.5-2 дня работы** (refactor + миграция + тесты), если делать всё в одной задаче.
- Vs. ~1 час если оставить одну таблицу с колонками `target_price` / `action` / `potential_pct`.

#### Уточняющие вопросы (отвечай по каждому)

**Q1. Готов на ~1-1.5 дня refactor'а** (пересмотр при γ: дешевле δ — не трогаем item_type у finam и не мигрируем existing data)?

Ответ: **ДА** (решение делегировано Claude'у — пользователь: «найди сам оптимальное решение»)


**Q2. Что делать с finam'ом** — выбери вариант из α/β/γ/δ выше:

- **α** — LLM-классификация до insert'а (большой refactor pipeline'а)
- **β** — move-across-tables после анализа (некрасиво, не рекомендую)
- **γ** — компромисс: finam → news+item_type как сейчас, recommendations только для lmsic
- **δ** — упрощение: finam = always news, item_type убираем, recommendations только из специализированных источников

**Рекомендация: δ** — самый чистый.

Ответ: **γ** (зафиксировано выше в разделе вариантов)


**Q3. Существующие finam-recommendation строки** в `news` — мигрировать в `recommendations` (1a) или оставить в `news` (1b)?

Если Q2 = δ: вопрос усложняется — finam-recommendation строки **перестают существовать концептуально** (finam теперь always news). Тогда варианты:
- **1a'** — мигрировать существующие finam-rec строки в `recommendations` (one-shot, исторические остаются как recs)
- **1b'** — оставить в `news`, переименовать в news концептуально (item_type='news' для всех)
- **1c'** — удалить их из БД (потеря исторических данных, но чистая модель — все будущие finam = news)

Ответ: **N/A при γ** — finam-rec строки остаются в `news` как раньше; миграции данных нет.


**Q4. Разделить ли на две задачи**: 06a = refactor news/recs архитектуры (без lmsic), 06b = lmsic-source поверх новой архитектуры?

- ✅ Чисто, ревьюабельно, ship'ится по очереди — два маленьких PR вместо одного большого
- ✅ Если refactor вскроет проблему, lmsic-spec остаётся валидной
- ❌ Два цикла WORKFLOW (spec/plan/review/ship × 2)
- ❌ Между 06a и 06b ветка master будет в промежуточном состоянии (item_type убран, но lmsic ещё не добавлен)

**Рекомендация: ДА, разделить.** Refactor sáм по себе — отдельная задача с понятным scope. lmsic поверх — тривиален когда архитектура готова.

Ответ: **ДА, разделить** (решение делегировано Claude'у).

**Перенумерация задач:**
- **NN=06** → новая задача `recommendations_split` (refactor архитектуры). Spec: `01_specs/06_recommendations_split_spec.md` (создаётся отдельно).
- **NN=07** → задача `lmsic_ideas` (этот файл). Spec переименовывается в `01_specs/07_lmsic_ideas_spec.md`.

---

### Техдолг при γ (осознанный)

Reporter для recommendations-папки `output/X5/recommendations/...` будет UNION-ить два источника:
- `news WHERE item_type='recommendation'` (finam-recs — legacy)
- `recommendations` (lmsic и всё будущее)

Этот UNION останется навсегда (пока не сделаем δ-completion: выкинуть item_type из news, мигрировать finam-recs в recommendations). Принимаем как осознанный компромисс ради сохранения existing finam-rec данных.

**Action:** добавить в `TODOS.md` после ship'а задачи 06:
> δ-completion: finam → always news; existing finam-recs → recommendations table; drop news.item_type. Trigger: finam recommendation accuracy становится critical OR другой mixed-stream source появляется.

---

Все Q1-Q4 ✅. Файл переименовывается в `07_lmsic_ideas_spec.md`, отдельный `06_recommendations_split_spec.md` создаётся для refactor'а. Phase 3 (recon lmsic) сдвигается на задачу 07.


### P4. Источник pagination + backfill глубина

Видим в листинге свежие идеи; "Загрузить ещё" подгружает старые.

**Сколько backfill'им на старте?**

Финам мы зафиксировали на ~70 items (то что в первой странице). Здесь годовой фильтр позволяет точечно брать архивы.

**Варианты:**

**A) MVP: только первая страница (что отдаёт GET без AJAX)**
- ✅ Простая логика, никакого AJAX/JS
- ❌ Покрытие ограничено — наверное 10-20 свежих идей. Если нет идеи про X5 в этом окне — нулевой fetch.

**B) Подгружать N страниц через "Загрузить ещё" (или эквивалентный JSON endpoint)**
- ✅ Гарантированно ловим свежие идеи про X5 (которые могли уйти за порог)
- ⚠️ Требует понимания AJAX/JS — Phase 3 уточнит.

**C) Year-based backfill: при первом fetch — обойти годы `?year=2024,2025,2026`**
- ✅ Полный backfill — все идеи про X5 за период попадут
- ❌ Тяжелее: ~3 запроса вместо 1, плюс пагинация внутри каждого года
- ❌ Идемпотентность держится через `INSERT OR IGNORE` — ok

**Рекомендация:** определимся после Phase 3 recon'а. **Предварительно — B** (одна страница + N подгрузок, ~50-100 свежих идей), это даст backfill на 6-12 месяцев. **C** — отдельная команда `--backfill` если понадобится исторические данные.

Твой ответ: вариант A


### P5. Дедупликация — `news.(source_id, url)` UNIQUE при отсутствии per-idea URL

Если "Подробнее" — это inline expand без отдельного URL — нам нечем заполнить `news.url`. А `UNIQUE(source_id, url)` в `db.SCHEMA_SQL` — основа идемпотентности.

**Варианты:**

**A) Синтетический URL: `https://lmsic.com/analytics/ideas/#YYYYMMDD-<ticker>`**
- Конструируем стабильный fragment по дате + эмитенту.
- ✅ Уникальность держится за счёт `(дата, эмитент)` пары — две идеи про X5 в один день маловероятны, но если будут — нужен disambiguator (например, `#YYYYMMDD-x5-<slug-headline>`)
- ❌ "URL" не открывается напрямую как deep-link — пользователь Obsidian-карточки увидит хэш, который ничего не покажет на главной (если страница уже пролистана за этот item)

**B) Используем настоящий URL детальной страницы — если найдётся в Phase 3**
- ✅ Чисто, deep-link работает
- ❌ Если такого URL нет — fallback на A

**C) Поменять constraint в схеме** — добавить колонку `external_id`, UNIQUE по `(source_id, external_id)`
- ⚠️ Migration ради одного источника; пересматривает invariants других источников. Overkill.

**Рекомендация:** **B если найдём в Phase 3, иначе A с дисамбигуатором по slug-headline.** Никаких миграций схемы — переиспользуем `news.url`.

Твой ответ: рекомендация B


### P6. Playwright vs httpx — определится в Phase 3

Если "Загрузить ещё" — это просто AJAX-запрос к JSON endpoint и main HTML рендерится server-side — обойдёмся `httpx + selectolax` (как x5_ir).

Если SPA с client-side rendering без работающего no-JS fallback — Playwright (как finam).

**Решение:** Phase 3 recon'а. Не блокирует APPROVED.

Твой ответ: (не требуется — recon)


### P7. Конфиг и регистрация

```yaml
# config.yaml.sources
lmsic:
  code: lmsic
  name: "LMS Invest"
  base_url: https://www.lmsic.com/
  parser: lmsic
  enabled: true

# config.yaml.companies[X5]
companies:
  - name: X5
    finam_ticker: "x5"
    sources: [x5_ir, finam, lmsic]
```

В отличие от finam, lmsic **не требует company-specific параметра** (нет `lmsic_ticker`) — мы фильтруем по name match. Если позже добавим Магнит / Лента — config просто получит их aliases.

**Альтернатива:** добавить `lmsic_aliases: list[str]` в `CompanyCfg` для случая когда lmsic называет компанию иначе чем canonical name. Пока не вижу необходимости — `CompanyCfg.aliases` уже есть.

Твой ответ: не требует company-specific параметра


### P8. Частота fetch и cadence

Идеи у lmsic выходят редко (по примерам: ~5-15 в месяц для всего рынка, про X5 — 1-3 в месяц). Высокочастотный poll не нужен.

**Рекомендация: раз в сутки (24 часа)**, manual trigger через `python -m src fetch --company X5` или общий `cycle`. Как у других источников — `auto_run: false`, никакого Task Scheduler.

Альтернатива — 4 часа (как у x5_ir / finam) для единообразия cadence. Минимальный overhead, тот же flow.

**Рекомендация: 4 часа** (единообразие важнее экономии запросов — речь о 6 GET в сутки).

Твой ответ: 4 часа (как у x5_ir / finam) для единообразия cadence. Минимальный overhead, тот же flow


### P9. Item structure — что именно сохраняем в `news.body`

В P3 решили: body text, без отдельных колонок. Но что именно идёт в body — варианта два:

**A) Только текст-разбор** (то что под "Подробнее")
- ✅ Чистый текст для LLM
- ❌ Теряем target price / мультипликаторы (они в листинге, не в expandable части — или повторяются?)

**B) Текст-разбор + структурированный header в начале** (autoformatted)
```
Рекомендация: hold
Целевая цена: 3200 ₽ (+12%)
EV/EBITDA: 4.1x · P/E: 6.8x · Net debt/EBITDA: 1.2x

<полный текст разбора>
```
- ✅ LLM получает структуру, в Obsidian-карточке всё на месте
- ✅ Если в Phase 3 окажется что структурированные поля и текст лежат в разных DOM-узлах — мы их соединяем при `_extract_body`

**Рекомендация: B** — preformatted header помогает и LLM (mood/mood_reason точнее), и Obsidian-читателю.

Твой ответ: рекомендация B


---

## Архитектура (предварительно)

```
src/sources/lmsic.py                      ← НОВЫЙ
src/sources/playwright_base.py            ← возможно reuse (зависит от P6)
src/fetcher.py                            ← +1 строка в SOURCE_REGISTRY
src/config.py                             ← без изменений (если P7.A)
config.yaml, config.example.yaml          ← +sources.lmsic, +companies[X5].sources
tests/fixtures/lmsic_listing.html         ← НОВЫЙ — снимок страницы /analytics/ideas/
tests/fixtures/lmsic_ajax_more.json|html  ← НОВЫЙ — снимок "Загрузить ещё" response
tests/fixtures/LMSIC_RECON.md             ← НОВЫЙ — Phase 3 артефакт
tests/test_lmsic.py                       ← НОВЫЙ
```

БД, analyzer, name_matcher, reporter — без изменений (наследуют логику recommendations из task 04).

---

## Поток данных (предварительно)

```
LmsicSource.__enter__(): httpx.Client (или Playwright если P6 покажет)
  → warmup: GET https://www.lmsic.com/analytics/ideas/

LmsicSource.fetch(since):
  → parse_listing(html) → list of IdeaRow{date, issuer, headline, body, target, recommendation, multipliers, url_fragment}
  → filter by date >= since
  → filter by issuer == company (P2.A)
  → build synthetic url (P5.B fallback to P5.A)
  → format body with header (P9.B)
  → INSERT OR IGNORE: item_type='recommendation' (P1.A) hardcoded
  → log structured stats {fetched, date_filtered, issuer_filtered, kept, errors_by_type}
```

---

## Безопасность

| Риск | Митигация |
|---|---|
| WAF / anti-bot | Phase 3 уточнит. Если есть — наследуем playwright-stealth из finam |
| SSRF через user-controlled URLs | URL формируется из hardcoded base + path; ticker не используется |
| Prompt injection через body | Наследуется system prompt analyzer'а |
| XSS / HTML inj в reporter | Наследуется `_yaml_quote` + body cleaner |
| SQL injection | `?`-placeholders в INSERT, как везде |
| `_clean_text` обязателен | Headline + body прогоняется через `_clean_text` (см. memory: feedback_body_cleaning) |
| Selectolax + whitelist | Если HTML-парсинг — не regex по тегам (см. memory: feedback_body_cleaning) |

---

## Out of scope

- ❌ Другие разделы lmsic (`/analytics/reports/`, `/analytics/macro/` если такие есть)
- ❌ Telegram-канал автора (упомянут в ленте, но это другой источник)
- ❌ Multi-company fetch — поддерживаем X5; другие просто добавятся в `companies[].sources`
- ❌ Extract структурированных полей (target/multipliers) в отдельные колонки БД — отдельная задача
- ❌ Historical backfill через `?year=YYYY` — отдельная команда `--backfill` (P4.C)

---

## Открытые вопросы

- P1 — Hardcode `item_type='recommendation'` vs LLM-классификация
- P2 — Фильтр по эмитенту: strict / keyword / комбо
- P3 — Структурированные поля: только body / отдельные колонки / JSON
- P4 — Backfill глубина: одна страница / N подгрузок / по годам
- P5 — Дедупликация / синтетический URL
- P6 — httpx vs Playwright (решит Phase 3)
- P7 — Конфиг минималистично (без `lmsic_*` в CompanyCfg)
- P8 — Cadence: 4 часа (как остальные) vs суточная
- P9 — Формат body: только текст / preformatted header + текст

После ответов перевожу статус в **APPROVED** и иду в Phase 3 (recon) → план 06.

---

## Update after task 06 ship (2026-05-25)

Task 06 (`recommendations_split`) merged в master (PR #4). Архитектура изменилась —
часть ответов в этой спеке нужно перечитать под новый код:

### P1 — `item_type` больше не применяется к lmsic

Раньше план был: hardcode `item_type='recommendation'` на этапе INSERT в `news`.

**Теперь:** lmsic объявляет `item_destination = ItemDestination.RECOMMENDATIONS` как
class-level атрибут `Source`. `fetcher._insert` диспатчит в `_insert_into_recommendations`
по этому полю и пишет в **отдельную таблицу `recommendations`** (со структурными
полями `target_price`, `recommendation_action`, `potential_pct`, `multipliers_json`).
Колонка `news.item_type` к lmsic-данным вообще не относится — мы не пишем в `news`.

P1.A "hardcode item_type" → **заменено на:** lmsic.`item_destination = ItemDestination.RECOMMENDATIONS`.

### P3-EXT — реализовано в task 06

Все архитектурные пункты P3-EXT (`recommendations` table, dispatcher, persons junction
`recommendation_persons`, reporter UNION, cloud sync 7-table push) — **ship'нуты**.
γ-стратегия для finam подтверждена. δ-completion трекается в `TODOS.md`.

### Новое в RawItem (готово принимать lmsic-данные)

`src/sources/base.py:RawItem` уже расширен опциональными полями:
- `target_price: float | None`
- `recommendation_action: str | None` (`'buy'` / `'hold'` / `'sell'`)
- `potential_pct: float | None`
- `multipliers_json: str | None`

lmsic-парсер должен парсить эти поля из листинга/детальной страницы. Если recon
покажет что P/E + EV/EBITDA + Net debt/EBITDA нужно сериализовать — формат:
`{"ev_ebitda": 4.1, "p_e": 6.8, "nd_ebitda": 1.2}`.

### `recommendation_action` enum в БД

Postgres mirror имеет CHECK: `recommendation_action IN ('buy','hold','sell')`. SQLite
проверки нет, но lmsic-парсер должен мапить русское «не рекомендуется покупать» → `'sell'`.
Маппинг — задача парсера, не LLM (рекомендация в lmsic — структурное поле, не текст).

### Что ещё в силе из старых ответов

- P2.A — фильтр по эмитент-полю в листинге ✅
- P4.A — MVP первая страница, без AJAX ✅
- P5.B — реальный URL детальной страницы (recon уточнит) ✅
- P7 — без `lmsic_*` в CompanyCfg ✅
- P8 — cadence 4 часа (через общий `cycle`) ✅
- P9.B — body с preformatted header + текст ✅

### Связанный план / recon — следующие шаги

- Phase 3: `tests/fixtures/LMSIC_RECON.md` — открытые вопросы recon'а из секции «Что вскрыл первичный recon»
- Phase 2: `work_directory/02_plans/07_claude_lmsic_ideas_plan.md` — пишется после recon'а

