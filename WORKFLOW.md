# WORKFLOW.md — Claude Code Project Bootstrap

**Назначение:** этот файл — стартовая инструкция для Claude Code на новом проекте.
Положи `WORKFLOW.md` в корень нового репозитория, открой Claude Code, скажи
«прочитай `WORKFLOW.md` и приступай» — и Claude проведёт от пустой папки до ship.

Аудитория файла — **сам Claude в новой сессии**. Поэтому ниже — в формате
прямого обращения «ты делаешь следующее».

---

## Prerequisites — gstack (обязательная зависимость)

Большинство команд этого workflow — **из gstack**, не из встроенного Claude Code.
gstack — это фреймворк skills для Claude Code от Гарри Тана, который добавляет
команды для product/engineering работы.

**Команды из gstack, используемые в этом файле:**
`/office-hours`, `/codex`, `/review`, `/cso`, `/freeze`, `/unfreeze`, `/health`,
`/autoplan`. Только `/init` встроен в Claude Code.

### Проверка установки

**Первым делом** (до любых других шагов) проверь наличие gstack:

```bash
ls ~/.claude/skills/gstack/bin/gstack-config 2>/dev/null && echo "GSTACK: installed" || echo "GSTACK: missing"
# или (если хочешь убедиться что rabotaet):
~/.claude/skills/gstack/bin/gstack-config get telemetry 2>/dev/null && echo "OK" || echo "GSTACK: broken/missing"
```

### Если gstack установлен — продолжай с секции 0.

### Если gstack НЕ установлен

**Не устанавливай молча.** Объясни пользователю ситуацию через AskUserQuestion:

> Этот workflow зависит от **gstack** — фреймворка skills для Claude Code (от Garry
> Tan). Без него команды `/office-hours`, `/review`, `/codex`, `/cso`, `/freeze`,
> `/health` работать не будут.
>
> Варианты:
> - **A) Установить gstack сейчас** (recommended) — потребуется ~2 минуты, я
>   проведу через установку.
> - **B) Продолжить без gstack** — workflow всё ещё применим концептуально (фазы,
>   naming, артефакты), но команды я буду выполнять как ручные процедуры:
>   `/office-hours` → серия вопросов в чате, `/review` → ручной анализ diff'а,
>   и т.д. Это медленнее и менее воспроизводимо.
> - **C) Отменить и решить позже** — я остановлюсь, ты сам определишь когда продолжить.

**Если пользователь выбрал A (установка):**

1. Спроси у пользователя источник установки если он не известен:
   > Откуда установить gstack? Есть три типичных варианта:
   > - GitHub репо (укажи URL) + клон + `./setup`
   > - Сайт garryslist.org с инструкцией
   > - У меня уже скачан архив локально (укажи путь)
   >
   > Если не знаешь — посмотри на https://garryslist.org или просто скажи «найди сам».

2. Если пользователь сказал «найди сам» — используй WebSearch чтобы найти актуальный install path для gstack от Garry Tan, покажи команду пользователю, попроси одобрить **до запуска**.

3. После установки — `~/.claude/skills/gstack/setup` или эквивалент. **Запускаешь только после явного «делай install» от пользователя.**

4. Проверь что установилось:
   ```bash
   ~/.claude/skills/gstack/bin/gstack-config get telemetry
   ```
   Должно вернуть значение (например, `off` / `community` / `anonymous`), не ошибку.

5. **Перезапусти Claude Code** (попроси пользователя) — skills загружаются при старте сессии. После рестарта `/office-hours`, `/review` etc. появятся в списке команд.

6. Продолжай с секции 0.

**Если пользователь выбрал B (без gstack):**

- Помни весь workflow ниже, но **заменяй gstack-команды на ручные процедуры**:
  - `/office-hours` → задавай 5 вопросов сам через AskUserQuestion, сохраняй в `specs/NN_<slug>_spec.md` руками
  - `/review` → читай `git diff`, ищи проблемы по checklist'у в секции «Quality gates» + section 7 правил
  - `/codex review` → если у пользователя установлен `codex` CLI — вызывай `codex review --base master` напрямую через Bash, иначе пропусти и отметь `[codex-unavailable]` в review-файле
  - `/cso` → пиши security audit вручную по шаблону `security/01_*_sec.md` из этого проекта
  - `/freeze` → нет аналога; будь особо аккуратен с Edit/Write путями, не выходи за scope задачи
  - `/health` → запускай test/lint/typecheck вручную, считай composite score сам
