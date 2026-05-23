# Trading News — агрегатор торговых новостей

Локальный CLI-инструмент: тянет новости о компаниях, анализирует PR-тональность
через GPT-5 mini, извлекает аффилированных лиц по seed-списку, генерирует отчёты
в Excel и Obsidian.

Подробности — в `work_directory/01_specs/01_*.md`, `work_directory/02_plans/01_*.md`, `work_directory/03_estimates/01_*.md`.

## Установка

```powershell
# 1. Виртуальное окружение
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Зависимости
pip install -r requirements.txt

# 3. Конфиги
copy config.example.yaml config.yaml
copy .env.example .env
# Открой .env, впиши свой OPENAI_API_KEY
# Открой config.yaml, проверь global.start_date

# 4. Инициализация БД
python -m src init-db
```

После `init-db` создаётся `data/db.sqlite` со схемой и seed-данными по X5.

## Команды

```powershell
python -m src init-db                 # создать БД, импортировать seed
python -m src fetch    --company X5   # парсинг источников (T2)
python -m src analyze  --company X5   # LLM-анализ 'new' новостей (T3)
python -m src report   --company X5   # генерация Excel + Obsidian (T4)
python -m src cycle    --company X5   # fetch -> analyze -> report
python -m src status                  # счётчики new/analyzed/error
```

Параметр `--company` опционален: без него обрабатываются все включённые компании.

## Ручной запуск

Один цикл (`fetch → analyze → report`):

```powershell
python -m src cycle
```

Или просто двойной клик по **`run.bat`** в корне проекта — он активирует venv,
запустит `cycle` и оставит окно открытым (`pause`), чтобы было видно вывод.

## Автозапуск (отключён по умолчанию)

`auto_run: false` в `config.yaml`. Скрипт работает **только** по ручной команде.
Никакая задача в Windows Task Scheduler не зарегистрирована — это сделано
осознанно: пока идёт ручное тестирование пайплайна на 1 источнике (X5 IR),
автоматизация не нужна.

### Когда понадобится автозапуск раз в час

1. Открой **Task Scheduler** (Планировщик заданий) → **Create Basic Task**
2. **Name:** `trading-news-cycle` (или любое)
3. **Trigger:** Daily, начало — сегодня, время — любое
4. После создания: открой задачу → вкладка **Triggers** → **Edit** → **Advanced
   settings**: поставь галочку **Repeat task every: 1 hour** for a duration of
   **1 day**
5. **Action:** Start a program → **Program/script:** полный путь к `run.bat`
   (например `C:\Users\Oleg\Desktop\Alex\03_claude\06_trading_news\run.bat`)
6. **Start in (optional):** путь к корню проекта (`C:\Users\...\06_trading_news`)
7. **Settings:** «Run whether user is logged on or not», «Run with highest
   privileges» по желанию.
8. В `config.yaml` обнови `auto_run: true` — это пометка о намерении (на
   поведение скрипта не влияет, нужна для будущей логики и self-documentation).

### Как выключить

Task Scheduler → найти `trading-news-cycle` → правый клик → **Disable** (или
**Delete**). В `config.yaml` верни `auto_run: false`.

## Структура проекта

```
06_trading_news/
├── work_directory/   # планировочные артефакты задач:
│   ├── 01_specs/     #   спецификации (01_, 02_, ...)
│   ├── 02_plans/     #   планы реализации (тот же номер)
│   ├── 03_estimates/ #   ревью планов
│   ├── 04_reviews/   #   pre-landing code reviews
│   └── 05_security/  #   CSO security audits
├── memory/       # convention notes для Claude Code
├── seed/         # стартовые данные (CSV)
├── src/          # код пакета
├── tests/        # pytest
├── data/         # SQLite — не коммитим
├── output/       # генерируемые отчёты — не коммитим
└── logs/         # ротация по дням — не коммитим
```

## Тесты

```powershell
pytest -q
```

## Статус MVP

