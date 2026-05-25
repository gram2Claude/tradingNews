# Estimate 07 — Codex critique of plan v1

Plan: `02_plans/07_claude_lmsic_ideas_plan.md`
Reviewer: codex (codex-cli 0.130.0, reasoning effort=medium)
Дата: 2026-05-25
Branch: `lmsic_ideas`
Codex session: `019e5daa-22e7-7b82-b4c9-be0031bca160` (.context/codex-session-id)

---

## CODEX SAYS (consult — plan critique)

════════════════════════════════════════════════════════════

**[P1]** `_extract_fields` action regex is wrong for the actual fixture. It misses:
`Мы не рекомендуем приобретать…` for ММК and `рекомендацию и не советуем «покупать»` for Северсталь. Your own acceptance expects `sell`, but `r'рекомендац\w+\s+[«"]…'` will return `None`.

**[P1]** Target price regex misses actual fixture wording: `целевой цены в 400 руб.` for Россети. Need optional `в`: `целев\w+\s+цен\w+\s+(?:в\s+)?…`.

**[P1]** Plan assumes `CompanyCfg.brands`; it does not exist. Brands come only from `persons.brand` via `base.py:86`. Any test/context that expects config-level brands is wrong.

**[P1]** `config.example.yaml` is listed as an edit target, but it is not present in the repo. That phase will fail unless the plan first creates it intentionally or removes the step.

**[P1]** `since=2026-05-20 → filtered_date=1, filtered_issuer=9` contradicts the pseudocode. With date filter before issuer, all 10 fixture items are older than since, so `filtered_date=10`, `filtered_issuer=0`.

**[P2]** `_build_body(thesis, fields, body_text)` duplicates the thesis because `body_text` already starts with the thesis. Either strip the first paragraph from body or do not prepend `thesis`.

**[P2]** `_map_action('BUY') → 'buy'` test is bogus. `_ACTION_MAP` has only Russian keys. Either remove the test or add English aliases deliberately.

**[P2]** `_match_company(issuer)` only checks the card title. That is OK for X5 in the current snapshot, but the plan overstates alias/brand matching. Body aliases like `КЦ Икс 5` are irrelevant if issuer is `X5 Retail group`; future title variants can be missed.

**[P2]** Synthetic URL dedup by `(date, issuer)` is too weak for a source with no detail URL. The plan admits same-day same-issuer collisions, but the safer MVP is cheap: include thesis slug from the start.

**[P2]** Redirect handling is under-specified. `follow_redirects=False` plus "log warning, skip" is fine for 3xx, but `_http_get` also needs explicit status handling for 4xx/5xx and retries only for transient failures. Do not silently skip parse-breaking HTTP responses.

**[P2]** `_clean_body` is fragile unless it preserves `<br>` before calling text extraction. `selectolax.text()` can flatten separators depending on usage. Tests must assert paragraph breaks from the fixture, not just absence of Telegram footer.

**[P2]** The plan says source-level `_clean_text` "from memory `feedback_body_cleaning`". There is only `text_cleanup.clean_text:56`, and it does not normalize whitespace like the local `_clean_text` copies in existing sources. Specify the actual helper or implement local normalization.

**[P2]** No explicit guard that `LmsicSource` requires `FetchContext`. Without it, `_match_company` can throw an opaque `AttributeError` in tests or direct use. Follow Finam's pattern and raise a clear `ValueError`.

**[P2]** Live smoke acceptance `kept ≥ 0, errors=0` is toothless. `kept >= 0` always passes. Assert either successful HTTP parse stats or a concrete DB state when an X5 item is still present.

Tokens: 597,608

════════════════════════════════════════════════════════════

## Resolution (Claude's response — все P1+P2 ACCEPTED, план v2 написан в самом 02_plans/07_…_plan.md)

Все 5 P1 — реальные баги в pseudocode/acceptance, не cosmetic. Все 8 P2 принимаем без споров — usability/test hygiene.

### P1 fixes в план v2

