# RBC Recon (T6.1) — результаты разведки rbc.ru

Дата: 2026-05-21
Метод: curl с реалистичным User-Agent
Связанный план: `plans/02_claude_rbc_news_plan.md` (v3)

---

## Вердикт

**Основной сайт rbc.ru — НЕДОСТУПЕН для httpx-based парсера.**
**RSS-канал rssexport.rbc.ru — РАБОТАЕТ без защиты.**

Архитектура источника: **RSS-only, без backfill** (см. Variant A в обсуждении).

---

## 1. Главный сайт rbc.ru — Qrator JS challenge

GET `https://www.rbc.ru/` с реалистичным `User-Agent` Chrome/131:

```
HTTP 401
Content-Length: 265
```

Тело ответа — всегда одинаковая страница challenge:

```html
<!DOCTYPE html>
<html><head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
<link rel="icon" href="data:,"/>
<meta name="referrer" content="no-referrer" />
<script src="/__qrator/qauth.js" charset="utf-8"></script>
</head><body></body>
</html>
```

Cookies в response:
- `qrator_jsr` — подписанный JWT-like токен (Qrator session)

`qauth.js` — 227 KB обфусцированный JavaScript, выполняет вычисления и ставит cookie, без которой бэкенд не отдаёт реальный HTML.

**Подтверждение из независимого источника:** `enterno.io/en/check/rbc.ru` показывает Qrator в HTTP-заголовках.

**Следствие:** без выполнения JS (Playwright / реализация Qrator-алгоритма в Python) httpx-парсер физически не может получить страницу. Search endpoint `/search/?query=...` ведёт себя так же.

### Markers для challenge detection (для будущих источников / fallback)
- HTTP status: `401` (нестандартно — обычно бот-защита даёт 403)
- HTML содержит: `<script src="/__qrator/qauth.js">`
- Response cookies: `qrator_jsr`, `__qrator*`
- Body size: ровно 265 байт (детерминированный шаблон)

---

## 2. RSS endpoint — работает

GET `https://rssexport.rbc.ru/rbcnews/news/30/full.rss` (User-Agent `Mozilla/5.0`):

```
HTTP 200
Content-Length: ~230 KB
Content-Type: application/rss+xml
```

**Никакого Qrator, никаких cookies-челленджей.** Хост — отдельный поддомен `rssexport.rbc.ru`, защита туда не натянута.

### Структура RSS

```xml
<rss xmlns:rbc_news="https://www.rbc.ru" version="2.0">
  <channel>
    <title>www.rbc.ru</title>
    <item>
      <title><![CDATA[Заголовок новости]]></title>
      <link>https://www.rbc.ru/rbcfreenews/6a0e9b1c9a7947029a7986c3</link>
      <pubDate>Thu, 21 May 2026 08:43:57 +0300</pubDate>
      <description><![CDATA[Краткое описание]]></description>
      <category>Политика</category>
      <guid isPermaLink="false">rssexport.rbc.ru:politics:6a0e...</guid>
      <enclosure url="https://s0.rbk.ru/v6_top_pics/.../*.jpeg" type="image/jpeg" length="0"/>
      <rbc_news:time>08:43:57</rbc_news:time>
      <rbc_news:date>21.05.2026</rbc_news:date>
      <pdalink>https://www.rbc.ru/rbcfreenews/6a0e...</pdalink>
      <rbc_news:anons><![CDATA[Краткое описание (то же что description)]]></rbc_news:anons>
      <rbc_news:news_id>6a0e9b1c9a7947029a7986c3</rbc_news:news_id>
      <rbc_news:type>short_news</rbc_news:type>          <!-- или 'article' -->
      <rbc_news:newsDate_timestamp>1779342237</rbc_news:newsDate_timestamp>
      <rbc_news:newsModifDate>Thu, 21 May 2026 08:48:22 +0300</rbc_news:newsModifDate>
      <rbc_news:newsline>politics</rbc_news:newsline>     <!-- politics|business|economics|... -->
      <rbc_news:full-text><![CDATA[Полный текст статьи. Может быть многоабзацный.]]></rbc_news:full-text>
    </item>
    <!-- ... 30 items total -->
  </channel>
</rss>
```

### Поля, которые мы используем

