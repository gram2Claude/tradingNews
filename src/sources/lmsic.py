"""lmsic.com/analytics/ideas — трейдинговые рекомендации инвесткомпании ЛМС.

Recon: tests/fixtures/LMSIC_RECON.md
Spec: work_directory/01_specs/07_lmsic_ideas_spec.md
Plan: work_directory/02_plans/07_claude_lmsic_ideas_plan.md (v2)

Recommendation-only source — пишет в таблицу ``recommendations`` через
``item_destination = ItemDestination.RECOMMENDATIONS``. Архитектура диспатча
готова после task 06.

Phase 4 этапы (см. план v2):
  T1 — scaffold + guard (done)
  T2 — HTTP layer + parser primitives (done)
  T3 — _extract_fields regex (this)
  T4 — body cleaning + preformatted header (this)
  T5 — synthetic URL (this)
  T6 — fetch() integration (this)
"""
from __future__ import annotations

import html as html_module
import json
import logging
import re
import unicodedata
from datetime import datetime, time, timezone
from typing import Iterable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import HTMLParser, Node
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.sources.base import FetchContext, ItemDestination, RawItem, Source

log = logging.getLogger(__name__)

LISTING_PATH = "/analytics/ideas/"
HTTP_TIMEOUT_S = 20.0
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


# =============================================================================
# T2 — transient HTTP error classifier (tenacity predicate)
# =============================================================================
def _is_transient_http(exc: BaseException) -> bool:
    """Retry только network + 5xx. 4xx terminal; 3xx через `_http_get` → RuntimeError."""
    if isinstance(exc, (
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.ConnectTimeout,
        httpx.RemoteProtocolError,
    )):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


# =============================================================================
# T3 — structural field extraction (regex)
# =============================================================================
# Order matters: negation проверяется ПЕРВОЙ, иначе positive regex заматчит
# "покупать" внутри "не рекомендуем покупать" → возьмёт `buy` вместо `sell`.
_NEGATION_PATTERNS = [
    # "Мы не рекомендуем приобретать/покупать" / "не рекомендуется покупать"
    # / "не рекомендуем «покупать»" (Сегежа, Эл5-Энерго — с кавычками)
    re.compile(r'не\s+рекомендуе[мт]?с?я?\s+[«"]?(?:приобрет|покуп)', re.I),
    # "не советуем «покупать»" / "не советуем приобретать" (Северсталь)
    re.compile(r'не\s+советуе[мт]\s+[«"]?(?:покуп|приобрет)', re.I),
]
# Positive citation: «рекомендацию «<X>»» — X ∈ {покупать, держать, продавать, ...}
_POSITIVE_RE = re.compile(r'рекомендац\w+\s+[«"]([^»"\n]+)[»"]', re.I)

_ACTION_MAP = {
    "покупать": "buy", "купить": "buy", "приобретать": "buy",
    "держать": "hold",
    "продавать": "sell", "продать": "sell",
}


def _extract_action(body: str) -> str | None:
    for neg in _NEGATION_PATTERNS:
        if neg.search(body):
            return "sell"
    m = _POSITIVE_RE.search(body)
    if m:
        return _ACTION_MAP.get(m.group(1).strip().lower())
    return None


# Target price: "целевой цены [в ]2800 руб" — optional `в\s+` (Россети)
_TARGET_RE = re.compile(
    r'целев\w+\s+цен\w+\s+(?:в\s+)?([\d\s.,   ]+?)\s*руб',
    re.I,
)

_POTENTIAL_RE = re.compile(r'(\d+(?:[.,]\d+)?)\s*%\s*потенциал', re.I)

# Net Debt / Net debt / ND, slash with or without spaces. ФосАгро использует
# "Net Debt/EBITDA" (capital D), X5 — "Net debt/ EBITDA" (пробел после /).
_MULTIPLIERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r'EV\s*/\s*EBITDA\s*=\s*(-?\d+(?:[.,]\d+)?)', re.I), 'ev_ebitda'),
    (re.compile(r'(?<![\w/])P\s*/\s*E\s*=\s*(-?\d+(?:[.,]\d+)?)', re.I), 'p_e'),
    (re.compile(r'Net\s+debt\s*/\s*EBITDA\s*=\s*(-?\d+(?:[.,]\d+)?)', re.I), 'nd_ebitda'),
]


