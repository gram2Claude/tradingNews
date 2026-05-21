# Estimate 02 (codex critique) — атака на `plans/02_claude_rbc_news_plan.md`

Источник: `/codex consult` (gpt-5.5, model_reasoning_effort=medium, 84 057 токенов)
Дата: 2026-05-21
Цель: найти слабые места в плане до старта T6.1.
Связанный план: `plans/02_claude_rbc_news_plan.md`
Связанная спека: `specs/02_rbc_news_spec.md`

GATE: **FAIL** (7 P1 + 7 P2)

---

## P1 — must-fix перед T6.3

### 1. Архитектурное допущение неверное
`Source.__init__` принимает только `(base_url, user_agent)` — НЕ `SourceCfg` и НЕ `CompanyCfg`.

- `src/sources/base.py:22` — конструктор.
- `src/fetcher.py:66` — `cls(base_url=src_cfg.base_url)`.

RBC **не может** достать `company.aliases`, `company_id` или seed-persons без изменения интерфейса. Это **не** «+1 строка в SOURCE_REGISTRY».

### 2. Fetcher не передаёт контекст компании
Итерация `company × source`, но в `Source.fetch(since)` уходит только дата.

- `src/fetcher.py:44, 49`.

Нужно одно из:
- Новый конструктор: `RBCSource(..., company_cfg, company_id, db_path)`.
- Новая сигнатура: `fetch(since, context)` где `context = FetchContext(company_cfg, seed_keywords, db_conn)`.

Должно быть в плане до T6.3.

### 3. `since = max(start_date, last_fetched_published_at)` не реализовано
Текущий `_resolve_since` использует только конфиг.

- `src/fetcher.py:48, 91`.

Никакого `SELECT MAX(published_at)` нигде нет. Это правка **оркестратора**, не локальная для RBC.

### 4. «3× 403 → `error` в БД» не вписывается в pipeline
Fetcher вставляет в БД **только успешные** `RawItem`'ы.

- `src/fetcher.py:138`.

Per-article fetch failures **не имеют строки в БД**, статус некуда писать. Поля `status/error_msg/retry_count` — семантика analyzer'а, не fetcher'а.

- `src/analyzer.py:149, 285`.

### 5. Recon должен быть ДО архитектуры
Если RBC search окажется JS-rendered / JSON-backed / заблокирован / с другим форматом date-параметров — `aliases` / фильтрация / лимиты / retry-математика **меняются полностью**. T6.2 (ручная разведка DOM) обязан быть hard-gate **до** написания `rbc.py`, а в идеале — до архитектурных решений в самом плане.

### 6. Anti-bot план слабый
UA + cookies + `uniform(3, 7)` — не серьёзная защита для 2026 года.

Public HTTP-проверка показывает у rbc.ru cookie `qrator_jsr` → **Qrator bot-mitigation в игре** (ссылка: https://enterno.io/en/check/rbc.ru).

План **обязан** детектить challenge/interstitial HTML с `200 OK`, не только HTTP-коды 403/429/503. Сейчас этого нет.

### 7. Лимит 30 запросов теряет данные
При `since = max(last_fetched_published_at)` без персистентного курсора, старые непрочитанные результаты будут **навсегда пропущены** как только встают новые записи. Что-то одно из:
- персистентный `last_search_offset` в `sources`;
- расширение диапазона поиска при недосборе;
- увеличение лимита + измерение реального объёма.

---

## P2

### 1. `CompanyCfg.aliases` ломает существующие тесты
Существующие тесты конструируют `CompanyCfg` **позиционно** с 4 аргументами:

- `src/config.py:16` — dataclass.
- `src/config.py:65` — `load_config`.

Вставка `aliases` **перед** `seed_persons` (как в плане) → тесты падают.

**Фикс:** добавлять в **конец** dataclass с `default_factory=list`, в `load_config` использовать `c.get("aliases", [])`.

### 2. Скрытая связь fetch ↔ БД
«No DB changes» в части хранения статей — правда (`news` уже имеет `company_id`, `source_id`, `UNIQUE(source_id, url)` — `src/db.py:30, 45`).

Но keyword-фильтр требует **либо** DB-доступа из fetch (загрузить brands/surnames на старте), **либо** precomputed keywords из fetcher'а в Source-конструктор. План эту связь прячет.

### 3. Даты хрупкие
RBC search скорее всего отдаёт **Moscow-локальные** даты, БД хранит **UTC ISO**.

- `src/fetcher.py:145`.

Backdated articles, timezone boundaries, semantics ребуса «дата публикации vs дата индексации» в RBC → пропуски.

**Решение:** брать диапазон с overlap (`last_fetched_at - 1-2 дня`), полагаться на `UNIQUE(source_id, url)` для дедупа.

### 4. Substring match наивный
- `"X5"` → ложные срабатывания на `"OX5"`, `"X50"`, `"X5-rated"`.
- `"Чижик"` → ложные срабатывания на птицу и сленг.
- Только `headline + lead` → ложные **пропуски**, если фамилия в body.

Нужны **token boundaries** (regex `\b` для латиницы, ручная нормализация для кириллицы) или хотя бы whitespace/пунктуация-ограничители.

«70% rejection rate» в плане — **выдумано**, не измерено.

### 5. Retry-математика опасна
`wait_fixed(60) × stop_after_attempt(3)` = ~2 минуты ожидания на каждый падающий запрос. С 30 запросами worst-case wall-time ≈ **1 час** (без учёта обычных `polite_sleep`). Плохо для часового крона (когда его включат).

**Альтернатива:** `wait_exponential(multiplier=2, max=30)` + `stop_after_attempt(2)` для transient HTTP. Полные блокировки (3× подряд) → перейти в режим «cooldown» на следующий цикл, не сидеть в текущем.

### 6. SSRF allow-list может ломать RBC
Копи-паста allow-list'а `{www.rbc.ru, rbc.ru}` из `x5_ir.py`. У RBC есть связанные хосты (CDN'ы, поддомены статики). Нужно verify против реальных редиректов **до** lock'а — иначе легитимные fetch'и будут падать с SSRF-ошибкой.