| Наше поле | Источник в RSS |
|---|---|
| `url` | `<link>` |
| `headline` | `<title>` (CDATA) |
| `body` | `<rbc_news:full-text>` (CDATA) |
| `published_at` | `<pubDate>` (RFC 822 с offset +0300 Moscow) → парсим в UTC |

Дублирующие поля для верификации: `<rbc_news:newsDate_timestamp>` (Unix timestamp) — sanity-check pubDate.

### Ограничения

- **Только `/30/full.rss` работает.** Любое другое число (`/5`, `/50`, `/100`, `/500`) → 404 или 302 редирект на `/30/`.
- **Тематических подканалов нет.** `/30/business/`, `/30/economics/`, `/30/retail/` → 404.
- **Time window:** в текущей выборке 30 items покрывают **~7 часов** (08:43 — 01:41 = 7h 2m). RBC выпускает ~4-5 новостей в час.
- **Backfill через RSS невозможен.** Архив старых новостей не отдаётся.

### Cycle-стратегия

- Часовой cycle покроет с запасом (30 items / 4 в час = ~7.5 ч окно).
- Двухчасовой cycle тоже безопасен.
- При cycle раз в день — теряем 17+ часов новостей.
- Дедуп через `UNIQUE(source_id, url)` — повторно скачанные item'ы не дублируются в БД.

---

## 3. Host allow-list (для SSRF)

Хосты, к которым обращается источник:
- `rssexport.rbc.ru` — единственный endpoint, который мы запрашиваем

В RSS встречаются URL'ы:
- `<link>https://www.rbc.ru/...` — **не fetching'ем**, только пишем как `url` в БД
- `<enclosure url="https://s0.rbk.ru/...">` — изображения, **не используем**
- `<pdalink>https://www.rbc.ru/...` — pdа-версия, **не используем**

SSRF-проблема не актуальна, потому что **не делаем HTTP-запросов по URL'ам из RSS** — только парсим RSS XML. `redirect`-цепочки не возникает.

---

## 4. Что упрощается в архитектуре vs план v2

| Компонент v2 | Статус для v3 |
|---|---|
| `_warmup()` + cookies | ❌ не нужен |
| `_polite_sleep(3-7s)` | ⬇ снижаем до 0.5-1.5s (один HTTP-запрос на cycle) |
| `_looks_like_challenge()` | ❌ не нужен |
| `_TransientError` + retry/cooldown | ⬇ упрощаем до tenacity `wait_exponential` 3× attempts |
| `_keyword_match` (token boundaries) | ✅ нужен |
| `_parse_listing` + `_fetch_article` + `_parse_article` | ❌ всё это сворачивается в `_parse_rss` |
| `_MAX_REQUESTS = 50` | ❌ один запрос на cycle |
| Двухэтапная фильтрация | ❌ одноэтапная (на 30 items сразу) |
| SSRF allow-list + ручной follow_redirects | ⬇ только проверка хоста финального URL |
| Playwright (deferred) | ❌ полностью убран — RSS обходит Qrator |

---

## 5. Что в новом плане v3

Архитектура:

```
src/sources/rbc.py:
  class RBCSource(Source):
    RSS_URL = "https://rssexport.rbc.ru/rbcnews/news/30/full.rss"
    
    def fetch(self, since: datetime) -> list[RawItem]:
      keywords = self.context.load_keywords()
      response = self._client.get(RSS_URL, timeout=30)  # retry on transient
      items = list(_parse_rss(response.text))           # ~30 items
      results = []
      stats = {hits: 30, keyword_rejects: 0, kept: 0, older_than_since: 0}
      for item in items:
        if item.published_at < since:
          stats['older_than_since'] += 1; continue
        if not _keyword_match(f"{item.headline}\n{item.body}", keywords):
          stats['keyword_rejects'] += 1; continue
        results.append(item)
        stats['kept'] += 1
      logger.info("rbc fetch summary: %s", stats)
      return results
```

Парсинг RSS — стандартная библиотека `xml.etree.ElementTree`, без зависимостей.

---

## 6. Acceptance

**T6.1 = ПРОЙДЕН.**

Подтверждено:
- ✅ Главный сайт недоступен (Qrator)
- ✅ RSS работает и содержит full-text
- ✅ Жёсткое ограничение: 30 items, ~7 часов
- ✅ Backfill через RSS невозможен
- ✅ Решение: RSS-only, без backfill (Variant A одобрен пользователем)

Следующая фаза: **T6.2 — переписать план v3 под RSS-архитектуру.**
