# Plan 01 — Реализация агрегатора торговых новостей

Статус: READY (v2 — учтены правки из `estimates/01_claude_trading_news_aggregator_est.md`)
Дата: 2026-05-20
Связанная спека: `specs/01_trading_news_aggregator_spec.md`
Связанная оценка: `estimates/01_claude_trading_news_aggregator_est.md`

## Изменения v1 → v2 (на основе estimate)

1. Тональность — только **PR** (`mood`), `mood_trading` пока не добавляем. Колонка
   `mood_reason` всё же добавляется — пригодится для отладки и аудита.
2. Нормализация имён через **pymorphy3** — лемматизация и матчинг словоформ.
3. **Tenacity** для retry с экспоненциальной задержкой; после **3 неудачных
   попыток** → `status='error'`, `error_msg`, лог + stderr. Канал уведомлений
   email/Telegram — отдельная спека позже.
4. Все даты для путей файлов — в `tz=Europe/Moscow` (хранение в БД — UTC ISO).
5. CLI разбит на 6 команд: `init-db`, `fetch`, `analyze`, `report`, `cycle`,
   `status`. Все принимают `--company NAME` (default: все включённые).
6. Транслитерация slug — библиотека **transliterate**.
7. **Автозапуск выключен по умолчанию.** Скрипт работает только вручную
   (`python -m src cycle`). Регистрация в Windows Task Scheduler — НЕ
   автоматизируем. Пользователь явно скажет, когда переводить в автоматический
   режим. В `config.yaml` есть флаг `auto_run: false` для документирования
   намерения; T5 (см. ниже) теперь только пишет README-инструкцию, а не
   регистрирует задачу.

---

## Архитектура

```
06_trading_news/
├── config.yaml                  # глобальные настройки + ключи (в .gitignore)
├── config.example.yaml          # шаблон без секретов (коммитим)
├── .env                         # OPENAI_API_KEY (в .gitignore)
├── seed/
│   └── x5_persons.csv           # стартовый список персон X5
├── src/
│   ├── __init__.py
│   ├── cli.py                   # init-db|fetch|analyze|report|cycle|status
│   ├── config.py                # загрузка config.yaml + .env
│   ├── db.py                    # схема SQLite + миграции
│   ├── models.py                # dataclasses: Company, Source, NewsItem, Person
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── base.py              # абстрактный Source
│   │   └── x5_ir.py             # MVP: парсер сайта X5 IR
│   ├── fetcher.py               # оркестратор fetch: проходит по источникам
│   ├── analyzer.py              # LLM-разбор (GPT-5 mini): mood_pr + persons, retry
│   ├── name_matcher.py          # pymorphy3-based матчинг русских имён к seed
│   ├── reporter.py              # генерация Obsidian MD + Excel + persons.csv
│   └── utils.py                 # логирование, slug (transliterate), TZ-helpers
├── data/
│   └── db.sqlite                # источник истины (в .gitignore)
├── output/                      # сгенерированные read-only отчёты (в .gitignore)
│   └── X5/
│       ├── news/2026/2026_05/...md
│       ├── affiliate/persons.csv
│       └── news_list/data.xlsx
├── logs/
│   └── run-YYYY-MM-DD.log       # ротация ежедневная (в .gitignore)
├── tests/
│   ├── test_x5_ir_parser.py
│   ├── test_analyzer.py         # на фиктивной новости
│   └── fixtures/                # сохранённые HTML/JSON для офлайн-тестов
├── requirements.txt
├── README.md
└── .gitignore
```

### Поток данных

```
[Task Scheduler hourly] → cli.run
   → fetcher: для каждой company × source → fetch_new_items()
       → нормализация → INSERT INTO news (status='new')
   → analyzer: WHERE status='new'
       → GPT-5 mini: {mood, mentioned_persons[]} → JSON
       → UPSERT persons, INSERT news_persons
       → UPDATE news SET status='analyzed'
   → reporter: пересчёт частот + регенерация output/X5/*
```

Идемпотентность: дедуп новостей по `(source, url)` UNIQUE-индексу.

---

## Схема БД (SQLite)

