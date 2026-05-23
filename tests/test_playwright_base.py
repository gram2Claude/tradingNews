"""Unit tests для PlaywrightSource ABC. Всё на моках, без реального браузера.

Один opt-in smoke (`@pytest.mark.smoke`) тестирует реальный Playwright на example.com.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.sources.playwright_base import (
    _BrowserDead,
    _ChallengeFailure,
    PlaywrightSource,
)


class _DummyPWSource(PlaywrightSource):
    """Concrete subclass for testing the abstract base."""

    def fetch(self, since):
        return []


def _wire_mocks_into(src, *, content="x" * 5000, cookies=("ok",), visible=True):
    """Inject mock objects bypassing __enter__."""
    src._page = MagicMock()
    src._bcontext = MagicMock()
    src._browser = MagicMock()
    src._pw = MagicMock()
    src._stealth_cm = MagicMock()
    src._sp = MagicMock()
    src._page.content.return_value = content
    src._bcontext.cookies.return_value = [{"name": n} for n in cookies]
    src._page.is_visible.return_value = visible
    src._page.goto.return_value = None
    return src


# ---------- ABC contract ----------


def test_playwright_base_is_abstract() -> None:
    with pytest.raises(TypeError):
        PlaywrightSource(base_url="https://example.com")  # type: ignore[abstract]


# ---------- close() idempotency ----------


def test_close_when_never_entered_is_noop() -> None:
    src = _DummyPWSource(base_url="https://example.com")
    src.close()  # должно не падать
    assert src._page is None
    assert src._browser is None


def test_close_obnules_all_fields() -> None:
    src = _DummyPWSource(base_url="https://example.com")
    _wire_mocks_into(src)
    src.close()
    assert src._page is None
    assert src._bcontext is None
    assert src._browser is None
    assert src._pw is None
    assert src._stealth_cm is None


def test_close_swallows_exceptions() -> None:
    src = _DummyPWSource(base_url="https://example.com")
    _wire_mocks_into(src)
    src._page.close.side_effect = RuntimeError("boom")
    src._browser.close.side_effect = RuntimeError("boom")
    src.close()  # должно не падать
    assert src._page is None
    assert src._browser is None


def test_close_is_idempotent() -> None:
    src = _DummyPWSource(base_url="https://example.com")
    _wire_mocks_into(src)
    src.close()
    src.close()  # повторный вызов — no-op


# ---------- headless env override ----------


def test_headless_default_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default — headless=True."""
    monkeypatch.delenv("PLAYWRIGHT_HEADLESS", raising=False)
    captured: dict = {}

    def fake_sync_playwright():
        cm = MagicMock()
        pw = MagicMock()

        def launch(*args, headless=True, **kwargs):
            captured["headless"] = headless
            return MagicMock()
        pw.chromium.launch = launch
        cm.__enter__.return_value = pw
        return cm

    class FakeStealth:
        def use_sync(self, sp):
            return sp

    with patch("src.sources.playwright_base.sync_playwright", fake_sync_playwright), \
         patch("src.sources.playwright_base.Stealth", FakeStealth):
        src = _DummyPWSource(base_url="https://example.com")
        try:
            src.__enter__()
        finally:
            src.close()
    assert captured["headless"] is True


def test_headless_env_override_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "false")
    captured: dict = {}

    def fake_sync_playwright():
        cm = MagicMock()
        pw = MagicMock()

        def launch(*args, headless=True, **kwargs):
            captured["headless"] = headless
            return MagicMock()
        pw.chromium.launch = launch
        cm.__enter__.return_value = pw
        return cm

    class FakeStealth:
        def use_sync(self, sp):
            return sp

    with patch("src.sources.playwright_base.sync_playwright", fake_sync_playwright), \
         patch("src.sources.playwright_base.Stealth", FakeStealth):
        src = _DummyPWSource(base_url="https://example.com")
        try:
            src.__enter__()
        finally:
            src.close()
    assert captured["headless"] is False


# ---------- __enter__ failure cleanup ----------


