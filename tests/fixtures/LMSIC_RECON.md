# LMSIC Recon — `https://www.lmsic.com/analytics/ideas/`

Date: 2026-05-25
Author: Claude (Phase 3 для задачи 07 `lmsic_ideas`)
Fixture: `tests/fixtures/lmsic_listing_raw.html` (snapshot, 126 KB, UTF-8)

---

## Verdict

**Простой SSR-сайт, ноль anti-bot, всё в первичном HTML.** httpx + selectolax,
никакого Playwright. Per-idea URL'ов **нет** — синтетический URL по `(date, ticker)`.
Структурные поля (`target_price`, `recommendation_action`, multipliers) **в теле
текстом**, не в DOM — извлекаются regex'ом из body. Phase 4 unblocked.

---

## 1. Endpoint и доступ

| Поле | Значение |
|---|---|
| URL | `https://www.lmsic.com/analytics/ideas/` |
| Метод | GET |
| User-Agent | Любой адекватный браузерный UA проходит. Без UA не тестировал — добавим в код для надёжности |
| Cookies | Не требуется |
| Auth | Нет |
| Response status | 200 OK |
| Response size | ~126 KB на одну страницу |
| Encoding | `Content-Type: text/html; charset=UTF-8` — **честная UTF-8**, не cp1251. Кириллица читается без перекодировки. |
| Redirects | Нет (на `/` без `/` редиректит на `/`, но `/analytics/ideas/` — финальный URL) |

## 2. Anti-bot / WAF

- ✅ **Cloudflare:** нет
- ✅ **Qrator:** нет
- ✅ **Captcha:** нет
- ✅ **JS-обязателен:** нет (контент в первичном HTML; JS только для toggle-кнопок «Подробнее»)
- ✅ **Rate-limit:** не выявил (4-5 запросов в минуту прошли без 429)
- 💡 Yandex.Metrika подключена, но это analytics, не блокировка

## 3. Структура DOM (одного item'а)

```html
<li class="ideas-page__list-item">
    <div class="ideas-card" data-ideas>
        <span class="ideas-card__date">19.05.2026</span>
        <div class="ideas-card__title">X5 Retail group</div>
        <div class="ideas-card__preview-text" data-ideas-text>
            КЦ Икс 5: падение трафика во всех форматах сети
            <br><br>
            <полный текст с встроенной строкой:
             "Текущие мультипликаторы: EV/EBITDA = 3.31, P/E = 7.43, Net debt/EBITDA = 1.12.">
            <br><br>
            <блок про дивиденды / контекст>
            <br><br>
            <блок с финальной рекомендацией и target price:
             "Мы подтверждаем нашу рекомендацию «держать»",
             "имеют 14% потенциал роста до нашей целевой цены 2800 руб.">
            <br><br>
            Телеграмм-канал: https://t.me/lmsstock
        </div>
        <button class="ideas-card__more" data-ideas-more>Подробнее</button>
        <button class="ideas-card__less" data-ideas-less>...</button>
    </div>
</li>
```

**Селекторы для selectolax:**

| Поле | Селектор |
|---|---|
| Item container | `li.ideas-page__list-item` |
| Date | `span.ideas-card__date` (text, формат `DD.MM.YYYY`) |
| Issuer | `div.ideas-card__title` (text) |
| Body | `div.ideas-card__preview-text` (innerHTML или text) — **уже полный**, кнопка «Подробнее» это CSS-truncation, не fetch |

## 4. Pagination

В первичном HTML внизу списка:

```html
<div class="ideas-page__bottom" data-pagination>
    <button class="load_more" data-pagination-button
            data-url="/analytics/ideas/?PAGEN_1=2&ajax_call=Y">
        <span>Загрузить ещё</span>
    </button>
</div>
```

- Мощность одной страницы: **10 items**
- AJAX endpoint: `?PAGEN_1=N&ajax_call=Y` — возвращает partial HTML (фрагмент списка)
- **MVP (spec P4.A):** берём только page 1 — никакого AJAX, никакого PAGEN. X5 на топе (19.05.2026), поймаем без backfill'а.

## 5. Year filter

```
GET /analytics/ideas/?year=YYYY
```

- Годы: 2009–2026 (18 опций в dropdown)
- Поведение: тоже 10 items на страницу (paginates внутри года через `PAGEN_1`)
- 💡 Полезно для **будущего** `--backfill` режима (spec P4.C, out-of-scope MVP)

## 6. Per-idea URL — НЕТ

`<button>Подробнее</button>` — это JS-toggle для CSS-truncation, **не href**. Открывает
в-плейс полный текст из `data-ideas-text` (который и так в HTML).

**Это блокирует spec P5.B** (использовать настоящий URL детальной страницы) — fallback
на **P5.A: синтетический URL**.