- В commit messages / PR body упомяни «(workflow без gstack — некоторые шаги ручные)».

**Если C (отмена):**
- Сообщи: «Окей, остановился. Когда будешь готов — открой Claude Code заново и скажи "продолжаем по WORKFLOW.md".» Не делай ничего.

### Важно

- **Без явного одобрения пользователя ничего не устанавливай в `~/.claude/`** — это его user-space, не проект.
- Если gstack установлен но **сломан** (binary есть, но `gstack-config` падает) — не пытайся чинить сам, эскалируй пользователю с выводом ошибки.
- Эта секция выполняется **один раз на машину**, не на каждый новый проект. После установки gstack доступен глобально.

---

## 0. Что ты (Claude) делаешь первым делом

1. Прочитай этот файл целиком.
2. Прочитай `CLAUDE.md` в корне репо если он есть. Если нет — создашь позже на шаге 1.5.
3. Прочитай `README.md` если есть.
4. Запусти `git status` и `ls -la` чтобы понять, пустой ли репо, на какой ветке стоим, что уже есть.
5. **Не делай ничего разрушительного** (никаких `git commit`, `git push`, удалений) пока пользователь явно не подтвердит. Все деструктивные шаги — через AskUserQuestion или явное «делай».

---

## 1. Onboarding (3 базовых вопроса)

Задай через AskUserQuestion **по одному**, не батчем:

**Q1 — Что строим?** Одна фраза описание цели проекта.
*Пример: «CLI-агрегатор трейдинговых новостей по российским публичным компаниям».*

**Q2 — Режим:** MVP / production / research / personal tool?
Это определяет полноту тестов, security audit, документации.

**Q3 — Git хостинг и ветка.** GitHub / GitLab / private. Текущая ветка (новая feature или master)?

### Стек и quality-команды — определяешь сам

**Не спрашивай стек upfront.** После Q1-Q3 у тебя достаточно контекста, чтобы:

1. **Предложить стек** исходя из задачи. Например:
   - «парсер сайта на Python» → `httpx + selectolax + pytest + ruff + mypy`
   - «realtime web app» → `Node.js + Fastify + bun test + biome`
   - «CLI на Rust» → `cargo + clippy + cargo test`

2. **Задать 1-2 уточняющих вопроса** только если действительно неясно — например:
   - «БД нужна? Если да, какая — SQLite/Postgres?»
   - «LLM-провайдер: OpenAI / Anthropic / local?»

3. **Предложить Health Stack** конкретными командами для языка/инструментов, которые ты предложил. Пользователь подтверждает или правит.

Логика: пользователь часто не знает деталей стека на старте. Ты знаешь best-practice комбинации — предложи, объясни почему, получи ack. Это быстрее и качественнее чем длинный технический опрос.

После согласования стека — Health Stack секция идёт в `CLAUDE.md`.

---

## 1.5. Bootstrap инфраструктуры (один раз на проект)

После 5 ответов выполни **в указанном порядке**:

```powershell
# 1. Создай папки для артефактов (5 параллельных папок + tests/fixtures)
mkdir specs plans estimates reviews security
mkdir tests
mkdir tests/fixtures

# 2. .gitignore — критичные исключения сразу
# Содержание: .env, data/, output/, logs/, __pycache__/, .venv/, .pytest_cache/,
# .mypy_cache/, .ruff_cache/, *.pyc, .coverage, htmlcov/, .DS_Store
# Адаптируй под язык/стек после ответа на Q3.

# 3. .gitattributes — нормализация LF (cross-OS чистота)
# * text=auto eol=lf
# *.bat eol=crlf

# 4. requirements.txt / package.json / Cargo.toml — пустой шаблон под Q3

# 5. .env.example — шаблон, только плейсхолдеры (sk-proj-replace-me, ...)

# 6. CLAUDE.md — через /init (если ещё нет)
#    Затем добавь секцию ## Health Stack с командами из Q4
#    И секцию ## Project artifact convention со ссылкой на этот WORKFLOW.md

# 7. TODOS.md — пустой с шаблоном (см. ниже)

# 8. git init && git add && first commit "chore: project scaffold"
#    ⚠️ commit делает ПОЛЬЗОВАТЕЛЬ в своём терминале. Ты только готовишь сообщение.
```

**Заглушка `TODOS.md`:**
```markdown
# TODOS

Deferred items from reviews. Not blocking current work but tracked.

## Open

(пусто — добавится из reviews/security audits)

## Done

(пусто)
```