| ID | Plan v2 fix |
|---|---|
| P1.1 (action regex) | T3: расширить regex и `_ACTION_MAP`. Принять формы: `не рекомендуем (приобретать\|покупать)` → `sell`; `не советуем «покупать»` → `sell`; основная — `рекомендац\w+ …` + универсальный negation-check **перед** позитивной формой. Тесты на ММК и Северсталь добавлены явно. |
| P1.2 (target regex) | T3: добавить опциональное `в\s+` между «целев… цен…» и числом. Тест на Россети 400 руб. |
| P1.3 (CompanyCfg.brands) | T2 + T7: brands берутся из `FetchContext.load_keywords().strong` (содержит aliases+brands из персон). План v1 неточно описал источник. Исправлено в T2 спецификации `_match_company` и в acceptance T6. |
| P1.4 (config.example.yaml не существует) | T1: убрать упоминание `config.example.yaml` — в репо его нет (есть только `config.yaml`). |
| P1.5 (бажный pseudocode acceptance) | T6 acceptance: пересчитан. `since=2026-05-20` → `filtered_date=10, filtered_issuer=0, kept=0`. `since=2026-05-01` → `filtered_date=0, filtered_issuer=9, kept=1`. |

### P2 fixes в план v2

| ID | Plan v2 fix |
|---|---|
| P2.1 (thesis duplication) | T4: `_build_body` НЕ prepend thesis — body уже начинается с thesis-строки. Просто `f"{header}\n\n{body}"`. |
| P2.2 (BUY action test) | T3: удалить английский тест. `_ACTION_MAP` исключительно русский. |
| P2.3 (issuer matching only title) | T2: документируем как known limitation MVP (см. recon §9). Future task — fallback на body-aliases если title не сходится. Не блокирует ship. |
| P2.4 (synthetic URL weak) | T5: добавляем slug-headline в URL **с самого старта** (не «if needed»). Формат: `#YYYYMMDD-<issuer-slug>-<headline-slug-first-3-words>`. |
| P2.5 (redirect/status handling) | T2: explicit handling — 3xx → log+skip; 4xx → terminal (raise), 5xx → tenacity retry. Наследуем паттерн из x5_ir._http_get. |
| P2.6 (selectolax `<br>` flatten) | T4: использовать `node.html` (innerHTML), regex `<br/?>` → `\n` ДО парсинга, потом `HTMLParser(...).text()`. Тест на сохранение abzac-границ. |
| P2.7 (`_clean_text` helper уточнить) | T4: source-level `_clean_text` локально в `src/sources/lmsic.py` (зеркалирует паттерн x5_ir/finam — `_clean_text` живёт в каждом source-модуле, см. CLAUDE.md «Body cleaning convention»). После этого можем дёрнуть `text_cleanup.clean_text` для downstream нормализации в reporter, но обязателен именно локальный source-helper. |
| P2.8 (FetchContext requirement guard) | T1: в `LmsicSource.__init__` проверка `if context is None: raise ValueError("LmsicSource requires FetchContext (need company aliases for matching)")`. |
| P2.9 (live smoke toothless) | T8: усилить acceptance — `kept ≥ 1 OR (kept == 0 AND log explicitly says "no fresh X5 ideas")`. Плюс post-cycle SQL check: `SELECT COUNT(*) FROM recommendations WHERE source_id=(SELECT id FROM sources WHERE code='lmsic')` > 0. |

### Verdict

Codex review — **ACCEPT ALL P1+P2**. Plan v2 пишется поверх v1.

### Synthesis recommendation

Recommendation: **Принять все 5 P1 как блокирующие правки плана и переписать pseudocode в T3+T5+T6** because the regex misses (P1.1, P1.2) и неверная dispatch-логика в T6 acceptance (P1.5) — это не cosmetic, а bugs которые произведут broken T7 tests; альтернатива «start T1 anyway, fix during impl» — хуже (потеря 1+ ч на отладку под фикстурами которые сами противоречат plan'у).

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run |
| Codex Review | `/codex consult` | Independent 2nd opinion | 1 | DONE_WITH_CONCERNS | 13 findings (5 P1, 8 P2), все ACCEPTED |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 0 | — | not run (Claude self-review on plan v1 — внутренний, в самом плане v2 будет inline) |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | n/a (CLI tool, нет UI) |
| DX Review | `/plan-devex-review` | DX gaps | 0 | — | n/a |

- **CODEX:** 5 P1 + 8 P2; все приняты, переходим к plan v2 с переписанным T3 (regex), T5 (URL), T6 (acceptance) + рядом мелких T1/T2/T4/T7/T8 правок
- **UNRESOLVED:** 0 (все findings → plan v2)
- **VERDICT:** plan v1 → plan v2 (REWRITE) → готов к Phase 4 после v2
