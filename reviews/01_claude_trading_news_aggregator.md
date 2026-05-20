# Review 01 — Pre-Landing Review агрегатора торговых новостей

Дата: 2026-05-20
Ветка: `site_news` (коммит `46764e4`)
Связанная спека: `specs/01_trading_news_aggregator.md`
Связанный план: `plans/01_trading_news_aggregator.md`
Связанная оценка: `estimates/01_trading_news_aggregator.md`
Фокус: обработка ошибок API источников, утечки секретов в логи, тайминги,
корректность подсчёта тональности при пустом списке новостей, прочие ошибки.
Итог: **10 issues (4 critical, 6 informational)**

---

## 🚨 CRITICAL

### [C1] Нет таймаута на вызов GPT-5 mini
**Файл:** `src/analyzer.py:165`
**Confidence:** 9/10

SDK дефолт ~10 минут. Зависание API → пайплайн висит 10 мин × N новостей.
На Task Scheduler с триггером раз в час процессы наслаиваются.

**Fix:** `OpenAI(api_key=..., timeout=60.0)` или передать `timeout=60` в
`client.chat.completions.create(...)`.

### [C2] `conn.commit()` только в самом конце `fetch_all`
**Файл:** `src/fetcher.py:117`
**Confidence:** 9/10

Если процесс падает посреди фетча из X5 (10 минут парсинга), все INSERT
теряются. Идемпотентность сохранится, но прогресс — нет. На N источников ×
600 статей лишний риск.

**Fix:** commit после каждого источника (после `_fetch_one`).

### [C3] `cycle` не валидирует OPENAI_API_KEY до фетча
**Файл:** `src/cli.py:73`
**Confidence:** 9/10

Сценарий: `.env` пуст → `fetch` отрабатывает (вставит N строк), `analyze`
падает с `RuntimeError("OPENAI_API_KEY is not set")`, `report` не
запускается. У юзера в БД новые `status='new'` без анализа и непонятный
traceback.

**Fix:** проверить `cfg.openai_api_key` в начале `cmd_cycle` и `cmd_analyze`.

### [C4] Нет ретрая на транзиентные HTTP в x5_ir
**Файл:** `src/sources/x5_ir.py:78`
**Confidence:** 8/10

`httpx.HTTPStatusError` ловится только для 404 (конец пагинации).
502/503/504 от x5.ru или `ConnectError`/`TimeoutException` пропагируются →
`errors += 1` в `_fetch_one`, **остаток источника не обрабатывается**.
Один транзиентный 502 = пропуск свежих новостей до следующего часа.

**Fix:** оборачиваем `_http_get` через `tenacity` — 3 попытки,
экспоненциальный бэк-офф на сетевые/5xx.

---

## 🟡 INFORMATIONAL

### [I1] Пустое/короткое body шлётся в LLM
**Файл:** `src/analyzer.py:138`
**Confidence:** 8/10

Если `body=""` или `len(body) < 50`, мы платим за токены, но контекст
пустой. Тратим деньги впустую, mood определится по одному заголовку.

**Fix:** если `len(body) < 50`, либо пропускаем
(`status='error', msg='body too short'`), либо помечаем `mood='neutral'`
без LLM.

### [I2] httpx.Client создаётся per request
**Файл:** `src/sources/x5_ir.py:101`
**Confidence:** 8/10

Каждый `_http_get` открывает новый TLS-handshake. На 600 статей × ~200ms
на handshake = +2 минуты впустую. Переиспользование снизит время фетча в
2-3 раза.

**Fix:** один `httpx.Client` на инстанс `X5IRSource`, открываем в
`__init__`, закрываем в `__del__` или через `__enter__/__exit__`.

### [I3] `shutil.rmtree(news_dir)` без обработки PermissionError
**Файл:** `src/reporter.py:115`
**Confidence:** 7/10

Если Obsidian открыт на одном из MD-файлов в
`output/X5/news/2026/2026_05/`, на Windows `rmtree` упадёт с
`[WinError 32]`. Юзер закрывает Obsidian — но reporter уже грохнулся,
news_dir в полуудалённом состоянии.

**Fix:** try/except PermissionError → лог + продолжаем (создаст новые MD
рядом или с другим именем).

### [I4] MAX_PAGES=50 молча обрывает фетч
**Файл:** `src/sources/x5_ir.py:54`
**Confidence:** 7/10

Если `since` далеко в прошлом и 600 статей не хватает, дойдём до страницы
50 и тихо выйдем. Никакого WARN.

**Fix:** при достижении MAX_PAGES:
`log.warning("reached MAX_PAGES=%d without hitting since=%s; older news skipped", MAX_PAGES, since)`.

### [I5] `_meta` regex требует фиксированный порядок атрибутов
**Файл:** `src/sources/x5_ir.py:152`
**Confidence:** 7/10

Паттерн `(property|name)=...[^>]+content=` ломается, если WP-плагин
выдаст `<meta content="..." property="og:title">`. Сейчас Rank Math выдаёт
нужный порядок, но смена плагина → молчаливая поломка парсинга.

**Fix:** более робастно — найти весь `<meta ...>` тег, отдельно вытащить
property/content через два независимых поиска.