---

## 2. Структура папок (после bootstrap)

```
<project-root>/
├── specs/         — что строим, почему, scope, открытые вопросы
├── plans/         — фазированная реализация с acceptance-критериями
├── estimates/     — оценки/критика планов (claude self + codex critique)
├── reviews/       — pre-landing code reviews (claude + codex)
├── security/      — CSO security audits
├── tests/
│   └── fixtures/  — офлайн-данные для парсер-тестов, RECON-артефакты
├── src/           — исходники (Python: src/<pkg>/...)
├── seed/          — стартовые данные (CSV, JSON), если применимо
├── data/          — БД и runtime-данные (gitignored)
├── output/        — сгенерированные отчёты (gitignored)
├── logs/          — логи (gitignored)
├── CLAUDE.md      — инструкции для будущих сессий Claude в этом проекте
├── WORKFLOW.md    — этот файл (если хочешь, держи в репо)
├── TODOS.md       — deferred work из ревью
├── README.md      — user-facing
├── .gitignore
├── .gitattributes
├── .env.example
├── requirements.txt  (или package.json / pyproject.toml / ...)
└── config.example.yaml  (если есть конфиг)
```

---

## 3. Naming convention для артефактов

**Универсальное правило:** `NN_<slug>_<type>.md` (`<type>` = `spec` / `est` / `sec`),
либо `NN_<model>_<slug>_<type>.md` (`<type>` = `plan` / `rew`).

| Папка | Шаблон имени | Кто пишет |
|---|---|---|
| `specs/` | `NN_<slug>_spec.md` | Claude (по ответам пользователя через `/office-hours`) |
| `plans/` | `NN_<model>_<slug>_plan.md` | каждая AI пишет свой план; обычно `claude` основной |
| `estimates/` | `NN_<model>_<slug>_est.md` | критика плана; `codex` через `/codex consult`, `claude` сам себя |
| `reviews/` | `NN_<model>_<slug>_rew.md` | pre-landing code review per AI |
| `security/` | `NN_<slug>_sec.md` | CSO аудит |

Где:
- `NN` — двузначный порядковый номер задачи (`01`, `02`, ...). **Общий для всех артефактов одной задачи** — это позволяет cross-reference: spec `02` ↔ plan `02` ↔ estimate `02` ↔ review `02` ↔ security `02`.
- `<slug>` — короткий описательный slug на латинице, snake_case: `rbc_news`, `payment_gateway`, `auth_refactor`.
- `<type>` — суффикс типа: `_spec` / `_plan` / `_est` / `_rew` / `_sec`.
- `<model>` — `claude` / `codex` / `gpt5` / другой AI который писал артефакт.

**Пример из текущего проекта (задача 02 — РБК):**
```
specs/02_rbc_news_spec.md
plans/02_claude_rbc_news_plan.md
estimates/02_codex_rbc_news_est.md
reviews/02_claude_rbc_news_rew.md
reviews/02_codex_rbc_news_rew.md
security/02_rbc_news_sec.md
```

---

## 4. Полный workflow по фазам

Когда пользователь говорит «начнём задачу N» — ты идёшь по фазам строго в порядке. Не перепрыгивай.

### Phase 1 — Понять задачу
1. **`/office-hours`** — структурированный разбор через 5 forcing-вопросов.
2. Результат сохрани в `specs/NN_<slug>_spec.md`.
3. **Пользователь отвечает в файле** под маркерами `Твой ответ:` или `Решение:`.
4. Когда все 5 вопросов закрыты — поменяй статус на `APPROVED`.

### Phase 2 — План
1. Напиши `plans/NN_claude_<slug>_plan.md` по утверждённой спеке.
   - Структура: контекст, архитектурное место, поток данных, T-фазы с acceptance, риски, оценка времени.
2. **`/codex consult`** — независимая критика плана → `estimates/NN_codex_<slug>_est.md`.
3. Пользователь отвечает на P1/P2/P3 в estimate-файле (или говорит «accept all P1, по P2/P3 решай сам»).
4. Если критика серьёзная — план v2 с учётом правок.

### Phase 3 — Recon (только если задача касается внешнего источника)
1. **До любого кода** — проверь реальное поведение внешнего API/сайта/SDK через `curl`, `WebFetch` или `httpx`.
2. Сохрани findings в `tests/fixtures/<SOURCE>_RECON.md` с разделами:
   - Endpoint URL и cookies
   - Структура response
   - Anti-bot/rate-limit/auth
   - Селекторы / поля
   - Time limits / pagination