### 7. T6.2 нужен durable артефакт
Без него gate — церемония. Создавать `tests/fixtures/RBC_RECON.md` с заполненными ответами:
- endpoint URL и его финальный URL после редиректов;
- HTTP status, кукисы (особенно `qrator_*`);
- есть ли карточки результатов в initial HTML (без JS) — yes/no с цитатой из HTML;
- селекторы листинга + статьи;
- формат date-параметра с подтверждённым примером;
- строка для challenge-detection (как опознать interstitial-страницу с 200 OK).

Эти ответы → ревью пользователем → подтверждение «можно начинать T6.3».

---

## P3

### 1. Analyzer / name_matcher / reporter правок не требуют — подтверждено
- Analyzer обрабатывает любой `news.status='new'` — `src/analyzer.py:151`.
- Reporter джойнит `sources` динамически — `src/reporter.py:117`.

### 2. Тесты не покрывают operational behavior
План перечисляет 9 тестов, но не упомянуты:
- mocked `polite_sleep` (verify `time.sleep` вызван между запросами);
- warmup failure / challenge page (warmup получил interstitial, не cookies);
- parser empty-page (selectolax не нашёл ожидаемых нод — raise vs return None);
- 403/429 exhaustion (3 подряд → правильный финальный state);
- request-limit truncation visibility (логируется ли явно «truncated N results»);
- изоляция клиента / cookies между источниками (cycle = x5_ir + rbc — не текут ли cookies).

### 3. Оценка 1.5 дня оптимистична
Реалистично только если RBC search = SSR HTML без блокировок. С Qrator или JS-рендером 90-й перцентиль — **4-7 дней** (включая recon + решение по Playwright + новые фикстуры + стабилизация).

### 4. Visibility missing
Логировать структурно:
- search hits (сколько вернулось из RBC);
- keyword rejects (сколько отфильтровано на этапе 1);
- fetched articles (сколько реально скачано);
- skipped из-за request cap (явный счётчик);
- challenge detections (когда сработала detection-строка);
- truncated results (когда лимит 30 обрезал выдачу).

Без этих метрик качество фильтра и потери данных не видны до post-mortem.

---

## Итоговая рекомендация Codex

> **Recommendation:** Переписать план 02 v2 — закрыть все 7 P1 (особенно интерфейс Source, последний-published-at в fetcher, Qrator detection, persistent cursor) + добавить обязательный recon-артефакт T6.2 — потому что у текущей v1 фундаментальные архитектурные допущения (Source-интерфейс, fetcher orchestrator, anti-bot модель) не соответствуют коду, и реализовать как написано без правки оркестратора **невозможно**.

---

## Reaction (заполняется пользователем перед v2)

Каждый пункт — accept / reject / defer. Под маркером **Решение:** — твой ответ.

**P1.1** (Source.__init__ не принимает CompanyCfg) — Решение: **accept**. Расширяем сигнатуру `Source.__init__` опциональным параметром `context: FetchContext | None = None` — обратно-совместимо для x5_ir, обязательно для RBC. Альтернативу через `fetch(since, context)` отверг: контекст нужен ещё в `__enter__` (для warmup logic), не только в fetch.

**P1.2** (fetcher не передаёт контекст) — Решение: **accept**. `fetcher.run_fetch` собирает `FetchContext` для каждой `(company × source)` пары и пробрасывает в конструктор. См. P1.1.

