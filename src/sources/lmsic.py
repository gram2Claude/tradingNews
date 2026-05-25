"""lmsic.com/analytics/ideas — трейдинговые рекомендации инвесткомпании ЛМС.

Recon: tests/fixtures/LMSIC_RECON.md
Spec: work_directory/01_specs/07_lmsic_ideas_spec.md
Plan: work_directory/02_plans/07_claude_lmsic_ideas_plan.md (v2)

Recommendation-only source — пишет в таблицу ``recommendations`` через
``item_destination = ItemDestination.RECOMMENDATIONS``. Архитектура диспатча
готова после task 06.

Структура страницы:
- SSR, 10 items в листинге, нет anti-bot
- Селекторы: ``li.ideas-page__list-item`` → ``span.ideas-card__date`` /
  ``div.ideas-card__title`` / ``div.ideas-card__preview-text``
- Per-idea URL отсутствует ("Подробнее" — CSS-toggle, не href) →
  синтетический URL ``…/#YYYYMMDD-<issuer-slug>-<thesis-slug>``
- Структурные поля (target_price / recommendation_action / multipliers) —
  в теле текстом, извлекаются regex'ом в ``_extract_fields`` (T3, не в этом T1)

T1 — scaffold + конфиг. Парсер заполняется в T2-T6.
"""
from __future__ import annotations

import logging

from src.sources.base import FetchContext, ItemDestination, Source

# RawItem + Iterable будут импортированы в T6 (fetch() integration).

log = logging.getLogger(__name__)


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

    def fetch(self, since):  # type: ignore[no-untyped-def]
        # T2-T6 — заполнить. Сейчас — placeholder.
        log.info("lmsic fetch placeholder (T1): no items yielded")
        return iter([])