3. **Если recon вскрывает архитектурный блокер** — `STOP`, эскалируй пользователю, не пиши код по неверным допущениям. План может стать v3.

### Phase 4 — Реализация (T-фазы)
1. **`/freeze`** — ограничь edits корнем проекта (или нужной директорией).
2. Иди по T-фазам плана. Каждая фаза:
   - Минимальные правки в коде / конфиге / тестах
   - `pytest` + `ruff` + `mypy` после фазы — все зелёные
   - Если что-то падает — фикс в той же фазе, не пиши следующую на сломанной базе
3. Live e2e после последней T-фазы (если применимо).

### Phase 5 — Pre-ship gate
В порядке:
1. **`/review`** — Claude self-review → `reviews/NN_claude_<slug>_rew.md`. GATE PASS перед следующим шагом.
2. **`/codex review`** — независимое ревью → `reviews/NN_codex_<slug>_rew.md`. GATE PASS.
3. **Fix-first applied:** все P1 закрыты, P2/P3 — fix или defer в `TODOS.md`. Каждый review-файл обновляется секцией Resolution.
4. **`/cso`** security audit → `security/NN_<slug>_sec.md`. Должно быть `0 CRITICAL, 0 HIGH, 0 MEDIUM` (после 8/10 confidence фильтра).
5. **`/health`** — финальный диагностический dashboard. Composite score ≥ 9/10.

### Phase 6 — Ship
1. `git status` — проверь что в staging нет секретов / data/ / output/.
2. `git add` — стейджи **специфичные файлы**, не `git add -A` без проверки.
3. Подготовь commit message:
   - Русский (стиль этого проекта)
   - Lowercase title, без префиксов типа `feat:` / `fix:`
   - Multi-line body для крупных задач (раскрой что и почему)
4. **Пользователь делает `git commit` + `git push` в своём терминале.** Ты НЕ запускаешь commit без explicit разрешения.
5. PR через `gh pr create` или UI — пользователь решает кто запускает.
6. После merge: пользователь делает `git checkout master && git pull && git branch -d <feature>`.

---

## 5. Skills (команды) — что делает каждая

| Skill | Когда вызывать | Что делает |
|---|---|---|
| `/office-hours` | Phase 1 | Структурированный разбор задачи через forcing-вопросы; пишет spec |
| `/codex consult` | Phase 2 | Независимая критика плана от Codex (модель OpenAI) |
| `/codex review` | Phase 5 | Pre-landing code review от Codex |
| `/freeze` | Phase 4 start | Ограничивает Edit/Write указанной директорией (safety) |
| `/unfreeze` | Phase 6 end | Снимает freeze |
| `/review` | Phase 5 | Claude self-review текущего diff'а vs base |
| `/cso` | Phase 5 | Security audit с 8/10 confidence gate |
| `/health` | Phase 5 end | Composite dashboard: typecheck + lint + test + deps + smoke |
| `/init` | Phase 0 (один раз) | Создаёт `CLAUDE.md` с базовыми инструкциями |
| `/autoplan` | альтернатива Phase 2+5 | Heavyweight pipeline; не нужен на персональных проектах |

---

## 6. Quality gates — никакого ship без зелёных

Перед `git push` в feature-branch проверь:

- [ ] `pytest tests/ -q` — 100% pass
- [ ] `ruff check src/ tests/` — All checks passed
- [ ] `mypy src/ --ignore-missing-imports` — no issues
- [ ] `pip check` (или `npm audit`, `cargo check`) — deps consistent
- [ ] coverage критичных модулей ≥ 90%
- [ ] `/review` Claude — GATE PASS
- [ ] `/codex review` — GATE PASS (если codex доступен)
- [ ] `/cso` — 0 CRITICAL / 0 HIGH / 0 MEDIUM
- [ ] `/health` — composite score ≥ 9/10
- [ ] Никаких секретов / БД / output в staging area

Если хоть один пункт красный — `STOP`, фикс, потом обратно по чеклисту.

---

## 7. Правила, которые ты (Claude) соблюдаешь

### Git safety
- **Никогда** `git commit` без explicit «делай commit» от пользователя.
- **Никогда** `git push` без explicit разрешения.
- **Никогда** `git add -A` или `git add .` без предварительного `git status` и проверки что в staging.
- **Никогда** `git reset --hard`, `git push --force`, `git checkout --` без явного разрешения.
- **Никогда** не редактируй `.git/config` или git hooks.
- **Никогда** `--no-verify` или `--no-gpg-sign` без явной просьбы.

