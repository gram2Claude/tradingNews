# e-disclosure.ru Recon (T7.1)

Дата: 2026-05-21
Метод: curl с реалистичным User-Agent
Связанный план: `work_directory/02_plans/03_*_plan.md` (создаётся после APPROVED)

---

## Вердикт

**`www.e-disclosure.ru` закрыт agressive anti-bot защитой `servicepipe.ru`.**
**Через httpx без headless browser — не работает.**

Архитектура источника: требует **Playwright** (или эквивалент, выполняющий JS).
См. spec/03 для решения по подходу.

---

## 1. Anti-bot защита — servicepipe.ru

GET `https://www.e-disclosure.ru/` с реалистичным User-Agent Chrome/131:

```
HTTP 200
Content-Length: 1703
```

Тело — challenge-страница со spinner'ом, выполняющая JS:

```html
<!DOCTYPE html>
<html>
<head>
  <meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
  <noscript><meta http-equiv="refresh" content="0; url=/exhkqyad"></noscript>
</head>
<body>
  <div id="id_spinner" class="container">
    <div class="load"></div>
    <div class="spinner"></div>
  </div>
  <div id="id_captcha_frame_div" style="display: none;height: 100vh;"></div>
  <script type="text/javascript" src="https://servicepipe.ru/static/jsrsasign-all-min.js"></script>
  <script type="text/javascript">
  function get_cookie_spsn() { return "spsn=1779374724702_"; }
  function get_cookie_spid() { return "spid=..."; }
  function get_options() { return JSON.parse('{"kncdipiytd":"...","iamehkqlib":"...",...}'); }
  ...
  </script>
</body>
```

Что делает JS:
1. Подгружает `jsrsasign-all-min.js` (RSA crypto lib) с `servicepipe.ru`
2. Решает crypto challenge с параметрами из `get_options()`
3. Ставит cookies `spsn` и `spid`
4. Редиректит/обновляет страницу с этими cookies

Без выполнения JS:
- `<noscript><meta refresh url=/exhkqyad>` → редирект на другой challenge endpoint
- httpx не подгрузит `jsrsasign-all-min.js`, не решит challenge, не получит cookies
- Все последующие запросы возвращают **HTTP 403 «Forbidden»** с copy-button:
  ```
  Datetime: 2026-05-21 14:45:44 +0000
  IP: 95.216.44.30
  ID: ijdWNf7BlmI1
  If you are not a bot, please copy the report and send it to our support team.
  ```

**servicepipe.ru** — это российский WAF/bot-mitigation сервис (аналог Cloudflare Bot Management).
Активная защита: после 3-5 «подозрительных» запросов от одного IP — блокировка на 15-60 минут.

### Markers для challenge detection (для будущих источников)

- HTTP status: `200` на challenge-странице, `403` после throttle
- HTML содержит: `<noscript><meta refresh url=/exhkqyad>` или `servicepipe.ru` references
- Cookies: `spsn`, `spid`
- Body size: ровно `1703 байт` на challenge-странице, `1294 байт` на 403-странице (детерминированные шаблоны)

---

## 2. Endpoints — все закрыты после первых проб

| URL | Initial response | После throttle |
|---|---|---|
| `https://www.e-disclosure.ru/` | 200 (challenge page, 1703B) | 403 |
| `https://www.e-disclosure.ru/Company/Search?query=X5` | 200 (challenge page) | 403 |
| `https://www.e-disclosure.ru/sitemap.xml` | 200 (первая проба) | 403 |
| `https://www.e-disclosure.ru/sitemap_index.xml` | 200 (первая проба) | 403 |
| `https://www.e-disclosure.ru/api/` | 200 (первая проба) | 403 |
| `https://www.e-disclosure.ru/api/events` | 403 |  |
| `https://www.e-disclosure.ru/api/issuers` | 403 |  |
| `https://www.e-disclosure.ru/portal/files.aspx?id=X5` | 403 |  |
| `https://www.e-disclosure.ru/portal/company.aspx?id=1380` | 403 |  |
| `https://www.e-disclosure.ru/portal/event.aspx?EventId=...` | 403 |  |
| `https://www.e-disclosure.ru/portal/lentanews.aspx` | 403 |  |
| `https://www.e-disclosure.ru/LatestEvents.aspx` | 403 |  |
| `https://www.e-disclosure.ru/poslednie-novosti` | 403 |  |
| `https://www.e-disclosure.ru/news` | 403 |  |
| `https://www.e-disclosure.ru/rss` | 403 |  |
| `https://www.e-disclosure.ru/feed` | 403 |  |
| `https://www.e-disclosure.ru/portal/rss` | 403 |  |

