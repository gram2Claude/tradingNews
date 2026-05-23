# Review 02 (Claude) — Pre-Landing Review: RBC source

Дата: 2026-05-21
Ветка: `rbc_news` (uncommitted working tree, ~+650 / -130 vs master)
Связанная спека: `specs/02_rbc_news_spec.md`
Связанный план: `plans/02_claude_rbc_news_plan.md` (v3, RSS-only)
Связанная оценка: `estimates/02_codex_rbc_news_est.md`
Фокус: корректность парсера и фильтра, обработка ошибок, anti-bot подход, тесты, безопасность RSS-парсинга.

Итог: **6 issues (1 critical, 2 important, 3 informational). GATE: PASS** — все P1+P2 закрыты, P3 закрыты или вынесены в TODOS.md.

---

## Resolution status (2026-05-21, после Fix-First прогона)

| # | Тема | Статус | Что сделано |
|---|---|---|---|
| 1 | Russian declensions filter | **FIXED** | pymorphy3 лемматизация в `_keyword_match` (вариант A). 12 эмпирических кейсов: ✓ все 5 склонений «Пятёрочка», все 4 склонения «Шехтерман», ✓ Latin `X5` (✗ `OX50`), ✓ multi-word `X5 Retail Group`, ✗ деривативное прилагательное. |
| 2 | `xml.etree` → `defusedxml` | **FIXED** | `defusedxml>=0.7` в `requirements.txt`, drop-in импорт в `rbc.py`. |
| 3 | Test name misleading | **FIXED** | `test_keyword_match_cyrillic_surname_with_declension` теперь реально проверяет все 5 падежей; добавлен `test_keyword_match_cyrillic_brand_with_declension`. |
| 4 | `init-db` enable sync | **DEFERRED** | Вынесено в `TODOS.md` — не блокирует, поле в БД сейчас не используется. |
| 5 | `.gitattributes` | **FIXED** | Файл создан, `* text=auto eol=lf` + per-extension правила + `.bat eol=crlf` + binary fixtures. |
| 6 | Тест на пустые keywords | **FIXED** | `test_rbc_fetch_warns_when_strong_keywords_empty` — проверяет log warning + пустой результат. |

**Тесты:** 56/56 passed (было 33 до ветки, добавлено 23 = 21 в T6.4 + 2 в /review).
**Lint:** ruff All checks passed.
**Types:** mypy 14 files clean.

---

## P1 — Critical

### 1. Фильтр пропускает русские словоформы (declensions)

**Симптом** (эмпирически воспроизведено на текущем коде):

```
[True]  kw='Пятёрочка'   text='Пятёрочка открыла магазин'         ← nominative match
[False] kw='Пятёрочка'   text='Выручка Пятёрочки выросла'         ← genitive miss
[False] kw='Пятёрочка'   text='В Пятёрочке снизили цены'          ← prepositional miss
[False] kw='Пятёрочка'   text='Пятёрочкой управляет'              ← instrumental miss
[False] kw='Перекрёсток' text='В Перекрёстке продаётся'           ← prepositional miss
[True]  kw='Шехтерман'   text='Заявил Шехтерман'                  ← nominative match
[False] kw='Шехтерман'   text='Решение Шехтермана'                ← genitive miss
[False] kw='Шехтерман'   text='Шехтерману сообщили'               ← dative miss
```

**Причина:** `_keyword_match` в `src/sources/rbc.py:184-205` использует token-boundary regex (`(?:^|[^\wа-яё])` + escape + `(?:$|[^\wа-яё])`). В склонениях после основы идёт word-character (`и`, `е`, `у`, `а`, ...), boundary не срабатывает, match не находится.

**Влияние:** **6 из 8 типичных форм русских слов из бизнес-новостей мы пропускаем.** В практике это означает потерю большинства релевантных RBC-статей про X5: "выручка Пятёрочки", "директор Перекрёстка", "поручение Шехтерману" — все мимо. Только nominative-форма проходит.

Это **прямо противоречит цели filter'а** — отсеивать нерелевантное и пропускать релевантное.

**Codex это предупреждал в `estimates/02_codex_rbc_news_est.md` P2.4:**
> Russian surnames and brand words need token boundaries **or at least regex/normalization**.

Я реализовал token boundaries, но не нормализацию. Token boundaries закрыли омонимию (`X5` vs `OX50`) — это хорошо. Но регулярные русские формы упустили.

**Решения (нужно твоё):**

**A) Лемматизировать через pymorphy3 (полное решение)**
- pymorphy3 уже в requirements.txt (используется в `name_matcher.py`)
- В `_keyword_match`: токенизируем text, лемматизируем каждый токен, сравниваем леммы с лемматизированными keywords
- Плюс: **полностью** покрывает все падежи и склонения, единообразно с name_matcher
- Минус: +30-50 строк кода в `rbc.py`; ~5-10ms overhead на статью (30 статей × 10ms = 0.3 сек на cycle — приемлемо)
- Тест: добавить declension-asserts в `test_keyword_match_cyrillic_surname_with_declension`

**B) Stem-prefix tolerance (компромисс)**
- В regex: вместо `kw + boundary_end` использовать `kw + r"[а-яё]{0,6}" + boundary_end` для cyrillic-токенов
- Плюс: 3 строки кода, ноль overhead
- Минус: будут ложно-положительные ("Пятёрочка" хитит "Пятёрочкин-как-нибудь" фантастический вариант); не справится с супплетивными формами

**C) Принять текущую ситуацию для MVP, фикс позже отдельной спекой**
- Плюс: ship now
- Минус: режет fetch yield на ~60-75% реальных статей про X5