```sql
CREATE TABLE companies (
    id        INTEGER PRIMARY KEY,
    name      TEXT UNIQUE NOT NULL,         -- 'X5'
    start_date TEXT,                        -- override глобальной даты, ISO
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE sources (
    id        INTEGER PRIMARY KEY,
    code      TEXT UNIQUE NOT NULL,         -- 'x5_ir', 'interfax', ...
    name      TEXT NOT NULL,
    base_url  TEXT NOT NULL,
    enabled   INTEGER DEFAULT 1
);

CREATE TABLE news (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES companies(id),
    source_id     INTEGER NOT NULL REFERENCES sources(id),
    url           TEXT NOT NULL,
    headline      TEXT NOT NULL,
    body          TEXT,
    published_at  TEXT NOT NULL,            -- ISO UTC; для путей конвертим в Europe/Moscow
    fetched_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    mood          TEXT,                     -- 'pos' | 'neutral' | 'neg' | NULL  (PR-тональность)
    mood_reason   TEXT,                     -- одно предложение, почему такой mood
    status        TEXT DEFAULT 'new',       -- 'new' | 'analyzed' | 'error'
    error_msg     TEXT,
    retry_count   INTEGER DEFAULT 0,        -- сколько раз tenacity повторял
    tokens_used   INTEGER,                  -- prompt + completion tokens (для бюджета)
    UNIQUE (source_id, url)
);

CREATE INDEX idx_news_company_date ON news(company_id, published_at);
CREATE INDEX idx_news_status ON news(status);

CREATE TABLE persons (
    id         INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    full_name  TEXT NOT NULL,               -- 'Игорь Шехтерман'
    status     TEXT,                        -- 'CEO', 'CFO', ...
    brand      TEXT,                        -- 'X5 Retail Group' | 'Пятёрочка' | ...
    from_seed  INTEGER DEFAULT 0,
    UNIQUE (company_id, full_name)
);

CREATE TABLE news_persons (
    news_id   INTEGER NOT NULL REFERENCES news(id) ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    PRIMARY KEY (news_id, person_id)
);
```

Частоты `pos_freq/neg_freq/zero_freq/total_freq` НЕ хранятся в таблице — считаются
запросом в `reporter.py`:

```sql
SELECT
  p.full_name, p.status, p.brand,
  SUM(CASE WHEN n.mood='pos'     THEN 1 ELSE 0 END) AS pos_freq,
  SUM(CASE WHEN n.mood='neg'     THEN 1 ELSE 0 END) AS neg_freq,
  SUM(CASE WHEN n.mood='neutral' THEN 1 ELSE 0 END) AS zero_freq,
  COUNT(n.id) AS total_freq
FROM persons p
LEFT JOIN news_persons np ON np.person_id = p.id
LEFT JOIN news n          ON n.id = np.news_id AND n.status='analyzed'
WHERE p.company_id = ?
GROUP BY p.id
ORDER BY total_freq DESC;
```

---

## config.yaml (шаблон)

```yaml
global:
  start_date: 2026-05-01
  llm_provider: openai
  llm_model: gpt-5-mini
  output_root: ./output
  db_path: ./data/db.sqlite
  auto_run: false              # автозапуск выключен; запускать вручную через cycle

companies:
  - name: X5
    start_date: 2026-05-01    # null → берётся global.start_date
    sources: [x5_ir]          # на MVP — только один
    seed_persons: ./seed/x5_persons.csv

sources:
  x5_ir:
    name: X5 IR
    base_url: https://www.x5.ru/ru/investors/
    parser: x5_ir
  interfax:
    name: Interfax
    base_url: https://www.interfax.ru/
    enabled: false           # включим после MVP
  # ... rbc, vedomosti, kommersant, e_disclosure, moex, finam, forbes
```

`.env`:
```
OPENAI_API_KEY=sk-proj-...
```

---

## LLM-промпт для анализа (analyzer.py)

Используем GPT-5 mini с JSON-режимом. Промпт:

```
Ты анализируешь новость о компании "{company}" с точки зрения PR-репутации бренда.

Известные персоны и их бренды (используй ТОЛЬКО эти имена в mentioned_persons,
не выдумывай новых):
{seed_persons_table}

Новость:
HEADLINE: {headline}
BODY: {body}

Верни ТОЛЬКО валидный JSON, без markdown:
{
  "mood": "pos" | "neutral" | "neg",
  "mood_reason": "одно предложение почему",
  "mentioned_persons": ["Игорь Шехтерман", ...]   // только из списка выше
}
```

Если LLM вернул персону вне seed — игнорируем на MVP (логируем). На втором этапе
добавим pipeline «новая персона → ручная валидация».

---

## Парсер X5 IR (MVP)

Сайт `x5.ru/ru/investors/news/` отдаёт SSR-HTML со списком новостей. План:

1. `httpx.get(base_url + 'news/')` → `selectolax.HTMLParser`.
2. Селектор: карточки новостей (определю эмпирически на этапе T2 ниже).
3. Для каждой карточки: ссылка, дата, заголовок → переход на детальную страницу → body.
4. State per source: храним `last_fetched_url` в таблице `sources` или в отдельной
   `fetch_state` — стопаем парсинг, когда дошли до уже виденной url.