def _parse_float(s: str) -> float | None:
    """Принимает '2 800,5', '2 800.5', '6,75' → float.

    Чистит: regular space, NBSP (U+00A0), narrow-NBSP (U+202F), figure-space
    (U+2007); запятая → точка.
    """
    if not s:
        return None
    for ch in (" ", " ", " ", " "):
        s = s.replace(ch, "")
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _extract_fields(body: str) -> dict:
    """Извлекает {target_price, recommendation_action, potential_pct, multipliers_json}
    из плоского текста body. Все поля могут быть None при отсутствии паттерна."""
    fields: dict = {
        "target_price": None,
        "recommendation_action": _extract_action(body),
        "potential_pct": None,
        "multipliers_json": None,
    }
    if (m := _TARGET_RE.search(body)):
        fields["target_price"] = _parse_float(m.group(1))
    if (m := _POTENTIAL_RE.search(body)):
        fields["potential_pct"] = _parse_float(m.group(1))
    found: dict[str, float] = {}
    for rx, key in _MULTIPLIERS:
        if (m := rx.search(body)):
            v = _parse_float(m.group(1))
            if v is not None:
                found[key] = v
    if found:
        fields["multipliers_json"] = json.dumps(found, ensure_ascii=False)
    return fields


# =============================================================================
# T4 — body cleaning + preformatted header
# =============================================================================
_NBSP_CHARS = "   "
_BR_RE = re.compile(r'<br\s*/?>', re.I)
_MULTI_NEWLINE_RE = re.compile(r'\n{3,}')
# Telegram-footer: "Телеграмм-канал: <url>" в конце текста. Опечатки автора:
# "Телеграмм"/"Телеграм"/"Телеграмн" + "канал"/"-канал" + любой пробел.
_TELEGRAM_FOOTER_RE = re.compile(
    r'\n+Тел[еэ]гра[мн]+\-?\s*канал\s*:[^\n]*$',
    re.I,
)


def _clean_text(s: str) -> str:
    """Локальный source-helper (см. CLAUDE.md `Body cleaning convention`):
    HTML-unescape, NBSP → space, control chars drop, multi-newline collapse."""
    if not s:
        return ""
    s = html_module.unescape(s)
    for ch in _NBSP_CHARS:
        s = s.replace(ch, " ")
    s = "".join(
        c for c in s
        if c == "\n" or c == "\t" or unicodedata.category(c)[0] != "C"
    )
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    s = _MULTI_NEWLINE_RE.sub("\n\n", s)
    return s.strip()


def _clean_body(html_fragment: str) -> str:
    """preview-text innerHTML → плоский текст с сохранёнными абзацами.

    Шаги:
      1. `<br>` → `\n` ДО selectolax (P2.6 — иначе abzac flatten'ятся в пробел)
      2. selectolax.text(strip=False)
      3. Telegram-footer strip
      4. `_clean_text` (NBSP, control, multi-newline)
    """
    if not html_fragment:
        return ""
    with_newlines = _BR_RE.sub("\n", html_fragment)
    text = HTMLParser(with_newlines).text(strip=False)
    # Normalise FIRST (drop trailing whitespace, collapse newlines), THEN strip
    # footer — иначе regex anchor `$` промахивается из-за trailing spaces после
    # "lmsstock" в исходном HTML.
    text = _clean_text(text)
    text = _TELEGRAM_FOOTER_RE.sub("", text)
    return text.rstrip()


def _extract_thesis(body_text: str) -> str:
    """Первый непустой абзац (до первого blank line) — обычно headline-тезис."""
    for para in body_text.split("\n\n"):
        para = para.strip()
        if para:
            return para
    return ""


def _build_headline(issuer: str, thesis: str) -> str:
    if thesis and issuer.lower() not in thesis.lower():
        return f"{issuer}: {thesis}"
    return thesis or issuer