### [I6] `error_msg` хранит `str(exc)` целиком
**Файл:** `src/analyzer.py:152`
**Confidence:** 6/10

В сценарии auth-ошибки или специфичных API errors тело может содержать
диагностику с request_id, эндпоинтом и т.п. Не секреты, но лишний шум в
БД. Низкая, но не нулевая вероятность утечки чего-то служебного.

**Fix:** ограничиться
`f"{type(exc).__name__}: {exc.__class__.__module__}"` для error_msg,
полный текст — только в лог.

---

## Утечки секретов в логи — проверено

- `OPENAI_API_KEY` нигде не логируется (`config.py` никогда не выводит
  значение, `analyzer.py` передаёт в SDK, SDK сам не светит ключ в
  exception body).
- `httpx` на уровне `INFO` логирует только URL+статус.
  Authorization-заголовок не в URL, не в логах.
- **НО:** если кто-то переключит логгер на `DEBUG` (например через `-v`
  флаг + изменение `_setup_logging`), httpx начнёт логировать заголовки
  → утечка ключа. Сейчас этот путь не активен. Defense-in-depth: явный
  фильтр на `logging.getLogger("httpx").setLevel(logging.INFO)`
  независимо от верхнего уровня.

## Корректность подсчёта тональности при пустом списке новостей — проверено

`reporter._write_persons_csv` SQL:

```sql
SELECT SUM(CASE WHEN n.mood='pos' THEN 1 ELSE 0 END) AS pos_freq,
       COUNT(n.id) AS total_freq
FROM persons p
LEFT JOIN news_persons np ON np.person_id = p.id
LEFT JOIN news n ON n.id = np.news_id AND n.status='analyzed'
WHERE p.company_id = ? GROUP BY p.id
```

- Нет ни одной новости → LEFT JOIN даёт NULL на `n.id` →
  `SUM(CASE WHEN n.mood='pos' THEN 1 ELSE 0 END)` = 0, `COUNT(n.id)` = 0
  (COUNT не считает NULL). **Корректно.**
- `news_persons` есть, но новость в `status='new'` → JOIN-фильтр
  `AND n.status='analyzed'` сделает `n.id` NULL → frequencies = 0.
  **Корректно.**
- Тест `test_report_creates_md_persons_xlsx` это покрывает (13 персон, 2
  упомянуты, 11 — нули).

✅ Этот блок работает правильно.

---

## Что предлагается сделать

**AUTO-FIX (6 правок, нет вопросов):**
- C1 — timeout на OpenAI
- C3 — валидация API ключа в начале `cycle`/`analyze`
- I1 — пропуск пустого body
- I4 — warning при MAX_PAGES
- I6 — короткий error_msg
- Defense-in-depth: pin httpx logger на INFO

**ASK (3 правки, нужно решение пользователя):**

1. **[C2] commit per source** — небольшое изменение архитектуры
   (передавать conn в `_fetch_one` явно с commit'ом). Делать?
   - да
2. **[C4] retry для x5_ir** — обернуть `_http_get` в `tenacity` с 3
   попытками. Это +зависимость на tenacity в sources (уже есть в
   analyzer). Делать?
   - да
3. **[I2] httpx connection reuse** — рефакторинг X5IRSource на
   `__enter__/__exit__` контекстный менеджер. Делать сейчас или отложить
   до второго источника?
- сделай сейчас
## Статус

**DONE** — все 10 правок применены, 31/31 тестов проходит, end-to-end
`cycle --company X5` отработал без ошибок.

### Применённые изменения

| # | Файл | Изменение |
|---|------|-----------|
| C1 | `src/analyzer.py` | `OpenAI(api_key=..., timeout=60.0)` + `LLM_TIMEOUT_S=60` |
| C2 | `src/fetcher.py` | `conn.commit()` после каждого источника + `with source_instance` |
| C3 | `src/cli.py` | Валидация `OPENAI_API_KEY` в `cmd_analyze` и `cmd_cycle`, fail-fast |
| C4 | `src/sources/x5_ir.py` | `@retry` на `_http_get` через `tenacity` (3 попытки, exp backoff, только транзиентные — 5xx/connect/timeout, не 404) |
| I1 | `src/analyzer.py` | Если `len(body) < MIN_BODY_CHARS=50` → пропускаем LLM, ставим `mood='neutral'`, токены=0 |
| I2 | `src/sources/x5_ir.py` + `src/sources/base.py` | Персистентный `httpx.Client` в `X5IRSource`, контекстный менеджер через `Source.__enter__/__exit__/close` |
| I3 | `src/reporter.py` | `try/except PermissionError` на `rmtree` — лог + продолжаем |
| I4 | `src/sources/x5_ir.py` | `log.warning` при достижении `MAX_PAGES=50` без cutoff |
| I5 | `src/sources/x5_ir.py` | `_meta` теперь итерирует `<meta>` через selectolax, читает атрибуты независимо |
| I6 | `src/analyzer.py` | `error_msg` хранит только `f"{type}: {ClassName}"`, полный текст — в `log.warning` |
| + | `src/cli.py` | Defense-in-depth: `httpx`/`httpcore` логгеры пинятся на INFO |
| + | `tests/test_analyzer.py` | Новый тест `test_short_body_skipped_without_llm_call` (31-й) |

