"""Tests for src/sources/lmsic.py — LMSIC trading recommendations parser.

Phase 4 implementation tracker:
- T1: scaffold + guard (this file initial)
- T2: listing parser + HTTP layer
- T3: _extract_fields (regex)
- T4: body cleaning + header
- T5: synthetic URL
- T6: integration fetch()
- T7: full coverage (~25 tests target)
"""
from __future__ import annotations

import pytest

from src.sources.base import ItemDestination
from src.sources.lmsic import LmsicSource


# ---- T1 tests ----------------------------------------------------------------


def test_item_destination_is_recommendations():
    """LmsicSource declares recommendation-only destination → fetcher dispatches
    items into `recommendations` table, not `news`."""
    assert LmsicSource.item_destination is ItemDestination.RECOMMENDATIONS


def test_init_without_context_raises():
    """Codex P2.8: explicit FetchContext requirement. Without context, _match_company
    would fail with opaque AttributeError; raise ValueError up-front instead."""
    with pytest.raises(ValueError, match="FetchContext"):
        LmsicSource(base_url="https://www.lmsic.com/", context=None)


def test_code_attribute():
    assert LmsicSource.code == "lmsic"