User-Agent: реальный браузерный. Rate limit: 1 req/сек.

---

## Этапы (boil the lake по слоям, не по фичам)

### T1. Скелет проекта (день 1)
- `requirements.txt`: `httpx`, `selectolax`, `openai`, `openpyxl`, `pyyaml`,
  `python-dotenv`, `tenacity`, `pymorphy3`, `pymorphy3-dicts-ru`, `transliterate`,
  `pytest`
- `config.example.yaml`, `.env.example`, `.gitignore`
- `src/cli.py` с командами (заглушки):
  - `init-db` — создать БД, импортировать seed
  - `fetch [--company X5]` — только парсинг источников → status='new'
  - `analyze [--company X5]` — только LLM-анализ 'new' → 'analyzed' / 'error'
  - `report [--company X5]` — генерация Excel + Obsidian + persons.csv
  - `cycle [--company X5]` — fetch → analyze → report (для Task Scheduler)
  - `status [--company X5]` — счётчики new/analyzed/error по компаниям
- `src/db.py`: создание схемы, миграции (наивно — версия в `PRAGMA user_version`)
- `src/config.py`: загрузка yaml + env
- Seed: `seed/x5_persons.csv` → импорт в `persons` при `init-db`
- README с инструкцией запуска
- **Acceptance:** `python -m src init-db` создаёт `data/db.sqlite` с 5 таблицами и
  13 seed-персонами по компании X5.

### T2. Парсер X5 IR (день 2-3)
- `src/sources/base.py`: интерфейс `fetch(since: datetime) -> list[RawItem]`
- `src/sources/x5_ir.py`: реализация
- Сохранить 2-3 HTML-фикстуры в `tests/fixtures/` (curl с разных дней)
- Юнит-тесты на парсинг фикстур (офлайн)
- `src/fetcher.py`: оркестратор + INSERT в `news` со `status='new'`, дедуп по
  `UNIQUE(source_id, url)`. `published_at` сохраняется как UTC ISO.
- **Acceptance:** `python -m src fetch --company X5` тянет реальные новости с
  X5 IR и кладёт в БД; повторный запуск не создаёт дубликатов.
- **Ручная валидация:** открыть `data/db.sqlite` через `sqlite3` или DB Browser,
  глазами проверить 10 новостей на адекватность.

### T3. LLM-анализ + матчинг имён (день 4)

**3a. Name matcher (`src/name_matcher.py`)**
- На старте: загрузить seed-список персон, для каждой построить набор форм через
  `pymorphy3` — лемму фамилии и имени, инициал-форму («И. Шехтерман»), latin-форму
  (опционально).
- Функция `match(text: str) -> list[Person]`: токенизация текста, лемматизация
  каждого N-грамма (1–3 слова), сравнение с lemma-индексом persons.
- Возвращает только канонические `full_name` из seed → исключает дубли в БД.
- Юнит-тест: 5 русских предложений со всеми падежами Шехтермана → все находятся.

**3b. Анализатор тональности (`src/analyzer.py`)**
- Батчевая обработка `WHERE status='new' AND retry_count < 3`, по одной новости =
  один запрос к GPT-5 mini через OpenAI SDK
  (`client.chat.completions.create` с `response_format={"type": "json_object"}`).
- Промпт просит: `mood` (pos/neutral/neg) + `mood_reason` (одно предложение).
  Извлечение персон НЕ делаем через LLM — это работа name_matcher на тексте.
- Валидация JSON, `mood ∈ {pos,neutral,neg}` иначе ошибка.
- Retry через **tenacity** (`stop_after_attempt(3)`, `wait_exponential` 2–30 сек,
  ретраим только сетевые/rate-limit/timeout, НЕ парсинг-ошибки).
- После анализа: `name_matcher.match(headline + ' ' + body)` → связи в
  `news_persons`.
- На каждой попытке инкрементируем `retry_count`. После 3-й неудачной попытки →
  `status='error'`, `error_msg`, лог + stderr, идём дальше.
- Логирование `tokens_used = usage.prompt_tokens + usage.completion_tokens`.
- Юнит-тест: мок ответа OpenAI SDK + проверка retry-логики на 429.
- **Acceptance:** `python -m src analyze` — все 'new' переходят в 'analyzed' c
  `mood`, `mood_reason` и связанными персонами из seed; при искусственной 429
  отрабатывает retry и в худшем случае ставит 'error'.