def _build_body(fields: dict, body_text: str) -> str:
    """Preformatted header (рекомендация / target / multipliers) + blank line + body.

    Plan v2 P2.1: body_text УЖЕ начинается с thesis — НЕ дублируем его в header.
    Если все структурные поля None — возвращаем body_text как есть.
    """
    parts: list[str] = []
    if fields["recommendation_action"]:
        parts.append(f"Рекомендация: {fields['recommendation_action']}")
    if fields["target_price"] is not None:
        line = f"Целевая цена: {fields['target_price']:g} ₽"
        if fields["potential_pct"] is not None:
            line += f" ({fields['potential_pct']:+g}%)"
        parts.append(line)
    if fields["multipliers_json"]:
        m = json.loads(fields["multipliers_json"])
        bits: list[str] = []
        if "ev_ebitda" in m:
            bits.append(f"EV/EBITDA={m['ev_ebitda']:g}")
        if "p_e" in m:
            bits.append(f"P/E={m['p_e']:g}")
        if "nd_ebitda" in m:
            bits.append(f"Net debt/EBITDA={m['nd_ebitda']:g}")
        if bits:
            parts.append("Мультипликаторы: " + " · ".join(bits))
    if not parts:
        return body_text
    header = "\n".join(parts)
    return f"{header}\n\n{body_text}" if body_text else header


# =============================================================================
# T5 — synthetic URL (dedup ключ для INSERT OR IGNORE)
# =============================================================================
_CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
_SLUG_KEEP_RE = re.compile(r"[^a-z0-9\-]")
_SLUG_DASHES_RE = re.compile(r"-+")


def _slugify(s: str) -> str:
    """Cyrillic→latin transliteration → kebab-case ASCII slug."""
    s = (s or "").lower()
    s = "".join(_CYR_TO_LAT.get(ch, ch) for ch in s)
    s = s.replace(" ", "-").replace("\t", "-").replace("/", "-")
    s = _SLUG_KEEP_RE.sub("", s)
    s = _SLUG_DASHES_RE.sub("-", s).strip("-")
    return s


def _build_url(published_at: datetime, issuer: str, thesis: str) -> str:
    """Синтетический URL `…#YYYYMMDD-<issuer-slug>[-<thesis-slug-3words>]`.

    Plan v2 P2.4: thesis-slug-prefix добавлен СРАЗУ как disambiguator,
    не «if needed» — защищает от коллизии при двух идеях по одному issuer'у
    в один день (теоретически возможно при переоценке).
    """
    issuer_slug = _slugify(issuer)
    thesis_words = [w for w in _slugify(thesis).split("-") if w][:3]
    thesis_slug = "-".join(thesis_words)
    date_part = published_at.strftime("%Y%m%d")
    fragment = f"{date_part}-{issuer_slug}" if issuer_slug else date_part
    if thesis_slug:
        fragment += f"-{thesis_slug}"
    return f"https://www.lmsic.com/analytics/ideas/#{fragment}"


