"""Defensive text cleanup для контента из источников.

Используется на двух стадиях:
* Reporter (`reporter._report_company`) — чистит то, что выдаём в .md/.xlsx.
* Analyzer (`analyzer._analyze_one`) — чистит то, что шлём в LLM.

Оба места применяют:
1. `html.unescape()` — раскрывает `&quot;`, `&amp;`, `&#39;` и пр. сущности
   в реальные символы.
2. `_sanitize_inline_code()` — обрезает body на первом маркере inline JS/CSS
   дампа из встроенных виджетов источника (комментарии, AI-инсайты, рекламные
   слайдеры). Это backup на случай старых строк в БД, сохранённых до фикса
   парсера; для новых fetch'ей он no-op.
"""
from __future__ import annotations

import html as html_module
import re

# Маркеры конца статьи / начала «лишней информации».
#
# Срабатывают только если в body есть такая подстрока — defensive.
# Объединено в один регексп для того чтобы truncate шёл на ПЕРВОМ совпадении
# любой категории (раньше = безопаснее: оставляем меньше шума).
#
# Категории (см. соответствующие тесты):
# 1. Inline JS/CSS дампы из widget'ов источника (finam embeds комментариев,
#    AI-инсайтов, glide-слайдеров, deck-quest).
# 2. Trailing footer пресс-релизов X5 IR: ссылка на PDF + категория.
_INLINE_CODE_MARKERS_RE = re.compile(
    # --- Inline JS / CSS ---
    r"(\.\w[\w\-]*\s*:?\s*\w*\s*\{"          # CSS rule: .foo:hover { , .bar {
    r"|\[data-[\w\-]+[^\]]*\]"               # CSS attribute selector
    r"|function\s*\([^)]*\)\s*\{"            # function(...) {
    r"|if\s*\(\s*window\."                   # if(window.xxx
    r"|var\s+\w+\s*=\s*\{"                   # var foo = {
    r"|@media\b"                             # @media rule
    # --- X5 IR press-release footer ---
    r"|\bПресс-?релиз\s+pdf,\s*\d"           # «Пресс-релиз pdf, 407 КБ»
    r"|\bКатегория\s*:\s*$"                  # standalone «Категория:» label
    r")",
    re.MULTILINE,
)


def sanitize_inline_code(text: str | None) -> str:
    """Обрезать text на первом маркере inline JS/CSS-дампа. None / '' → ''."""
    if not text:
        return text or ""
    m = _INLINE_CODE_MARKERS_RE.search(text)
    if not m:
        return text
    return text[: m.start()].rstrip()


def clean_text(text: str | None) -> str:
    """Композитный cleanup: html.unescape + sanitize_inline_code. None → ''."""
    if not text:
        return ""
    return sanitize_inline_code(html_module.unescape(text))
