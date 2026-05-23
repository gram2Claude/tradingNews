"""T8.2 probe — открывает 2-3 finam.ru/publications/item/ статьи через Playwright+stealth.

Цель:
- Сохранить HTML фикстуры (news-type + recommendation-type)
- Зафиксировать селекторы заголовка / тела / даты в FINAM_RECON.md
- URL date vs article meta verification (codex P1.5)
- Backfill coverage proof (codex P1.6) — parse все 70 listing dates
- Items-per-week histogram

Запуск: .venv\\Scripts\\python.exe tools\\probe_finam.py
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

REPO_ROOT = Path(__file__).resolve().parent.parent
FIX_DIR = REPO_ROOT / "tests" / "fixtures"
FIX_DIR.mkdir(parents=True, exist_ok=True)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

URL_DATE_RE = re.compile(r"-(\d{8})-(\d{4})/?$")

# Article candidates (из T8.1 listing fixture)
CANDIDATES = {
    "news": "/publications/item/29-aprelya-x5-predstavit-finansovye-rezultaty-za-1-kvartal-po-msfo-20260429-0524/",
    "recommendation": "/publications/item/aktsii-afk-sistema-ne-vyglyadyat-privlekatelnym-obektom-dlya-portfelnykh-investitsiy-20260417-0950/",
}


def extract_url_date(path: str) -> datetime | None:
    """YYYYMMDD-HHMM из URL → datetime UTC (assumes Moscow time, -3h)."""
    m = URL_DATE_RE.search(path)
    if not m:
        return None
    try:
        msk = datetime.strptime(f"{m.group(1)}{m.group(2)}", "%Y%m%d%H%M")
    except ValueError:
        return None
    return (msk - timedelta(hours=3)).replace(tzinfo=timezone.utc)


def main() -> None:
    with Stealth().use_sync(sync_playwright()) as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=UA, locale="ru-RU", timezone_id="Europe/Moscow",
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()

        # Warmup
        print("=== Warmup finam.ru ===")
        page.goto("https://www.finam.ru/", wait_until="domcontentloaded", timeout=15000)
        time.sleep(8)
        print(f"  size: {len(page.content())}")

        # ----- Save listing fresh (для backfill coverage proof) -----
        print("\n=== Re-fetch X5 publications listing ===")
        page.goto(
            "https://www.finam.ru/quote/moex/x5/publications/",
            wait_until="domcontentloaded", timeout=15000,
        )
        time.sleep(8)
        listing_html = page.content()
        (FIX_DIR / "finam_x5_publications.html").write_text(listing_html, encoding="utf-8")
        print(f"  size: {len(listing_html)}, saved")

        # Extract all article URLs + dates from listing
        item_re = re.compile(r'href="(/publications/item/[a-z0-9\-_]+-(\d{8})-(\d{4})/)"')
        articles = []
        for m in item_re.finditer(listing_html):
            d = extract_url_date(m.group(1))
            if d:
                articles.append((m.group(1), d))
        seen = set()
        uniq_articles = []
        for url, d in articles:
            if url not in seen:
                seen.add(url)
                uniq_articles.append((url, d))
        uniq_articles.sort(key=lambda x: x[1], reverse=True)

        print(f"\n=== Backfill coverage proof (codex P1.6) ===")
        print(f"  Total unique articles in listing: {len(uniq_articles)}")
        if uniq_articles:
            newest = uniq_articles[0][1]
            oldest = uniq_articles[-1][1]
            print(f"  Newest URL date: {newest.isoformat()}")
            print(f"  Oldest URL date: {oldest.isoformat()}")
            target_since = datetime(2026, 5, 1, tzinfo=timezone.utc)
            covers = oldest <= target_since
            print(f"  Covers since=2026-05-01: {'YES' if covers else 'NO — pagination needed'}")

        # Items-per-week histogram
        per_week = defaultdict(int)
        for _, d in uniq_articles:
            year, week, _ = d.isocalendar()
            per_week[f"{year}-W{week:02d}"] += 1
        print(f"\n=== Items per ISO-week ===")
        for week in sorted(per_week.keys(), reverse=True):
            print(f"  {week}: {per_week[week]}")

        # ----- Open 2 candidate articles (URL date vs meta verification) -----
        print(f"\n=== Opening sample articles ===")
        url_date_verify = []  # для codex P1.5

        for kind, path in CANDIDATES.items():
            full = f"https://www.finam.ru{path}"
            print(f"\n  --- {kind} ---")
            print(f"  url: {full}")
            url_date = extract_url_date(path)
            print(f"  URL date: {url_date.isoformat() if url_date else 'None'}")
            try:
                page.goto(full, wait_until="domcontentloaded", timeout=15000)
                time.sleep(5)
                html = page.content()
                print(f"  size: {len(html)}")
                # Save fixture
                fname = f"finam_article_{kind}.html"
                (FIX_DIR / fname).write_text(html, encoding="utf-8")
                print(f"  saved {fname}")
                # Title
                title = page.title()
                print(f"  title: {title[:120]}")
                # H1
                h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
                if h1_match:
                    h1 = re.sub(r"<[^>]+>", "", h1_match.group(1)).strip()
                    print(f"  h1: {h1[:160]}")
                # meta published_time
                meta_re_list = [
                    r'<meta\s+property="article:published_time"\s+content="([^"]+)"',
                    r'<meta\s+itemprop="datePublished"\s+content="([^"]+)"',
                    r'<meta\s+name="article:published_time"\s+content="([^"]+)"',
                ]
                for re_pat in meta_re_list:
                    m = re.search(re_pat, html)
                    if m:
                        print(f"  meta date: {m.group(1)}")
                        url_date_verify.append({
                            "kind": kind,
                            "url_date": url_date.isoformat() if url_date else None,
                            "meta_date": m.group(1),
                        })
                        break
                else:
                    # Inline date in body? Look for dd.mm.yyyy
                    dates = re.findall(r"\d{2}\.\d{2}\.\d{4}", html)[:5]
                    print(f"  inline dates (top 5): {dates}")
            except Exception as e:
                print(f"  FAIL: {type(e).__name__}: {str(e)[:200]}")

        # ----- 3 more sample URLs for date verify (5 total) -----
        if uniq_articles:
            mid_idx = len(uniq_articles) // 2
            samples = [
                ("oldest", uniq_articles[-1][0]),
                ("middle", uniq_articles[mid_idx][0]),
                ("newest_after_first", uniq_articles[1][0] if len(uniq_articles) > 1 else uniq_articles[0][0]),
            ]
            print(f"\n=== Date verify on 3 more URLs (oldest/middle/newest_after_first) ===")
            for label, path in samples:
                full = f"https://www.finam.ru{path}"
                url_date = extract_url_date(path)
                print(f"\n  --- {label}: {path[:80]} ---")
                print(f"  URL date: {url_date.isoformat() if url_date else 'None'}")
                try:
                    page.goto(full, wait_until="domcontentloaded", timeout=15000)
                    time.sleep(4)
                    html = page.content()
                    meta_re_list = [
                        r'<meta\s+property="article:published_time"\s+content="([^"]+)"',
                        r'<meta\s+itemprop="datePublished"\s+content="([^"]+)"',
                    ]
                    found_meta = None
                    for re_pat in meta_re_list:
                        m = re.search(re_pat, html)
                        if m:
                            found_meta = m.group(1)
                            break
                    print(f"  meta date: {found_meta}")
                    if url_date and found_meta:
                        url_date_verify.append({
                            "kind": label,
                            "url_date": url_date.isoformat(),
                            "meta_date": found_meta,
                        })
                except Exception as e:
                    print(f"  FAIL: {e}")

        # Save verification summary
        print(f"\n=== URL date vs meta verification summary ===")
        for r in url_date_verify:
            print(f"  {r['kind']}: URL={r['url_date']}  meta={r['meta_date']}")

        browser.close()

    print("\n=== T8.2 PROBE DONE ===")
    print(f"Fixtures saved to {FIX_DIR}")


if __name__ == "__main__":
    main()