**Recommendation: A** — у нас уже есть pymorphy3, добавление лемматизации естественно и закрывает проблему чисто. Это и есть «правильный» вариант, который изначально предполагался спекой.


ответ: делаем вариант A
---

## P2 — Important

### 2. `xml.etree.ElementTree` уязвим к XXE/billion-laughs

**Файл:** `src/sources/rbc.py:36, 156`

Используем `import xml.etree.ElementTree as ET` + `ET.fromstring(xml_text)`. Это стандартная библиотека, но она **не защищена** от XML-атак (XXE, entity expansion, billion-laughs).

RBC RSS — доверенный источник, но:
- HTTPS не защищает от компрометации самой rbc.ru (низкая вероятность, но не нулевая);
- defense-in-depth — низкая стоимость защиты;
- `defusedxml` — стандарт безопасности для XML в Python (рекомендация PSF).

**Fix:** добавить `defusedxml>=0.7` в `requirements.txt`, заменить:
```python
import defusedxml.ElementTree as ET
```
Остальной код не меняется — defusedxml — drop-in замена для `fromstring`/`parse`.

### 3. Тест-имя вводит в заблуждение

**Файл:** `tests/test_rbc_parser.py:118-128`

Тест называется `test_keyword_match_cyrillic_surname_with_declension`, но **declensions не тестирует**. Реально проверяется только nominative ("Шехтерман") и derivative-форма ("шехтермановский" — adjective). Это **скрывает баг #1**.

**Fix (зависит от решения #1):**
- Если выбран фикс #1 (вариант A или B) — добавить ассерты на склонения, должны зеленеть после фикса.
- Если выбран отказ (#1 вариант C) — переименовать тест в `test_keyword_match_cyrillic_token_boundary` и оставить как есть.

---

## P3 — Informational

### 4. `init-db` не синхронизирует `sources.enabled` с конфигом

**Эмпирически наблюдалось в T6.5:** В `data/db.sqlite` поле `sources.rbc.enabled = 0`, хотя в `config.yaml` стоит `enabled: true`. Причина: `db.init_db` инсертит источник только если его нет; при повторном запуске не обновляет.

**Влияние:** **нулевое** — `fetcher.py:51` проверяет `src_cfg.enabled` из YAML, не из БД. Поле `sources.enabled` в БД сейчас наследие.

**Опции:**
- Убрать колонку `enabled` из `sources` (БД-схема + миграция)
- Сделать `init-db` upsert
- Оставить как есть, оформить как тикет на cleanup

### 5. CRLF/LF — нет `.gitattributes`

`git status` выдаёт `LF will be replaced by CRLF` при изменении файлов на Windows. Без `.gitattributes` Git хранит то, что отдал OS, ведя к diff-шуму на cross-OS работе.

**Fix:** добавить `.gitattributes` с `* text=auto eol=lf` (или `eol=crlf` если предпочтение).

### 6. Нет теста на пустые keywords

`RBCSource.fetch` логирует warning при пустых keywords.strong и возвращает 0 items. Поведение определено, но тестом не покрыто.

**Fix:** добавить `test_rbc_fetch_warns_when_strong_keywords_empty` (~10 строк).

---

## Что сделано правильно (worth calling out)

- **Strong/weak split** в `FetchContext.load_keywords()` — принципиальное решение по варианту B из codex-критики. Закрывает омонимию фамилий чисто и без ad-hoc эвристик.
- **Token boundaries** для латиницы — `\b` работает корректно, тесты подтверждают что `OX50` не хитит `X5`.
- **SSRF surface = 0** — один endpoint, `follow_redirects=False`, нет per-article fetch'ей.
- **Structured summary logging** — `{fetched, kept, keyword_rejects, weak_only_rejects, older_than_since, malformed}` — наблюдаемость filter quality из логов.
- **Backward-compat для Source ABC** — `context: FetchContext | None = None`, `x5_ir` не сломан, все 33 старых теста зелёные.
- **Recon-артефакт** `tests/fixtures/RBC_RECON.md` — спас 5+ дней работы (Qrator detection, pivot на RSS) и зафиксирован в репо.
- **Test coverage** rbc.py — **95%** (115 stmts, 6 missed — это tenacity-edge paths).
- **mypy + ruff clean** на новых файлах.

---

## Adversarial pass (само-проверка)

- ❓ XML bomb через rss endpoint — теоретически возможно при компрометации rbc.ru → **P2 #2 закрывает**
- ❓ Регресс x5_ir при добавлении `context` параметра — нет, все старые тесты зелёные
- ❓ Утечка cookies между sources — нет, отдельный `httpx.Client` per Source instance
- ❓ Race condition на shared state — нет, single-threaded fetch
- ❓ SQL injection в `FetchContext.load_keywords` — нет, параметризованные queries (`WHERE company_id = ?`)
- ❓ Что если `seed_persons` CSV содержит вредоносный SQL в `full_name`? — нет, всё через параметры; max что можно — записать "DROP TABLE" как литеральную строку
- ❓ Tenacity не делает retry на 4xx (404) — корректно (4xx — не transient)
- ❓ Если cycle бежит дважды одновременно — UNIQUE constraint спасёт от duplicate insert
- ❓ Если RSS feed внезапно возвращает 5 items вместо 30 — pipeline отрабатывает, просто меньше kept

---

## Решение

GATE: **FAIL** — нужно решение по #1 до merge.

Остальные находки (P2-P3) — auto-fix или defer по твоему выбору.