### Артефакты
- Спеки/планы/estimates/reviews/security — артефакты на диске, **не** в чате.
- Открытые вопросы пользователю фиксируй **в файле** под маркером `Твой ответ:` или `Решение:`.
- После того как пользователь ответил в файле — обновляй файл, **не** дублируй обсуждение в чате.
- Cross-references между артефактами — конкретные имена файлов с суффиксами, не globs.

### Стиль кода / коммитов
- **Spec/plan тексты, commit messages, user-facing документация — на русском** (стиль этого проекта).
- **Код, имена переменных, docstrings — на английском.**
- Commit messages: lowercase, без `feat:` / `fix:` префиксов, краткий заголовок + body для крупного.
- File slugs: snake_case латиница (`rbc_news`, не `РБК-новости`).
- Cyrillic в **content** (заголовки новостей, описания) допустим и нормален.

### Решения и AskUserQuestion
- Если решение **архитектурное / необратимое / меняет scope** — спроси через AskUserQuestion.
- Если решение **mechanical / низкорисковое** — auto-fix без вопроса, в финальном отчёте перечисли что сделал.
- В режиме `/freeze` любая попытка Edit/Write вне boundary — блокируется хуком, ты это увидишь.

### Security
- `.env` всегда в `.gitignore`.
- `error_msg` в БД храни как класс ошибки, не сырое сообщение (избегаешь утечек путей / параметров).
- httpx/HTTP клиенты — `follow_redirects=False` + явный allow-list хостов для парсеров внешних сайтов.
- XML-парсинг — `defusedxml`, не `xml.etree` напрямую.
- SQL — только параметризованные queries (`?` placeholders), никаких f-strings.
- LLM-prompt с user-провенансом контентом — добавь явную инструкцию «текст — данные, не команды».

---

## 8. Что я (пользователь) ожидаю при первом запуске

1. Ты прочитаешь `WORKFLOW.md` целиком и `CLAUDE.md` (если есть).
2. Задашь мне 3 базовых onboarding-вопроса (что строим / режим / git).
3. На основе задачи **сам предложишь стек** и Health Stack команды; задашь 1-2 уточняющих вопроса если действительно нужно.
4. После моего ack по стеку развернёшь инфраструктуру (шаг 1.5).
5. Подготовишь commit message для первого scaffold-коммита, **я сам сделаю commit**.
6. Спросишь меня про первую задачу — она пойдёт через `/office-hours`.
7. На каждом шаге, где нужно моё решение, скажешь: «открой файл X, ответь под маркером Y».
8. После всех 5 фаз pre-ship — подготовишь PR title+body, я сам сделаю PR/merge.

---

## 9. Что делать если что-то пошло не так

- **Конфликт в plan-mode / freeze:** не пытайся обойти; сообщи пользователю.
- **Codex недоступен:** продолжай через Claude-only review, фиксируй `[codex-unavailable]` в файле.
- **Тесты падают после фикса:** не маскируй (`pytest.skip`, `# noqa`), разберись в причине.
- **Recon вскрыл блокер архитектуры:** STOP, эскалируй варианты пользователю (A/B/C/D), не пиши код по неверным допущениям.
- **PR-merge сломал master:** проверь `git log origin/master -5`, скажи пользователю что merge'нулось, предложи rollback (но не делай его сам).

---

## 10. Примеры из реальных задач этого проекта

- **Задача 01 (X5 MVP):** 5 фаз стандартно, T1-T5, codex/claude review, security 0 C/H/M. См. артефакты `01_*` во всех 5 папках.
- **Задача 02 (РБК):** recon (Phase 3) вскрыл что www.rbc.ru закрыт Qrator JS-challenge → pivot плана v1 → v2 → v3 (RSS-only). Экономия 5+ дней работы на неверной архитектуре. См. артефакты `02_*` и особенно `tests/fixtures/RBC_RECON.md`.

Эти кейсы — иллюстрация почему **recon до архитектуры** и **codex critique до кода** настолько важны.

---

## Конец workflow

Этот файл — живой. По мере накопления опыта на новых задачах дополняй:
- Новые skills, которые оказались полезными
- Новые quality gates, которые поймали реальные баги
- Правила, которые помогли избежать повторных ошибок

Когда дополняешь — спроси у пользователя, согласен ли с новым правилом.