**Предлагаемый формат синтетического URL** (для дедупа через `recommendations.(source_code, url)`):

```
https://www.lmsic.com/analytics/ideas/#YYYYMMDD-<slug>
```

где `<slug>` — transliterated issuer name + дисамбигуатор по headline-prefix:

```
https://www.lmsic.com/analytics/ideas/#20260519-x5-retail-group
https://www.lmsic.com/analytics/ideas/#20260507-fosagro
```

Если в один день две идеи про X5 (теоретически) — добавляем headline-slug:

```
https://www.lmsic.com/analytics/ideas/#20260519-x5-retail-group-padenie-trafika
```

Реализация: pyaslug + transliterate (наследуем стиль finam `_slugify`).

## 7. Извлечение структурных полей из body

`target_price`, `recommendation_action`, `potential_pct`, `multipliers` — **в тексте**,
не в DOM. Парсятся **в `LmsicSource._extract_fields`** regex'ами по проверенным паттернам.

**Паттерны на материале X5:**

```python
# Recommendation action — "Мы подтверждаем нашу рекомендацию «<X>»"
# где X ∈ {покупать, держать, продавать, не покупать}
re.search(r'рекомендац\w+\s*[«"]([^»"]+)[»"]', body)
# Маппинг:
#   покупать → buy
#   держать → hold
#   продавать → sell
#   не покупать → sell  (есть в идее ММК)

# Target price — "целевой цены <N> руб" / "целевую цену <N> руб"
re.search(r'целев\w+\s+цен\w+\s+(\d[\d\s.,]*)\s*руб', body)
# Пример: "целевой цены 2800 руб" → 2800.0

# Potential — "<N>% потенциал" / "<N>% дисконт"
re.search(r'(\d+(?:[.,]\d+)?)%\s+потенциал', body)
# Пример: "14% потенциал роста" → 14.0

# Multipliers — "EV/EBITDA = <N>, P/E = <N>, Net debt/ EBITDA = <N>"
# Формат строго одинаков ("Текущие мультипликаторы: EV/EBITDA = ...")
re.findall(r'(EV/EBITDA|P/E|Net debt/\s*EBITDA)\s*=\s*(-?\d+(?:\.\d+)?)', body)
# Пример → [('EV/EBITDA', '3.31'), ('P/E', '7.43'), ('Net debt/ EBITDA', '1.12')]
```

**Multipliers JSON:**

```json
{"ev_ebitda": 3.31, "p_e": 7.43, "nd_ebitda": 1.12}
```

**⚠ Когда чего-то нет в тексте** (см. ММК — нет target price, нет потенциала):
- `target_price = None`
- `potential_pct = None`
- `recommendation_action` всё равно может быть найдена («не рекомендуем покупать» → `sell`)
- `multipliers_json` может быть partial — `{"ev_ebitda": 2.97, "nd_ebitda": -0.99}` без P/E

**Тесты** должны покрыть: happy path (X5), no-target (ММК), no-multipliers (Сегежа), all-None edge case.

## 8. Что есть на первой странице (recon snapshot)

10 items, даты 19.05.2026 → 14.04.2026 (~5 недель окна):

| Date | Issuer | Has target? | Has multipliers? |
|---|---|---|---|
| 19.05.2026 | X5 Retail group | ✅ 2800 руб, 14% | ✅ EV/EBITDA, P/E, ND/E |
| 07.05.2026 | ФосАгро | ✅ 8000 руб, 19% | ✅ полные |
| 05.05.2026 | ММК | ❌ нет | ⚠ partial (нет P/E) |
| 04.05.2026 | Северсталь | ❌ нет | ✅ полные |
| 28.04.2026 | Новабев Групп | ✅ 450 руб, 24% | ✅ полные |
| 23.04.2026 | ЮМГ | ✅ 1000 руб, 19% | ✅ полные |
| 20.04.2026 | Сегежа | ❌ нет | ❌ нет |
| 16.04.2026 | Эл5-Энерго | ❌ нет | ✅ полные |
| 15.04.2026 | Циан | ✅ 750 руб, 24% | ✅ полные |
| 14.04.2026 | Россети Ленэнерго | ✅ 400 руб, 19% | ✅ полные |

**Один X5-item на странице.** Cadence идей про X5 — примерно 1 раз в месяц.

## 9. Фильтр по эмитенту (spec P2.A)

`div.ideas-card__title` — структурированное поле, парсится прямо. Для X5
aliases (case-insensitive substring match):

```python
X5_ALIASES = ["X5", "Икс 5", "ИКС 5", "КЦ Икс", "Пятёрочка", "Перекрёсток", "Чижик"]
# Сейчас в первичном HTML видим вариант "X5 Retail group" — совпадает по "X5"
```

