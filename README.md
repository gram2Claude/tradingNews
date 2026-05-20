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

## Автозапуск (отключён по умолчанию)

`auto_run: false` в `config.yaml`. Скрипт работает только вручную.

Когда понадобится включить расписание раз в час через Windows Task Scheduler:

1. Открой Task Scheduler → **Create Basic Task**
2. Trigger: **Daily**, повтор каждый **1 час** в течение **24 часов**
3. Action: **Start a program** → путь к `run.bat` в корне проекта
4. Settings: «Run whether user is logged on or not» + «Start in: `<project root>`»

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
- [ ] T2 — Парсер X5 IR
- [ ] T3 — LLM-анализ + матчинг имён
- [ ] T4 — Reporter (Excel + Obsidian)
- [ ] T5 — `run.bat` + документация автозапуска
