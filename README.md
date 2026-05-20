# Trading News — агрегатор торговых новостей

Локальный CLI-инструмент: тянет новости о компаниях, анализирует PR-тональность
через GPT-5 mini, извлекает аффилированных лиц по seed-списку, генерирует отчёты
в Excel и Obsidian.

Подробности — в `specs/01_*.md`, `plans/01_*.md`, `estimates/01_*.md`.

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
├── specs/        # спецификации задач (01_, 02_, ...)
├── plans/        # планы реализации (тот же номер, что у спеки)
├── estimates/    # ревью планов (тот же номер)
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

См. `plans/01_trading_news_aggregator.md`, секция «Этапы»:
- [x] T1 — Скелет проекта
- [x] T2 — Парсер X5 IR (через листинг пресс-релизов с пагинацией)
- [x] T3 — LLM-анализ через GPT-5 mini + матчинг имён через pymorphy3
- [x] T4 — Reporter: Obsidian MD, persons.csv, data.xlsx, TZ=Europe/Moscow
- [x] T5 — `run.bat` для ручного запуска + раздел про Task Scheduler выше

**MVP готов.** Расширение источников (Interfax, РБК, Ведомости и т.д.) —
в отдельной спецификации `specs/02_*.md` после периода обкатки X5.