**Источник aliases:** `CompanyCfg.aliases` (config.yaml — уже есть для finam).
**Brand list** (Пятёрочка/Перекрёсток/Чижик) — `CompanyCfg.brands`.

## 10. Body cleaning

Из preview-text приходит:
- `<br><br>` между параграфами (×4-5 в одной идее)
- лишние tab/whitespace в начале (HTML indentation)
- ссылка `https://t.me/lmsstock` в конце (один и тот же — Telegram автора)

**Обязательное в `_clean_text`** (наследуется из memory `feedback_body_cleaning`):

1. selectolax → extract text из `.ideas-card__preview-text`
2. `<br>` → `\n` (можно две подряд → одна пустая строка)
3. Заголовок-тезис (первая строка перед первым `<br><br>`) — отдельно как часть body или промптовый header
4. Удалить Telegram-footer (последнюю строку `Телеграмм-канал: ...`) — это footer, не часть идеи
5. Стандартный `_clean_text` (NBSP, control chars, etc.)

**Body format (по spec P9.B):**

```
Рекомендация: hold
Целевая цена: 2800 ₽ (+14%)
Мультипликаторы: EV/EBITDA=3.31 · P/E=7.43 · Net debt/EBITDA=1.12

КЦ Икс 5: падение трафика во всех форматах сети

Компания Корпоративный центр Икс 5 представила...
...
```

Header формируется **в парсере** из извлечённых структурных полей. Если поля
`None` — соответствующая строка header'а пропускается.

## 11. Headline

В DOM нет явного `<h2>` под headline-тезис — он внутри `data-ideas-text` как первая
строка перед первым `<br><br>`.

**Headline для БД** (`RawItem.headline`):

```python
# X5: "КЦ Икс 5: падение трафика во всех форматах сети"
# Берём как:
issuer = "X5 Retail group"        # из .ideas-card__title
thesis = body.split("<br>")[0].strip()  # первая строка из preview-text
headline = f"{issuer}: {thesis}" if thesis and thesis != issuer else issuer
# → "X5 Retail group: КЦ Икс 5: падение трафика во всех форматах сети"
```

Альтернативно — просто `thesis`. Решим в плане T-фаза 2 (парсер).

## 12. Published_at

`DD.MM.YYYY` → 23:59 Europe/Moscow (idea publishes к concу торгового дня) → UTC.

```python
from datetime import datetime, time
from zoneinfo import ZoneInfo
d = datetime.strptime("19.05.2026", "%d.%m.%Y").date()
moscow = datetime.combine(d, time(23, 59), tzinfo=ZoneInfo("Europe/Moscow"))
published_at = moscow.astimezone(ZoneInfo("UTC"))  # → 2026-05-19T20:59:00+00:00
```

Идея на день — точное время не нужно (lmsic не публикует HH:MM). Конец дня выбран чтобы
finam-news того же дня сортировались **перед** lmsic-recommendation того же дня (more
intuitive Obsidian view).

## 13. Открытые / решённые вопросы из spec 07

| Spec ID | Resolution |
|---|---|
| P1 | ⚠ Уточнено: `item_destination=ItemDestination.RECOMMENDATIONS`, не `item_type`. См. spec sync-note |
| P2 | ✅ A — match по `.ideas-card__title` через aliases |
| P3 | ✅ Структурные поля в отдельных колонках `recommendations` table (task 06 architecture) |
| P4 | ✅ A — только первая страница, никакого AJAX |
| P5 | ❌ B блокирован — нет per-idea URL. Fallback на A с синтетическим URL по `(date, slug-issuer)` |
| P6 | ✅ httpx + selectolax — никакого Playwright, anti-bot нет |
| P7 | ✅ Без `lmsic_*` в CompanyCfg, переиспользуем aliases |
| P8 | ✅ 4 часа, manual cycle |
| P9 | ✅ B — preformatted header + текст |

## 14. Архитектурные блокеры

**Нет.** Phase 2 (plan) можно писать.

## 15. Связанный код

| Файл | Изменение |
|---|---|
| `src/sources/lmsic.py` | **НОВЫЙ** — class `LmsicSource(Source)`, `item_destination=ItemDestination.RECOMMENDATIONS` |
| `src/fetcher.py` | +1 строка в `SOURCE_REGISTRY` |
| `config.yaml`, `config.example.yaml` | +`sources.lmsic`, +`companies[X5].sources.lmsic` |
| `tests/fixtures/lmsic_listing.html` | сохранить snapshot из recon (можно текущий `_raw.html` переименовать) |
| `tests/fixtures/_inspect_lmsic.py` | удалить (recon-only артефакт) |
| `tests/test_lmsic.py` | **НОВЫЙ** — фикстура + 8-10 тестов |

Аналайзер, name_matcher, reporter, cloud_sync, БД — **без изменений** (наследуют
recommendations-флоу из task 06).