# =============================================================================
# LmsicSource — orchestration
# =============================================================================
class LmsicSource(Source):
    code = "lmsic"
    item_destination = ItemDestination.RECOMMENDATIONS

    def __init__(
        self,
        base_url: str,
        user_agent: str = "Mozilla/5.0 (compatible; trading-news/0.1)",
        context: FetchContext | None = None,
    ) -> None:
        if context is None:
            raise ValueError(
                "LmsicSource requires FetchContext (need company aliases for issuer matching)"
            )
        super().__init__(base_url=base_url, user_agent=user_agent, context=context)
        self._client = httpx.Client(
            headers={"User-Agent": self.user_agent},
            timeout=HTTP_TIMEOUT_S,
            follow_redirects=False,
        )

    # ---------------------------------------------------------------- public

    def close(self) -> None:
        self._client.close()

    def fetch(self, since: datetime) -> Iterable[RawItem]:
        """T6 — full pipeline.

        Filter order (plan v2 P1.5): date ПЕРВОЙ (cheap, structural), потом
        issuer-match. Это даёт корректную статистику: если `since > newest_date`,
        все 10 items уходят в `filtered_date`, ни одного в `filtered_issuer`.
        """
        since_utc = _to_utc(since)
        try:
            html = self._fetch_listing()
        except Exception as exc:
            log.error("lmsic: fetch listing failed: %s", exc.__class__.__name__)
            raise

        tree = HTMLParser(html)
        kept = filtered_date = filtered_issuer = 0
        items: list[RawItem] = []

        for node in tree.css("li.ideas-page__list-item"):
            try:
                card = self._parse_card(node)
            except ValueError as exc:
                log.warning("lmsic: skip malformed card: %s", exc)
                continue

            try:
                published_at = self._published_at(card["date"])
            except ValueError:
                log.warning("lmsic: skip card with bad date %r", card["date"])
                continue

            # FILTER 1: date
            if published_at < since_utc:
                filtered_date += 1
                continue

            # FILTER 2: issuer
            if not self._match_company(card["issuer"]):
                filtered_issuer += 1
                continue

            body_text = _clean_body(card["body_html"])
            fields = _extract_fields(body_text)
            thesis = _extract_thesis(body_text)
            full_body = _build_body(fields, body_text)
            headline = _build_headline(card["issuer"], thesis)
            url = _build_url(published_at, card["issuer"], thesis)

            kept += 1
            items.append(RawItem(
                url=url,
                headline=_clean_text(headline),
                body=_clean_text(full_body),
                published_at=published_at,
                target_price=fields["target_price"],
                recommendation_action=fields["recommendation_action"],
                potential_pct=fields["potential_pct"],
                multipliers_json=fields["multipliers_json"],
            ))

        log.info(
            "lmsic fetch: kept=%d filtered_date=%d filtered_issuer=%d",
            kept, filtered_date, filtered_issuer,
        )
        return iter(items)

    # ------------------------------------------------------------- internals

    def _listing_url(self) -> str:
        return urljoin(self.base_url, LISTING_PATH)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception(_is_transient_http),
        reraise=True,
    )
    def _http_get(self, url: str) -> str:
        """GET с explicit status handling (plan v2 codex P2.5):
        200 ok / 3xx RuntimeError / 4xx terminal / 5xx tenacity retry."""
        r = self._client.get(url)
        if r.is_redirect:
            loc = r.headers.get("location", "<missing>")
            raise RuntimeError(
                f"lmsic: unexpected redirect from {url} → {loc} "
                f"(listing endpoint must not redirect)"
            )
        r.raise_for_status()
        return r.text

    def _fetch_listing(self) -> str:
        return self._http_get(self._listing_url())

    # ---- parsing primitives ------------------------------------------------

    @staticmethod
    def _published_at(date_str: str) -> datetime:
        """`DD.MM.YYYY` → UTC datetime at 23:59 Europe/Moscow (recon §12)."""
        d = datetime.strptime(date_str.strip(), "%d.%m.%Y").date()
        local = datetime.combine(d, time(23, 59), tzinfo=MOSCOW_TZ)
        return local.astimezone(timezone.utc)

    @staticmethod
    def _parse_card(node: Node) -> dict:
        date_node = node.css_first("span.ideas-card__date")
        if date_node is None:
            raise ValueError("lmsic card: span.ideas-card__date missing")
        title_node = node.css_first("div.ideas-card__title")
        if title_node is None:
            raise ValueError("lmsic card: div.ideas-card__title missing")
        body_node = node.css_first("div.ideas-card__preview-text")
        body_html = (body_node.html or "") if body_node is not None else ""
        return {
            "date": date_node.text(strip=True),
            "issuer": title_node.text(strip=True),
            "body_html": body_html,
        }

    def _match_company(self, issuer: str) -> bool:
        if not issuer:
            return False
        assert self.context is not None
        keywords = self.context.load_keywords().strong
        issuer_lc = issuer.lower()
        return any(kw and kw.lower() in issuer_lc for kw in keywords)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