**P1.3** (last_fetched_published_at не реализован) — Решение: **accept**. `_resolve_since` в fetcher расширяется: новый параметр `source_id`, читает `MAX(published_at)` из БД, берёт `max(config_date, db_max - overlap_days)`. Overlap = 2 дня (см. P2.3).

**P1.4** (3× 403 → error в БД не работает) — Решение: **accept**. Меняю модель: transient HTTP errors на article-fetch не пишут в БД (нет строки), а просто пропускают URL внутри текущего цикла. Логирование + счётчик `skipped_blocked` в return-структуре `fetch()`. URL подберётся в следующий цикл естественно (search вернёт его снова).

**P1.5** (recon до архитектуры) — Решение: **accept**. Жёстко переставляю фазы: T6.1 (recon + RBC_RECON.md) → T6.2 (конфиг) → T6.3 (impl) → T6.4 (тесты) → T6.5 (e2e). Без зелёного RBC_RECON.md код не пишется.

**P1.6** (Qrator detection) — Решение: **accept**. В rbc.py: функция `_looks_like_challenge(html, headers)` — проверяет наличие `qrator_jsr`/`__qrator` cookies на response, ищет в HTML строки `"Qrator"`, `"проверка браузера"`, `"Just a moment"`. Срабатывание → log warning, перейти в cooldown (см. P2.5), не парсить как валидную статью.

**P1.7** (лимит 30 теряет данные) — Решение: **accept (модифицированный)**. Не делаем persistent cursor — слишком много state. Вместо этого: (а) увеличиваю лимит до 50 req/cycle, (б) в search-этапе сортируем результаты от старых к новым → если упёрлись в лимит, теряем только хвост свежих, которые подберутся следующим циклом, не наоборот. Это работает потому что `since` будет двигаться вперёд только когда мы реально дочитали хвост.

---

P2/P3 — взял на себя:

**P2.1** (aliases в конец dataclass) — Решение: **accept** как описано codex. Добавляем в конец `CompanyCfg` с `default_factory=list`, в `load_config` — `c.get("aliases", [])`. Fallback на `[name]` если пусто.

**P2.2** (скрытая связь fetch↔БД) — Решение: **accept**. Делаем явно: `FetchContext` содержит `db_path: Path` + helper `load_keywords()` который загружает brands+surnames на первый вызов и кеширует. Связь видна в типе.

**P2.3** (date overlap window) — Решение: **accept**. `overlap_days = 2` константа в `fetcher.py`. Дедуп решает `UNIQUE(source_id, url)` — повторно скачанные статьи не дублируются.

**P2.4** (token boundaries) — Решение: **accept**. Для латинских алиасов — `re.search(r'\b' + re.escape(kw) + r'\b', text, re.IGNORECASE)`. Для кириллических (бренды, фамилии) — обрамляем `[^\wа-яёА-ЯЁ]|^|$`. Helper `_keyword_match(text, keywords)` в `rbc.py`.

**P2.5** (retry math) — Решение: **accept**. `wait_exponential(multiplier=2, min=2, max=30)` + `stop_after_attempt(2)`. После 2 неудач подряд — флаг `_cooldown_active=True`, остаток `fetch()` ранний return с логом. Следующий цикл начнётся чисто.

**P2.6** (SSRF allow-list verify) — Решение: **accept**. В T6.1 (recon) явный пункт: запросить главную rbc.ru, открыть статью, зафиксировать все хосты в редирект-цепочке и в финальном URL. Allow-list собирается из реальных данных, не предположений.

**P2.7** (RBC_RECON.md durable artifact) — Решение: **accept**. `tests/fixtures/RBC_RECON.md` — конкретный шаблон с обязательными ответами, gate перед T6.3.

**P3.1** (analyzer/name_matcher/reporter — no changes) — Решение: **accept**. Подтверждено.

**P3.2** (дополнительные тесты) — Решение: **accept все 6 пунктов**. Добавляю тесты: mocked sleep, warmup failure, parser empty, retry exhaustion, truncation visibility, client isolation. Итог ~15 тестов вместо 9.

**P3.3** (оценка 3-5 / 4-7 дней) — Решение: **accept**. В плане v2: T6.3 = 3-5 дней (если SSR), эскалация на Playwright = +2-4 дня. T6 целиком (с recon+тестами+e2e): 5-9 дней рабочего времени.

**P3.4** (logging visibility) — Решение: **accept**. В конце каждого `fetch()` — структурный summary log: `{search_hits, keyword_rejects, fetched, skipped_blocked, challenge_hits, truncated}`. Возвращается также в return-структуре для отображения в `cli.py status`.

---

Статус: **READY** — план v2 пишется.