# 05 · supabase_sync · pre-ship review (Codex)

**Команда:** `codex review --commit 392a158`
**Model:** gpt-5.5 (medium reasoning), sandbox read-only
**Session:** `019e5591-0878-7701-ad1f-92ed211b69b9`
**Дата:** 2026-05-23
**Статус:** PASS со одной P2-находкой.

---

## Сводка

| Severity | Кол-во | Действие |
|---|---|---|
| P1 (блокер) | 0 | — |
| P2 (warning) | 1 | смотри ниже |
| P3 (info) | 0 | — |

Codex не нашёл блокеров. Единственная находка — **расхождение между документированным контрактом и фактическим поведением push'а**.

---

## P2.1 · Delete cloud-only rows during mirror push

**Где:** `src/cloud_sync/pusher.py:99-103` (т.е. `_push_inner`).

**Цитата (codex):**

> When a row is removed from SQLite or added/edited directly in Supabase, this push path only UPSERTs the rows currently selected from SQLite and never removes rows that exist only in Postgres. That means `cycle`/`sync-cloud` can leave stale companies/persons/news/news_persons in the supposed read-only mirror, contradicting the documented "next push overwrites UI edits" behavior; add scoped deletes for rows absent locally before/after the upserts, especially respecting `--company` filters.

**Анализ.** Контракт в README (`Облачное зеркало → Ограничения`):

> «Это one-way push, не two-way sync. Если редактируешь строку в Supabase UI,
> следующий push **затрёт** изменения.»

Это утверждение **верно для UPDATE-конфликта** (тот же primary key уже есть в SQLite — `ON CONFLICT ... DO UPDATE` перезатрёт). **Но неверно для двух кейсов:**

1. **Local DELETE → cloud stale.** Если пользователь руками удалил строку в SQLite (или `analyzer` пометил row как `status='error'` и потом этот row физически удалили), на Postgres-стороне строка останется. Mirror перестаёт быть зеркалом.

2. **Supabase-side INSERT не trumped.** Если в Supabase UI вставить новую строку (например, `INSERT INTO trading_news.news VALUES ('finam', 'https://x5.ru/manual', ...)`), и в SQLite такой URL'а нет — следующий push **не тронет** эту строку. README обещает что "затрёт" — а оно не затрёт.

**Severity P2 (не P1):** local pipeline продолжает работать корректно, SQLite остаётся source of truth, корректность local-side гарантирована. Но (а) дашборд показывает ложную картинку, (б) документация лжёт.

**Fix options:**

- **(A) Full mirror (рекомендуется codex'ом):** перед или после UPSERT'ов сделать `DELETE FROM trading_news.X WHERE (key) NOT IN (SELECT keys from local snapshot)`, scoped по `--company` если фильтр задан. Truly read-only mirror.
- **(B) Принять расхождение:** обновить README — убрать формулировку "затрёт" и явно сказать «push добавляет/обновляет, но не удаляет». Скоро, безопасно, но снижает доверие к "mirror" семантике.
- **(C) Гибрид:** сделать опцию `--prune` для `sync-cloud`, по умолчанию `off`. Тогда дельта-push быстрый и без удалений, а полный sync — по явному флагу.

**Рекомендация:** (A) если планируем юзать Supabase как канонический read-source для дашбордов / других tools; (B) если Supabase — просто backup snapshot. Решает пользователь — это вопрос продуктовой семантики, не баг кода.

---

## Что codex НЕ нашёл (vs Claude review)

Codex прошёлся одним проходом и взял самую крупную семантическую проблему. Claude-review нашёл больше мелочей (3×P2 + 4×P3), но среди них только P2.3 (узкий `except`) и P2.1 (CHECK-constraint asymmetry) — реально работающие.

**Пересечения:** ноль — codex и claude увидели разные углы. Это **хороший знак**: два прохода покрывают разные surface areas.

**Уникальный вклад codex'а:** именно contract-mismatch с README, который Claude-review не отметил (Claude акцентировался на implementation correctness, не на documentation truthfulness).

---

## Pre-ship gate

**PASS** — есть P2 для последующего фикса (на выбор: full prune или правка README). Не блокирует ship.

**Что взять в следующую итерацию:**
- Решить вопрос продукта: A / B / C из P2.1 fix options.
- Если A → добавить unit-тест `test_push_prunes_cloud_only_rows`.
- Если B → две строки правки в README + явный комментарий в `pusher.py` про "additive-only".

Полный raw-лог codex'а сохранён в `codex_05_raw.txt` (gitignored — артефакт инспекции, не репо-источник).
