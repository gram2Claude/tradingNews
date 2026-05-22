"""Playwright-based Source base class — для источников за JS-challenge WAF (servicepipe, etc).

Lessons learned (T7.2 + T8.1 recon):
* ``wait_until="networkidle"`` НЕ работает — на финансовых сайтах долгие XHR
  (аналитика, реклама) держат сеть активной. Используем
  ``wait_until="domcontentloaded"`` + явный ``time.sleep(WARMUP_SLEEP_S)``.
* ``playwright-stealth`` обязателен — default Playwright headless блокируется
  servicepipe. С stealth — passes на chromium headless.
* Servicepipe IP throttle: 5-10 rapid запросов с одного IP → блок 15-60 мин.
  4-часовая cadence далеко за этим threshold.

Контракт subclasses:
* реализовать ``fetch(since)`` (из Source)
* в начале fetch — `_goto(self.base_url)` для warmup
* вызвать ``self._verify_warmup_success(WARMUP_SELECTOR)`` чтобы убедиться
  что challenge прошёл и мы видим целевой контент
* остальные `_goto(url)` — для per-article запросов

Lifecycle: managed через ``__enter__/__exit__`` (наследуется из Source ABC).
"""
from __future__ import annotations

import logging
import os
import random
import time
from abc import ABC

from typing import Any

from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)
from playwright_stealth import Stealth

from src.sources.base import FetchContext, Source

log = logging.getLogger(__name__)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Sleep после `domcontentloaded` чтобы JS challenge resolve + страница доросла.
# Warmup длиннее — больше JS работы при первом goto в сессии.
WARMUP_SLEEP_S = 8
ARTICLE_SLEEP_S = 3


class _ChallengeFailure(Exception):
    """Warmup завершился без признаков прохождения WAF challenge."""


class _BrowserDead(Exception):
    """Browser/page закрылся вне нашего control (crash / external close)."""


class PlaywrightSource(Source, ABC):
    """Base для anti-bot защищённых источников."""

    DEFAULT_TIMEOUT_MS = 30_000
    MIN_PAUSE_S = 1.0
    MAX_PAUSE_S = 3.0

    def __init__(
        self,
        base_url: str,
        user_agent: str | None = None,
        context: FetchContext | None = None,
    ) -> None:
        super().__init__(base_url=base_url, user_agent=user_agent or _DEFAULT_UA, context=context)
        # Type as Any — Playwright sync API types are not easily annotatable
        # without imports that aren't always available; lifecycle is the API
        # surface that matters, not the inner Playwright types.
        self._sp: Any = None  # sync_playwright() context manager
        self._stealth_cm: Any = None
        self._pw: Any = None
        self._browser: Any = None
        self._bcontext: Any = None
        self._page: Any = None
        self._requests_made = 0

    def __enter__(self) -> "PlaywrightSource":
        try:
            headless = os.environ.get("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
            self._sp = sync_playwright()
            self._stealth_cm = Stealth().use_sync(self._sp)
            pw = self._stealth_cm.__enter__()
            self._pw = pw
            browser = pw.chromium.launch(headless=headless)
            self._browser = browser
            bcontext = browser.new_context(
                user_agent=self.user_agent,
                viewport={"width": 1280, "height": 800},
                locale="ru-RU",
                timezone_id="Europe/Moscow",
            )
            self._bcontext = bcontext
            self._page = bcontext.new_page()
            log.info("playwright: chromium launched headless=%s", headless)
            return self
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Idempotent cleanup. Обнуляет ссылки чтобы повторный close был no-op."""
        for attr in ("_page", "_bcontext", "_browser"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        if self._stealth_cm is not None:
            try:
                self._stealth_cm.__exit__(None, None, None)
            except Exception:
                pass
            self._stealth_cm = None
            self._pw = None
            self._sp = None

    def _polite_pause(self) -> None:
        time.sleep(random.uniform(self.MIN_PAUSE_S, self.MAX_PAUSE_S))

    def _goto(self, url: str, sleep_s: float = ARTICLE_SLEEP_S) -> str:
        """Navigate to url, wait domcontentloaded, sleep `sleep_s`, return HTML.

        Raises:
          _BrowserDead — если page/browser закрылись externally.
          PlaywrightTimeoutError — если page.goto не успел за DEFAULT_TIMEOUT_MS.
        """
        if self._page is None or self._browser is None:
            raise _BrowserDead("browser/page is None — closed externally?")
        try:
            if self._requests_made > 0:
                self._polite_pause()
            self._requests_made += 1
            self._page.goto(url, timeout=self.DEFAULT_TIMEOUT_MS, wait_until="domcontentloaded")
            time.sleep(sleep_s)
            return self._page.content()
        except PlaywrightTimeoutError:
            # propagate as-is; caller distinguishes
            raise
        except PlaywrightError as exc:
            msg = str(exc).lower()
            if "closed" in msg or "target page" in msg or "browser has been closed" in msg:
                raise _BrowserDead(str(exc)) from exc
            raise

    def _verify_warmup_success(self, expected_selector: str) -> dict:
        """После warmup goto — убедиться что challenge пройден.

        Проверяет:
        * HTML не challenge-страница (size > 2500, нет 'servicepipe.ru' / 'id_spinner' markers)
        * `expected_selector` виден на странице

        Cookies (spsn/spid) логируются для telemetry, но НЕ требуются —
        servicepipe selectively включается, под stealth может вообще не сработать.

        Returns telemetry dict. Raises _ChallengeFailure если warmup failed.
        """
        assert self._page is not None and self._bcontext is not None
        t0 = time.monotonic()
        cookies = sorted({c["name"] for c in self._bcontext.cookies()})
        html = self._page.content()
        is_challenge = (
            "servicepipe.ru" in html
            or "id_spinner" in html
            or len(html) < 2500
        )
        try:
            selector_visible = self._page.is_visible(expected_selector, timeout=5000)
        except PlaywrightError:
            selector_visible = False
        telemetry = {
            "verify_ms": int((time.monotonic() - t0) * 1000),
            "cookies_count": len(cookies),
            "cookies_sample": cookies[:8],
            "is_challenge": is_challenge,
            "selector_visible": selector_visible,
            "html_size": len(html),
        }
        log.info("playwright warmup verify: %s", telemetry)
        if is_challenge or not selector_visible:
            raise _ChallengeFailure(
                f"warmup failed: is_challenge={is_challenge} "
                f"selector_visible={selector_visible} (selector={expected_selector!r})"
            )
        return telemetry