См. `work_directory/02_plans/01_claude_trading_news_aggregator_plan.md`, секция «Этапы»:
- [x] T1 — Скелет проекта
- [x] T2 — Парсер X5 IR (через листинг пресс-релизов с пагинацией)
- [x] T3 — LLM-анализ через GPT-5 mini + матчинг имён через pymorphy3
- [x] T4 — Reporter: Obsidian MD, persons.csv, data.xlsx, TZ=Europe/Moscow
- [x] T5 — `run.bat` для ручного запуска + раздел про Task Scheduler выше

**MVP готов.**

### Источники

| Код           | Статус                | Комментарий                                                      |
| ------------- | --------------------- | ---------------------------------------------------------------- |
| `x5_ir`       | ✅ активен            | Корпоративный сайт X5 (`www.x5.ru/ru/press-center/press-releases/`) |
| `finam`       | ✅ активен            | `finam.ru/quote/moex/x5/publications/` через Playwright + stealth |
| `rbc`         | ⏸ временно остановлен | RSS работает, но `enabled: false` — разработка приостановлена    |
| `e_disclosure`| 🚧 не реализован      | Recon есть (см. `tests/fixtures/EDISCLOSURE_RECON.md`), кода нет |

Body всех активных парсеров проходит через `_clean_text` (snimet HTML-сущности,
NBSP, control-символы; сжимает whitespace) — в БД попадает чистый plain-text
без виджет-мусора. Подробности — в `CLAUDE.md` секция «Body cleaning convention».

## Облачное зеркало (Supabase)

Локальная SQLite — source of truth. Поверх неё можно держать **read-only зеркало**
в Supabase Postgres, чтобы смотреть данные с любого компа через dashboard или
подключаться сторонними клиентами через REST/SQL.

### Настройка (один раз)

1. В [Supabase dashboard](https://supabase.com/dashboard) → **Settings → Database**:
   - Раздел **Connection string** → вкладка **Connection pooling**
   - Mode: **Transaction**, порт **6543**
   - Скопировать URI (вида
     `postgresql://postgres.<ref>:[PASSWORD]@aws-<region>.pooler.supabase.com:6543/postgres`)
   - **Pooler обязателен**: Direct connection (5432) у Supabase IPv6-only,
     не резолвится с большинства IPv4-сетей.
2. Открыть `.env`, заменить значение `SUPABASE_DB_URL` на свой URI с подставленным паролем.
3. Развернуть схему в облако:
   ```powershell
   python -m src init-cloud-db
   ```
   Создаёт `trading_news.{companies, sources, persons, news, news_persons}` в
   отдельной схеме (не пересекается с другими таблицами проекта). Idempotent —
   повторный запуск не падает.

### Использование

- `python -m src cycle` — после успешных fetch/analyze/report **автоматически**
  пушит данные в Supabase, если `SUPABASE_DB_URL` задан в `.env`. Сетевая ошибка
  не валит cycle, локальная часть всё равно проходит.
- `python -m src sync-cloud [--company X5]` — standalone push без fetch/analyze/report.

### Ограничения

- **Это additive one-way push**, не two-way sync и не полное зеркало.
  Push делает UPSERT по натуральным ключам:
  - правки **существующей** строки в Supabase UI → следующий push **затрёт** (UPDATE по конфликту PK);
  - удаление строки в SQLite → в Supabase она **останется** (никаких DELETE'ов на cloud-стороне);
  - INSERT новой строки в Supabase UI (с PK, которого нет в SQLite) → push её **не тронет**.

  Практическое следствие: правки делай локально и пушь. Если хочешь чистое
  зеркало без legacy-строк — пересоздать схему через `init-cloud-db` после
  ручного `DROP SCHEMA trading_news CASCADE` в Supabase SQL editor.
- Пароль БД и connection string хранятся **только в `.env`** (gitignored).
  Никогда не коммить `.env`. Connection string в логах автоматически маскируется
  (пароль → `***`).
- Идентификаторы в облаке — **натуральные ключи** (`companies.name`,
  `sources.code`, `news.(source_code, url)`), а не суррогатные `id`. Целочисленные
  id'шки SQLite зависят от порядка вставки и не переносимы между машинами.