### T4. Reporter (день 5)
- `src/reporter.py`:
  - **Все даты для путей и имён файлов конвертим из UTC → `Europe/Moscow`**
    (через `zoneinfo.ZoneInfo("Europe/Moscow")`).
  - SQL-агрегат частот → `output/X5/affiliate/persons.csv`
  - SELECT всех новостей → `output/X5/news_list/data.xlsx` (openpyxl,
    запись через `tempfile` + `os.replace` чтобы не падать при открытом Excel)
  - Для каждой новости → `output/X5/news/YYYY/YYYY_MM/yyyy_mm_dd_slug_NN.md`:
    ```yaml
    ---
    date: 2026-05-20
    source: x5_ir
    url: https://...
    mood: pos
    persons: [Игорь Шехтерман, Екатерина Лобачёва]
    ---
    # {headline}

    {body}
    ```
  - Slug: первые 5 слов заголовка → `transliterate.translit('ru', reversed=True)`
    → нижний регистр, не-alphanum → `_`, схлопывание `__` → `_`
  - NN — порядковый номер новости в этот день (для коллизий)
- **Acceptance:** `python -m src report --company X5` пересоздаёт `output/X5/*`
  идемпотентно; при открытом `data.xlsx` в Excel — пишет во временный файл и
  логирует предупреждение, не падает.

### T5. Ручной запуск + документация автозапуска (день 5)
**Автоматический режим НЕ включаем.** Скрипт работает только по ручной команде.

- Создать `run.bat` в корне: активация venv + `python -m src cycle` — удобство
  для ручного двойного клика, без расписания.
- Логи: `logs/run-YYYY-MM-DD.log` через `logging.FileHandler` — всегда пишутся,
  независимо от режима.
- В README — раздел «Как включить автозапуск (когда понадобится)» с пошаговой
  инструкцией для Windows Task Scheduler. **Команды не выполняем**, только
  документируем.
- В `cli.py` при старте `cycle` логировать: «manual run, auto_run=false in config».
- **Acceptance:** `run.bat` двойным кликом → новости фетчатся, анализируются,
  отчёты генерируются, лог пишется. **Никакая задача в Task Scheduler не
  зарегистрирована.**

### T6. (Следующая спека) Расширение источников
**ВНЕ ЭТОГО ПЛАНА.** После того как T1–T5 отработали неделю и есть доверие к данным
из X5 IR — заводим `specs/02_rbc_news_spec.md` для подключения Interfax/РБК/Ведомостей/etc.
Каждый источник = новый файл в `src/sources/` + запись в `config.yaml`.

---

## Что НЕ делаем на этом этапе

- ❌ Telegram-парсинг (отдельная спека, нужен API ключ)
- ❌ Финотчётность, котировки, рекомендации (отдельные спеки)
- ❌ Фронт / бэк (отдельные спеки)
- ❌ Автоматическое обнаружение новых персон (вне seed) — на MVP игнорируем
- ❌ Множество компаний — БД готова, но конфиг только под X5
- ❌ Автозапуск по расписанию — флаг `auto_run` есть, регистрация задачи —
  только когда пользователь явно скажет
- ❌ Локальная модель — оценим после прогона на GPT-5 mini
- ❌ Sentiment с торговой точки зрения — текущий промпт строго PR-репутационный

---

## Риски и митигации

| Риск | Митигация |
| --- | --- |
| X5 IR меняет верстку → парсер падает | Сохранённые фикстуры в `tests/`, fail loud в логе, ручная починка селекторов |
| GPT-5 mini галлюцинирует имена персон | Whitelisting по seed — игнорируем имена вне списка |
| Часовой крон пересекается с ручным запуском | SQLite WAL mode + короткие транзакции; на MVP — не страшно |
| `data.xlsx` открыт в Excel → reporter не может записать | Писать во временный файл и `os.replace()`; если ошибка — лог и пропустить итерацию |
| Стоимость GPT-5 mini взлетает | Логируем `usage.input_tokens` в `news.cost_tokens` (опционально добавить колонку), смотрим раз в неделю |

---

## Acceptance (план в целом)

Через ≤5 дней работы:
1. `python -m src init-db` → пустая БД + 13 персон.
2. `python -m src run` → новости X5 IR за последние 30 дней попадают в БД с mood.
3. `python -m src report` → `output/X5/` содержит MD-файлы новостей, persons.csv с
   ненулевыми частотами, data.xlsx со всеми новостями.
4. Task Scheduler настроен, при ручном запуске лог пишется.
5. Юнит-тесты T2 и T3 проходят локально.

---

## Следующие шаги после plan

Жду твоего ОК на план. Дальше:
1. Создаю T1 (скелет) — это коммит «chore: project scaffold».
2. Иду по T2 → T5 последовательно, коммит на каждый этап.
3. После T5 — пинг тебе на ревью и решаем по `specs/02_rbc_news_spec.md` (расширение источников).