**robots.txt доступен** (HTTP 200) и раскрывает:
```
User-Agent: *
Disallow: /api/*
Disallow: /Event/Certificate?*
Disallow: /Company/Certificate/*
Disallow: /PortalImageHandler.ashx?*
Disallow: /Company/Search?*
```

Значит у сайта есть:
- `/api/*` endpoints (вероятно — для JS-фронтенда сайта, тот же servicepipe защищён)
- `/Event/Certificate?id=...` — сертификаты раскрытий
- `/Company/Certificate/*` — сертификаты компаний
- `/Company/Search?*` — поиск компаний

Но всё это **за servicepipe**.

---

## 3. Альтернативы без Playwright

Проверено / известно:
- **Sitemap.xml** — за тем же servicepipe, недоступен после throttle.
- **/api/** — за тем же servicepipe.
- **Публичный RSS** — нет.
- **CSV/XML dumps** — нет.

Что **не проверено** но потенциально доступно:
- **API ЦБ РФ** (`cbr.ru/dataservice`) — может содержать обязательные раскрытия;
  скорее всего без anti-bot. Но это **reference data**, не лента событий.
- **disclosure.skrin.ru** — второй авторизованный диссеминатор; возможно, у них другая защита.
- **Yandex News / Google News** через поиск `site:e-disclosure.ru` — поток через агрегаторы.

---

## 4. Решение

Через httpx — **невозможно** для регулярного fetch.
Через Playwright — **должно работать**:

1. Playwright запускает реальный браузер (Chromium/Firefox)
2. JS-движок выполняет `jsrsasign-all-min.js`, решает challenge, ставит cookies
3. После `page.goto()` и `page.wait_for_load_state('networkidle')` мы имеем рабочую сессию
4. `page.content()` отдаст реальный HTML листинга
5. Каждая «реальная» статья — отдельный `page.goto()` + `page.content()`

**Цена:**
- ~200 MB Chromium binary
- В 5-10× медленнее чем httpx (300-500ms на page.goto vs 50-100ms httpx)
- Cross-OS issues (особенно Windows — нужно `playwright install chromium`)
- Headless False для дебага vs True для production
- Cookies persistence между запросами (один browser context на цикл)

**Принципиальные паттерны для Source ABC:**

- Новый базовый класс `PlaywrightSource(Source)` который держит browser+context lifecycle
- Конкретные источники наследуются от него: `EDisclosureSource(PlaywrightSource)`, в будущем `RBCFullSource(PlaywrightSource)` для www.rbc.ru
- В `__enter__` запускаем browser, в `__exit__` корректно закрываем
- Warmup: первый `goto` на главную (получаем servicepipe cookies), потом конкретные URL

---

## 5. Acceptance T7.1 (recon)

- ✅ Подтверждено: e-disclosure под servicepipe.ru
- ✅ Подтверждено: httpx-based fetch невозможен
- ✅ Подтверждено: robots.txt раскрывает `/api/`, но он за тем же WAF
- ✅ Подтверждено: Playwright — единственный реалистичный путь для регулярного fetch

---

## 6. T7.2 — живой Playwright recon (2026-05-21, ПРОЙДЕН)

### Результаты матрицы (4 режима default Playwright)

Все **FAIL**:
| Mode | Failure |
|---|---|
| chromium headless | `page.goto` timeout 30000ms на `wait_until="networkidle"` — challenge JS зацикливается |
| chromium headed | то же |
| firefox headless | warmup_ms=1463, **html=challenge page** (servicepipe не пройден) |
| firefox headed | то же |

### `playwright-stealth` + chromium headless — PASS (с правильным `wait_until`)

Установлено: `pip install playwright-stealth==2.0.3`. Обёрткой `Stealth().use_sync(sync_playwright())`.

**Критическое открытие:** `wait_until="networkidle"` не работает на e-disclosure из-за долгих XHR'ов (Яндекс.Метрика, реклама). Replace с `wait_until="domcontentloaded"` + явный `time.sleep(5-8)` — работает.

После warmup:
- `warmup_ms`: ~4000 (chromium+stealth, domcontentloaded + 8s sleep)
- Cookies: `.AspNetCore.Antiforgery.*`, `PVID`, `VID`, `_ym_*`, `adtech_uid`, `domain_sid` — **servicepipe пропустил, cookies его НЕ установлены** (servicepipe не сработал на главной)
- HTML: 117KB, title «Интерфакс – Сервер раскрытия информации» (реальная страница)
- **На последующих page.goto** (через ~5 мин подходов подряд) servicepipe **может включиться** и блокировать — IP throttle. Production 4-часовая cadence далеко за throttle window.

### Альтернативные подходы (deferred)

- **`Stealth + chromium headed`** — не проверено детально (раз stealth headless работает); deferred как fallback
- **Real Chrome user profile** (`launch_persistent_context`) — deferred, использовать если servicepipe адаптируется к stealth
- **Firefox + stealth** — не работает (firefox держится за challenge page)

**Production mode:** `chromium headless + playwright-stealth + wait_until="domcontentloaded" + manual sleep`.

### X5 emitter — найден

- **Internal ID:** `39008`
- **Полное юр. имя:** «ПАО Корпоративный центр ИКС 5»
- **ИНН:** `7726030449`
- **ОГРН:** `1027739216757`
- Company page URL: `https://www.e-disclosure.ru/portal/company.aspx?id=39008`

(Также найден id `9483` = ООО «ИКС 5 ФИНАНС» — финансовая дочка для облигаций, out of scope.)

### Search workflow — НЕ через GET URL, через form submit

URL `/Company/Search?query=...` отдаёт **servicepipe challenge page** (servicepipe selectively включается на этих URL).

**Working search:**
1. Открыть главную, warmup
2. Заполнить `input[name="query"]` (видна на главной)
3. `page.keyboard.press("Enter")` — submit form
4. Form action: `POST /search/newsfeed`
5. Results render на той же странице (без redirect)
6. Извлечь `a[href*="/portal/company.aspx?id="]` — оттуда видим candidate companies

### Listing структура — company page IS the listing

На странице компании (`/portal/company.aspx?id={id}`) видны:
- **19 последних событий** через `a[href*="/portal/event.aspx?EventId=..."]`
- В верстке: `<дата> <дата+время> <тип события>` рядом со ссылкой
- Например:
  - `13.03.2026 14:52 Проведение заседания совета директоров`
  - `19.05.2026 10:00 Дата определения лиц, имеющих право на осуществление прав по ценным бумагам`
  - `23.01.2026 26.01.2026 11:32 Выплаченные доходы`
- Дополнительно — 7 категорий через `files.aspx?id={id}&type=1..7` (специализированные разрезы; для MVP не используем)

### EventId — НЕ digit-only, base64-ish

Примеры реальных EventId:
- `YDJnAYBvL0udnyKj1zuduA-B-B`
- `B-Awjdof2lUu9FhT5NFO5Iw-B-B`
- `Tnsp9s43JEaegsCU9ppNfA-B-B`

Это **opaque строки** (~24 char alphanumeric с `-`). URL-pattern: `/portal/event.aspx?EventId=<ID>`.

**Важно для плана v3:** `EventId` хранится как часть URL в `news.url` — отдельной колонки `event_id` не нужно.

### Pagination — для X5 в текущем периоде не требуется

На company page видно 19 events. Самое старое из видимых — `23.01.2026`. Это покрывает 4 месяца назад — больше чем backfill с 2026-05-01 (3 недели).

Pagination markers (`page`, `paging`) встречаются в HTML, но **`?page=N` URL pattern не найден** — пагинация скорее всего JS-based / lazy load. Для MVP с backfill с 2026-05-01 это **не критично** — 19 свежих events покрывают наш период с запасом.

**Strategy:** если кому-то понадобится глубокий backfill — отдельная задача с pagination implementation (потенциально через scroll trigger).

### Event page структура

- URL: `/portal/event.aspx?EventId=<ID>`
- Size: ~74KB
- `<h2>`: «ПАО Корпоративный центр ИКС 5»
- Даты: формат `dd.mm.yyyy` (Moscow time, без явного TZ — assumed Europe/Moscow)
- Body содержит полный текст раскрытия + ссылки на PDF (attachments игнорируем по spec)

### Fixtures сохранены

- `tests/fixtures/edisclosure_listing.html` — company page X5 (89KB)
- `tests/fixtures/edisclosure_event.html` — одно событие (74KB)
- `tests/fixtures/ed-company-9483.html` — для сравнения (ООО ИКС 5 Финанс)
- `tests/fixtures/ed-company-39008.html` — дубликат listing для отдельного теста
- `tests/fixtures/edisclosure_listing_type1.html` — type=1 категория (для будущего использования)
- `tests/fixtures/PROBE_RESULTS.md` — лог матрицы

### Acceptance T7.2 — ✅ ПРОЙДЕН

- ✅ Playwright + Chromium + Firefox установлены на Windows
- ✅ Production mode определён: `chromium headless + playwright-stealth + domcontentloaded`
- ✅ X5 ID найден: `39008`
- ✅ ИНН + ОГРН зафиксированы для верификации
- ✅ Listing URL pattern: `/portal/company.aspx?id={id}` (company page = primary listing)
- ✅ Event URL pattern: `/portal/event.aspx?EventId=<opaque>`
- ✅ Селекторы: `a[href*="/portal/event.aspx?EventId="]` для events; дата + тип события в context до 200 символов до/после ссылки
- ✅ Pagination не нужна для текущего scope (19 events @ company page покрывают backfill с 2026-05-01)
- ✅ HTML фикстуры сохранены
- ⚠️ **Servicepipe throttle**: после 5-10 быстрых подходов IP блокируется на 15-60 мин. В production 4-часовая cadence далеко за throttle, но **monitoring challenge solve time в логах обязателен** (P3.2).

### Что изменилось vs план v2

| План v2 предполагал | Реальность из T7.2 |
|---|---|
| `LISTING_PATH = "/portal/files.aspx?id={id}&page={page}"` | На самом деле: company.aspx?id={id} — listing на главной компании |
| `EVENT_PATH = "/portal/event.aspx?EventId={id}"` | Подтверждено |
| `_goto(url, wait="networkidle")` | **НЕ работает** — нужен `wait="domcontentloaded" + sleep` |
| `e_disclosure_id` валидация: digit-only | Подтверждено (X5 id=39008) |
| Pagination через `MAX_PAGES` loop | Для MVP не нужно — 19 events на company page достаточно |
| servicepipe cookies `spsn`+`spid` обязательны | Под stealth servicepipe **может не сработать** на главной — cookies другие (ASP.NET) |
| Default Playwright headless | **playwright-stealth обязателен** — без него ни один из 4 default режимов не пробивает |

Эти правки идут в план v3 перед стартом T7.3.
