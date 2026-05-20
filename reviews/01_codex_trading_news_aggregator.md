# Review 01 (Codex) — Pre-Landing Review агрегатора торговых новостей

Дата: 2026-05-20
Ветка: `site_news` (коммит `1004065`)
Модель: Codex CLI 0.130.0, `model_reasoning_effort="high"`
База сравнения: `master` (полный diff проекта)
Связанная спека: `specs/01_trading_news_aggregator.md`
Связанный план: `plans/01_trading_news_aggregator.md`
Связанная оценка: `estimates/01_trading_news_aggregator.md`
Параллельное review (Claude): `reviews/01_claude_trading_news_aggregator.md`

Итог: **2 issues (1 critical, 1 informational). GATE: FAIL.**

---

## 🚨 [P1] Глобальные OpenAI API errors poisonят всю очередь

**Файл:** `src/analyzer.py:190-191`

При `APIError` вне retried классов (`AuthenticationError`, `NotFoundError` для
модели, `BadRequestError`, 5xx через `APIStatusError`) текущая строка ставится
в `status='error'`, а `analyze_all` продолжает по очереди. После исправления
ключа / модели / сервиса эти строки исключаются из `_select_pending` (WHERE
status='new'). Одна глобальная ошибка конфига = **вся очередь permanently
заблокирована**.

**Сценарий:**

1. Юзер вписывает в `config.yaml` `llm_model: gpt-5-min` (опечатка).
2. `cycle` фетчит 50 новостей → `status='new'`.
3. На первой попытке analyze получает `NotFoundError`.
4. analyzer помечает строку 1 как `status='error'`, идёт дальше.
5. Получает ту же ошибку на строках 2–50 → все 50 в `status='error'`.
6. Юзер замечает опечатку, исправляет.
7. Повторный `cycle` → `_select_pending` не подбирает эти 50, они застряли.

**Fix:** для глобальных config-ошибок (auth, not-found, bad-request) — abort
всего батча без изменения статуса строк. Для 5xx — оставить `status='new'`
или добавить в список ретраябельных.

---

## 🟡 [P2] Short-body ветка не матчит persons по body

**Файл:** `src/analyzer.py:145`

Когда `body < MIN_BODY_CHARS`, LLM пропускается, но person extraction должен
использовать весь доступный текст. Short-body ветка матчит только headline:

```python
matches = matcher.match(headline)
```

Normal LLM ветка матчит `headline + body`:

```python
matches = matcher.match(f"{headline}\n{body}")
```

**Inconsistency.** Короткий релиз, где аффилированное лицо упомянуто только в
теле, помечается `analyzed` без `news_persons` линка.

**Fix:** использовать ту же комбинированную строку в обеих ветках:

```python
text = f"{headline}\n{body}"
matches = matcher.match(text)
```

---

## Cross-model analysis

| Источник | Findings |
| --- | --- |
| **Только Claude нашёл** | 10 правок (timeout, commit-per-source, API key validation, HTTP retry, short-body skip, conn pooling, rmtree, MAX_PAGES, meta regex, error_msg truncate) |
| **Только Codex нашёл** | 2 правки (P1 APIError poisoning, P2 short-body persons match) |
| **Оба** | 0 |
| **Agreement rate** | **0%** |

Полное расхождение. Codex и Claude смотрели на разные углы:

- **Claude** — превентивные проблемы (что может пойти не так в проде).
- **Codex** — логическая корректность только что внесённых изменений.

Каждая модель закрывает слепое пятно другой. Имеет смысл **запускать обоих
последовательно**: сначала Claude (`/review`) на общую гигиену, затем
Codex (`/codex review`) для проверки только что внесённых правок.

### Что я (Claude) пропустил и почему

- **P1.** Я разделил «терминальные» vs «ретраябельные» ошибки, но всё
  `APIError` свалил в «терминальные на уровне строки». На самом деле часть
  этих ошибок — глобальные (auth, model-not-found, bad-request), и они
  должны прерывать пайплайн, а не помечать конкретные строки.
- **P2.** Это баг, который я сам внёс правкой I1 в Claude review. Я добавил
  skip-LLM ветку для короткого body, но забыл использовать body при матчинге
  persons. Codex проверил консистентность с normal-ветвью и заметил.

## Статус

**DONE** — обе правки применены, 33/33 тестов проходит, end-to-end `cycle`
отработал без ошибок.

### Применённые изменения

| # | Файл | Изменение |
|---|------|-----------|
| P1 | `src/analyzer.py` | Различили global-config errors (`AuthenticationError`, `PermissionDeniedError`, `NotFoundError`, `BadRequestError`) → raise `_GlobalConfigError` → `analyze_all.break` без изменения статуса строк. Также добавили `InternalServerError` (5xx) в retried, transient errors теперь оставляют `status='new'` вместо `'error'`. |
| P1 | `src/cli.py` | При `r.aborted=True` cmd_analyze возвращает exit code 3 + stderr-сообщение о необходимости исправить конфиг. |
| P1 | `tests/test_analyzer.py` | Переписан `test_three_rate_limits_keeps_row_new` (transient теперь не терминальный) + новый `test_auth_error_aborts_batch_without_poisoning_rows`. |
| P2 | `src/analyzer.py:145` | Short-body ветка теперь матчит persons по `f"{headline}\n{body}"` (как LLM-ветка). |
| P2 | `tests/test_analyzer.py` | Новый тест `test_short_body_matches_persons_against_body` проверяет матчинг персоны, упомянутой только в body. |

### Изменение семантики transient errors (побочный эффект P1)

До: после 3 неудач сети — `status='error'`, строка навсегда выпадает.
После: после 3 неудач — `status='new'`, `retry_count=3`. Цикл-уровень skip
держит её до тех пор, пока юзер вручную не сделает
`UPDATE news SET retry_count=0 WHERE id=...` (или мы не добавим CLI команду
для этого). **Это правильнее** — сетевая флуктуация не должна терять данные.