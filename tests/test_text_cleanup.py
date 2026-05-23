"""Unit tests for src.text_cleanup — shared cleanup helpers used by both
reporter (for .md/.xlsx output) and analyzer (for LLM input)."""
from __future__ import annotations

from src.text_cleanup import clean_text, sanitize_inline_code


def test_sanitize_inline_code_truncates_at_css_marker() -> None:
    article = "Реальный текст статьи."
    garbage = " .rate:hover { color: #333; }"
    assert sanitize_inline_code(article + garbage) == article


def test_sanitize_inline_code_truncates_at_data_attr_selector() -> None:
    article = "Статья. "
    garbage = "[data-element-name='deck'] { min-height: 30px; }"
    assert sanitize_inline_code(article + garbage).startswith("Статья")
    assert "data-element-name" not in sanitize_inline_code(article + garbage)


def test_sanitize_inline_code_truncates_at_function_decl() -> None:
    article = "Текст статьи. "
    garbage = "function(req){return 1;}"
    cleaned = sanitize_inline_code(article + garbage)
    assert "function" not in cleaned


def test_sanitize_inline_code_truncates_at_window_check() -> None:
    article = "Текст. "
    garbage = "if(window.foo!=undefined){bar();}"
    cleaned = sanitize_inline_code(article + garbage)
    assert "window" not in cleaned


def test_sanitize_inline_code_truncates_at_var_decl() -> None:
    article = "Текст. "
    garbage = "var combo_3f19 = { x: 1 };"
    cleaned = sanitize_inline_code(article + garbage)
    assert "var combo" not in cleaned


def test_sanitize_inline_code_truncates_at_media_rule() -> None:
    article = "Текст. "
    garbage = "@media (max-width: 600px) { .foo { color: red; } }"
    cleaned = sanitize_inline_code(article + garbage)
    assert "@media" not in cleaned


def test_sanitize_inline_code_noop_on_clean_text() -> None:
    body = "Просто чистый текст без JS и CSS."
    assert sanitize_inline_code(body) == body


def test_sanitize_inline_code_handles_empty_and_none() -> None:
    assert sanitize_inline_code("") == ""
    assert sanitize_inline_code(None) == ""  # type: ignore[arg-type]


def test_clean_text_unescapes_entities() -> None:
    """&quot; должно стать настоящей кавычкой."""
    assert clean_text('&quot;X5&quot;') == '"X5"'


def test_clean_text_combines_unescape_and_truncate() -> None:
    """Композит: сначала unescape, потом отрезать garbage."""
    raw = '&quot;ИКС 5&quot; объявила результаты. .rate:hover { color: red; }'
    cleaned = clean_text(raw)
    assert cleaned == '"ИКС 5" объявила результаты.'
    assert "rate" not in cleaned
    assert "&quot;" not in cleaned


def test_clean_text_none_and_empty() -> None:
    assert clean_text(None) == ""
    assert clean_text("") == ""


def test_sanitize_strips_x5_pdf_press_release_footer() -> None:
    article = (
        "Реальный текст статьи про X5. По данным INFOLine, "
        "сеть стала лидером среди крупнейших FMCG-сетей."
    )
    footer = "\n\nПресс-релиз\n\npdf, 407 КБ\n\nКатегория:\n\nРазвитие логистики"
    cleaned = sanitize_inline_code(article + footer)
    assert "Пресс-релиз" not in cleaned
    assert "pdf, 407" not in cleaned
    assert "Развитие логистики" not in cleaned
    assert cleaned.startswith("Реальный текст")
    # последнее предложение статьи сохранено
    assert "FMCG-сетей" in cleaned


def test_sanitize_strips_x5_footer_when_only_category_present() -> None:
    article = "Статья про X5. Конец материала."
    footer = "\n\nКатегория:\n\nКорпоративные новости"
    cleaned = sanitize_inline_code(article + footer)
    assert "Категория" not in cleaned
    assert "Корпоративные новости" not in cleaned
    assert cleaned.endswith("Конец материала.")


def test_sanitize_press_release_marker_requires_pdf_pattern() -> None:
    """Слово «Пресс-релиз» в тексте статьи НЕ должно отрезать body —
    нужно полное совпадение `Пресс-релиз pdf, NNN`. Защита от FP."""
    article = (
        "Сегодня компания распространила пресс-релиз с новой информацией "
        "о результатах деятельности за год."
    )
    cleaned = sanitize_inline_code(article)
    assert cleaned == article  # ничего не отрезано
