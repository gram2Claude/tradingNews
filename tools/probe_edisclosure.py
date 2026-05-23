"""T7.2 probe — Playwright матрица для e-disclosure.ru.

Запускает {chromium, firefox} × {headless, headed}, фиксирует точный
failure mode для каждой комбинации. Цель — найти **один работающий
режим** который reliably открывает X5 listing.

Запуск:
    .venv\\Scripts\\python.exe tools\\probe_edisclosure.py

Что проверяет на каждом режиме:
1. Главная e-disclosure.ru открывается через networkidle
2. Cookies spsn + spid установлены (servicepipe прошёл)
3. HTML не challenge-страница (нет 'servicepipe.ru' маркеров)
4. /Company/Search?query=Корпоративный+центр+ИКС+5 возвращает результаты
5. Можно найти ссылку на X5 компанию
6. Открыть страницу компании, найти listing раскрытий
7. Pagination: есть ли page=2?
8. Открыть одно раскрытие, прочитать тело

Если хоть один шаг падает — режим помечен FAIL с конкретной причиной.
Первый PASS-режим логируется как "production candidate".
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path
from typing import Iterator

from playwright.sync_api import (
    Browser,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)
from playwright_stealth import Stealth

REPO_ROOT = Path(__file__).resolve().parent.parent
FIX_DIR = REPO_ROOT / "tests" / "fixtures"
FIX_DIR.mkdir(parents=True, exist_ok=True)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Эти query terms перебираются по очереди — берём первый дающий результат.
SEARCH_QUERIES = [
    "Корпоративный центр ИКС 5",
    "ИКС 5",
    "X5",
    "Корпоративный центр",
]

MATRIX = [
    ("chromium", True),
    ("chromium", False),
    ("firefox", True),
    ("firefox", False),
]


def iter_modes() -> Iterator[tuple[str, bool]]:
    for browser_name, headless in MATRIX:
        yield browser_name, headless


def run_one(pw, browser_name: str, headless: bool) -> dict:
    """Прогоняет один режим. Возвращает dict с findings."""
    label = f"{browser_name} {'headless' if headless else 'headed'}"
    result = {
        "mode": label,
        "ok": False,
        "warmup_ms": None,
        "cookies": [],
        "html_size_main": None,
        "is_challenge": None,
        "search_query_used": None,
        "x5_company_link": None,
        "listing_url": None,
        "events_found_on_listing": None,
        "page2_exists": None,
        "first_event_url": None,
        "first_event_title": None,
        "first_event_body_len": None,
        "failure": None,
    }

    try:
        browser_launcher = getattr(pw, browser_name)
        browser: Browser = browser_launcher.launch(headless=headless)
    except Exception as exc:
        result["failure"] = f"launch failed: {type(exc).__name__}: {exc}"
        return result

    try:
        context = browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
            locale="ru-RU",
            timezone_id="Europe/Moscow",
        )
        page = context.new_page()

        # 1. Главная — warmup servicepipe challenge
        t0 = time.monotonic()
        try:
            page.goto("https://www.e-disclosure.ru/", timeout=30_000, wait_until="networkidle")
        except PlaywrightTimeoutError as exc:
            result["failure"] = f"main goto timeout: {exc}"
            return result
        result["warmup_ms"] = int((time.monotonic() - t0) * 1000)

        # Cookies / challenge marker check
        cookies = context.cookies()
        cookie_names = sorted({c["name"] for c in cookies})
        result["cookies"] = [n for n in cookie_names if n.startswith(("sp", "__qrator", "qr"))] or cookie_names[:10]
        html_main = page.content()
        result["html_size_main"] = len(html_main)
        result["is_challenge"] = (
            "servicepipe.ru" in html_main
            or "id_spinner" in html_main
            or len(html_main) < 2500
        )

        if result["is_challenge"]:
            result["failure"] = "main page still shows challenge after networkidle"
            return result

        # 2. Search — найти X5
        x5_link = None
        for q in SEARCH_QUERIES:
            search_url = f"https://www.e-disclosure.ru/portal/files.aspx?queryEvent={q.replace(' ', '+')}"
            # альтернативный URL search'а
            search_url_alt = f"https://www.e-disclosure.ru/Company/Search?query={q.replace(' ', '+')}"
            for url in (search_url_alt, search_url):
                try:
                    page.goto(url, timeout=30_000, wait_until="networkidle")
                except PlaywrightTimeoutError:
                    continue
                # Поищем ссылку с "/portal/company.aspx?id=" или "/portal/files.aspx?id="
                try:
                    href = page.locator('a[href*="company.aspx?id="]').first.get_attribute("href", timeout=3_000)
                except (PlaywrightTimeoutError, PlaywrightError):
                    href = None
                if href:
                    result["search_query_used"] = q
                    x5_link = href
                    break
            if x5_link:
                break

        if not x5_link:
            result["failure"] = f"could not find X5 company link via any search query"
            return result

        result["x5_company_link"] = x5_link

        # 3. Открываем профиль компании, ищем listing раскрытий
        full_link = x5_link if x5_link.startswith("http") else f"https://www.e-disclosure.ru{x5_link}"
        try:
            page.goto(full_link, timeout=30_000, wait_until="networkidle")
        except PlaywrightTimeoutError as exc:
            result["failure"] = f"company page timeout: {exc}"
            return result

        company_html = page.content()
        # Ищем listing — events / news / disclosures table
        # Попробуем разные селекторы
        event_links = []
        for selector in [
            'a[href*="event.aspx?EventId="]',
            'a[href*="/portal/event.aspx?"]',
            'a[href*="files.aspx?id="]',
        ]:
            elements = page.locator(selector).all()
            if elements:
                for el in elements[:30]:
                    try:
                        href = el.get_attribute("href", timeout=1000)
                        text = el.inner_text(timeout=1000)
                        if href:
                            event_links.append((href, text))
                    except (PlaywrightTimeoutError, PlaywrightError):
                        continue
                if event_links:
                    break

        result["events_found_on_listing"] = len(event_links)
        if not event_links:
            result["failure"] = "no event links found on company page"
            # сохраним HTML для разбора
            (FIX_DIR / f"debug_company_{browser_name}_{'headless' if headless else 'headed'}.html").write_text(
                company_html, encoding="utf-8"
            )
            return result

        # 4. Pagination check
        # Ищем что-то типа "page=2", "Next", ">>"
        result["page2_exists"] = (
            "page=2" in company_html
            or "Page=2" in company_html
            or 'aria-label="Next' in company_html
        )

        # Сохраняем HTML listing'а
        result["listing_url"] = full_link
        listing_path = FIX_DIR / "edisclosure_listing.html"
        listing_path.write_text(company_html, encoding="utf-8")

        # 5. Открыть первое event-page
        first_href, first_text = event_links[0]
        first_url = first_href if first_href.startswith("http") else f"https://www.e-disclosure.ru{first_href}"
        try:
            page.goto(first_url, timeout=30_000, wait_until="networkidle")
        except PlaywrightTimeoutError as exc:
            result["failure"] = f"event page timeout: {exc}"
            return result

        event_html = page.content()
        result["first_event_url"] = first_url
        result["first_event_title"] = (first_text or page.title())[:120]
        result["first_event_body_len"] = len(event_html)

        event_path = FIX_DIR / "edisclosure_event.html"
        event_path.write_text(event_html, encoding="utf-8")

        result["ok"] = True
        return result

    except Exception as exc:
        result["failure"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[:500]}"
        return result
    finally:
        try:
            browser.close()
        except Exception:
            pass


def main():
    print("=== T7.2 Playwright probe — e-disclosure.ru matrix (with stealth) ===\n")
    results = []
    with Stealth().use_sync(sync_playwright()) as pw:
        for browser_name, headless in iter_modes():
            label = f"{browser_name} {'headless' if headless else 'headed'}"
            print(f"--- TRYING: {label} ---")
            try:
                r = run_one(pw, browser_name, headless)
            except Exception as exc:
                r = {"mode": label, "ok": False, "failure": f"outer: {exc}"}
            results.append(r)
            status = "PASS" if r["ok"] else "FAIL"
            print(f"    {status}: warmup_ms={r.get('warmup_ms')} "
                  f"cookies={r.get('cookies')} "
                  f"is_challenge={r.get('is_challenge')} "
                  f"events_found={r.get('events_found_on_listing')}")
            if r.get("failure"):
                print(f"    failure: {r['failure'][:300]}")
            print()
            if r["ok"]:
                # Первый PASS — сохраняем findings и выходим
                print(f"\n=== FIRST PASS: {label} ===")
                break

    # Дамп всех результатов
    out_lines = ["# T7.2 probe results\n"]
    for r in results:
        out_lines.append(f"## {r['mode']} — {'PASS' if r['ok'] else 'FAIL'}")
        for k, v in r.items():
            if k == "mode":
                continue
            out_lines.append(f"  {k}: {v}")
        out_lines.append("")
    out_path = FIX_DIR / "PROBE_RESULTS.md"
    out_path.write_text("\n".join(out_lines), encoding="utf-8")
    print(f"Full results written to {out_path}")


if __name__ == "__main__":
    main()