def test_enter_failure_calls_close_and_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если launch() падает, __enter__ должен сделать close() и raise."""
    def fake_sync_playwright():
        cm = MagicMock()
        pw = MagicMock()
        pw.chromium.launch.side_effect = RuntimeError("launch failed")
        cm.__enter__.return_value = pw
        return cm

    class FakeStealth:
        def use_sync(self, sp):
            return sp

    src = _DummyPWSource(base_url="https://example.com")
    with patch("src.sources.playwright_base.sync_playwright", fake_sync_playwright), \
         patch("src.sources.playwright_base.Stealth", FakeStealth):
        with pytest.raises(RuntimeError, match="launch failed"):
            src.__enter__()
    # Все fields обнулены после close()
    assert src._browser is None
    assert src._pw is None


# ---------- _goto ----------


def test_goto_raises_browser_dead_if_page_none() -> None:
    src = _DummyPWSource(base_url="https://example.com")
    # _page = None
    with pytest.raises(_BrowserDead):
        src._goto("https://example.com/x")


def test_goto_sleeps_after_domcontentloaded() -> None:
    src = _DummyPWSource(base_url="https://example.com")
    _wire_mocks_into(src, content="ok")
    with patch("src.sources.playwright_base.time.sleep") as sleep_mock:
        src._goto("https://example.com/x", sleep_s=2.5)
    sleep_mock.assert_called_with(2.5)
    src._page.goto.assert_called_once()
    # wait_until="domcontentloaded", not networkidle
    call_kwargs = src._page.goto.call_args.kwargs
    assert call_kwargs.get("wait_until") == "domcontentloaded"


def test_goto_polite_pause_between_requests() -> None:
    src = _DummyPWSource(base_url="https://example.com")
    _wire_mocks_into(src, content="ok")
    with patch("src.sources.playwright_base.time.sleep") as sleep_mock:
        src._goto("https://example.com/a", sleep_s=1)
        src._goto("https://example.com/b", sleep_s=1)
    # Sleeps: первый goto = только sleep_s=1; второй = polite_pause(uniform 1..3) + sleep_s=1
    assert sleep_mock.call_count >= 3  # 1 + (1 polite + 1 sleep_s)


# ---------- _verify_warmup_success ----------


def test_verify_warmup_passes_on_good_state() -> None:
    src = _DummyPWSource(base_url="https://example.com")
    _wire_mocks_into(src, content="x" * 5000, cookies=("PVID", "VID"), visible=True)
    telemetry = src._verify_warmup_success("h1")
    assert telemetry["is_challenge"] is False
    assert telemetry["selector_visible"] is True
    assert telemetry["html_size"] == 5000


def test_verify_warmup_raises_on_servicepipe_marker() -> None:
    src = _DummyPWSource(base_url="https://example.com")
    _wire_mocks_into(src, content="<html>servicepipe.ru challenge</html>", visible=True)
    with pytest.raises(_ChallengeFailure, match="is_challenge=True"):
        src._verify_warmup_success("h1")


def test_verify_warmup_raises_on_small_html() -> None:
    src = _DummyPWSource(base_url="https://example.com")
    _wire_mocks_into(src, content="tiny", visible=True)
    with pytest.raises(_ChallengeFailure):
        src._verify_warmup_success("h1")


def test_verify_warmup_raises_on_invisible_selector() -> None:
    src = _DummyPWSource(base_url="https://example.com")
    _wire_mocks_into(src, content="x" * 5000, visible=False)
    with pytest.raises(_ChallengeFailure, match="selector_visible=False"):
        src._verify_warmup_success("h1")


# ---------- smoke (opt-in) ----------


@pytest.mark.smoke
def test_playwright_smoke_example_com() -> None:
    """Реальный page.goto на https://example.com — sanity check Playwright install.

    Запускается через `pytest tests/ -m smoke` (opt-in, не дефолтный).
    """
    src = _DummyPWSource(base_url="https://example.com")
    with src:
        html = src._goto("https://example.com", sleep_s=1)
    assert "Example Domain" in html
