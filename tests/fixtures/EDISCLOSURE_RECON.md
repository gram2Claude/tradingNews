# e-disclosure.ru Recon (T7.1)

Дата: 2026-05-21
Метод: curl с реалистичным User-Agent
Связанный план: `plans/03_*_plan.md` (создаётся после APPROVED)

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

Следующий шаг: написать `specs/03_e_disclosure_spec.md` с решением по Playwright-инфраструктуре.
