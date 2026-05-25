"""Generate read-only reports from SQLite.

For each company we produce, under ``<output_root>/<COMPANY>/``:

* ``news/<YYYY>/<YYYY_MM>/<yyyy_mm_dd>_<src>_<slug>_<NN>.md`` — Obsidian-ready
  Markdown with a YAML frontmatter. ``<NN>`` is the 1-based ordinal of the
  article within its publication day.
* ``affiliate/persons.csv`` — aggregated mood frequencies per seed person.
* ``news_list/data.xlsx`` — flat news table for review in Excel.

Dates are converted from UTC (storage) into the configured display timezone
(``config.yaml → global.timezone``, default ``Europe/Moscow``) before being
used in paths and filenames; the trading day must follow Moscow, not UTC.

Excel is written via a temp file + ``os.replace`` so an open workbook in
Excel survives the regeneration cycle (the replace fails atomically and we
log a warning instead of crashing).
"""

from __future__ import annotations

import csv
import html as html_module
import logging
import os
import re
import sqlite3
import shutil
import tempfile

from src.text_cleanup import clean_text
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook

from src import db
from src.config import Config

log = logging.getLogger(__name__)

XLSX_COLUMNS_NEWS = ["date", "headline", "persons", "mood", "item_type"]
# Recommendations sheet расширен структурными полями (multipliers_json как plain text)
XLSX_COLUMNS_RECS = [
    "date", "headline", "persons", "mood", "source",
    "target_price", "recommendation_action", "potential_pct", "multipliers",
]
# Backward-compat alias для существующих тестов
XLSX_COLUMNS = XLSX_COLUMNS_NEWS

# Короткие алиасы источников для имени файла. `corp` — официальный сайт компании
# (источник её собственных пресс-релизов); остальные — 3-буквенные сокращения.
_SOURCE_SLUG_ALIAS: dict[str, str] = {
    "x5_ir": "corp",
    "rbc": "rbc",
    "finam": "fnm",
}


def _source_slug(source_code: str) -> str:
    """Алиас для имени файла. Fallback: первые 3 символа кода (lowercase, alnum-only)."""
    if source_code in _SOURCE_SLUG_ALIAS:
        return _SOURCE_SLUG_ALIAS[source_code]
    cleaned = "".join(ch for ch in source_code.lower() if ch.isalnum())
    return (cleaned[:3] or "src")


@dataclass
class ReportResult:
    company: str
    md_files: int
    persons_rows: int
    xlsx_rows: int
    xlsx_written: bool   # False if Excel had the file locked


def report_all(cfg: Config, company_filter: str | None = None) -> list[ReportResult]:
    tz = ZoneInfo(cfg.timezone)
    out: list[ReportResult] = []

    conn = db.connect(cfg.db_path)
    try:
        if company_filter:
            companies = list(conn.execute(
                "SELECT id, name FROM companies WHERE name = ?",
                (company_filter,),
            ))
        else:
            companies = list(conn.execute("SELECT id, name FROM companies"))

        for c in companies:
            r = _report_company(conn, cfg.output_root, c["id"], c["name"], tz)
            out.append(r)
            log.info(
                "report: %s md=%d persons=%d xlsx=%d written=%s",
                r.company, r.md_files, r.persons_rows, r.xlsx_rows, r.xlsx_written,
            )
    finally:
        conn.close()
    return out


# ----------------------------------------------------------- per company


def _report_company(
    conn: sqlite3.Connection,
    output_root: Path,
    company_id: int,
    company_name: str,
    tz: ZoneInfo,
) -> ReportResult:
    company_dir = output_root / company_name
    news_dir = company_dir / "news"
    rec_dir = company_dir / "recommendations"
    affiliate_dir = company_dir / "affiliate"
    news_list_dir = company_dir / "news_list"

    # Idempotency: wipe both news/ AND recommendations/ trees so ordinal numbering
    # is stable AND items reclassified between types don't leave stale orphans.
    # We do NOT wipe affiliate/ or news_list/ — those are single files we
    # overwrite atomically below.
    for d in (news_dir, rec_dir):
        if d.exists():
            try:
                shutil.rmtree(d)
            except PermissionError as exc:
                log.warning(
                    "report: cannot wipe %s — likely held by another process "
                    "(Obsidian?): %s. Continuing without clean-slate; filenames "
                    "may collide with existing ones.", d, exc,
                )
    # mkdir only news_dir eagerly; recommendations/ created on-demand only if
    # we have recommendation rows (codex 04 P2.3 — don't create empty folders).
    news_dir.mkdir(parents=True, exist_ok=True)
    affiliate_dir.mkdir(parents=True, exist_ok=True)
    news_list_dir.mkdir(parents=True, exist_ok=True)

    # UNION ALL: news (включая finam-recs через item_type) + recommendations.
    # Deterministic tie-break: published_at, _src_table, source_code, url
    # (codex 06 P1.3 — без него nn нестабилен при коллизии timestamp'ов).
    # Новые структурные поля (target_price, ...) у news всегда NULL.
    rows = list(conn.execute(
        """
        SELECT n.id AS id, n.url AS url, n.headline AS headline, n.body AS body,
               n.published_at AS published_at, n.mood AS mood, n.mood_reason AS mood_reason,
               n.item_type AS item_type, s.code AS source_code,
               NULL AS target_price, NULL AS recommendation_action,
               NULL AS potential_pct, NULL AS multipliers_json,
               'news' AS _src_table
        FROM news n JOIN sources s ON s.id = n.source_id
        WHERE n.company_id = :cid AND n.status = 'analyzed'
        UNION ALL
        SELECT r.id AS id, r.url AS url, r.headline AS headline, r.body AS body,
               r.published_at AS published_at, r.mood AS mood, r.mood_reason AS mood_reason,
               'recommendation' AS item_type, s.code AS source_code,
               r.target_price AS target_price, r.recommendation_action AS recommendation_action,
               r.potential_pct AS potential_pct, r.multipliers_json AS multipliers_json,
               'recommendations' AS _src_table
        FROM recommendations r JOIN sources s ON s.id = r.source_id
        WHERE r.company_id = :cid AND r.status = 'analyzed'
        ORDER BY published_at ASC, _src_table ASC, source_code ASC, url ASC
        """,
        {"cid": company_id},
    ))

    md_files = 0
    news_xlsx_rows: list[dict] = []
    rec_xlsx_rows: list[dict] = []
    # Day counter scoped per (folder, day) — folder ∈ {'news', 'recommendations'}.
    day_counter: dict[tuple[str, str], int] = defaultdict(int)

    for row in rows:
        pub_utc = _parse_utc(row["published_at"])
        pub_local = pub_utc.astimezone(tz)
        date_key = pub_local.strftime("%Y_%m_%d")
        item_type = row["item_type"] or "news"
        # Route к нужной папке: 'news' если item_type='news', иначе 'recommendations'.
        # Покрывает оба источника recs: legacy news.item_type='recommendation' (finam)
        # и новую таблицу recommendations.
        folder = "news" if item_type == "news" else "recommendations"
        day_counter[(folder, date_key)] += 1
        nn = day_counter[(folder, date_key)]

        # Persons-link диспатчим по _src_table — две разные junction-таблицы.
        if row["_src_table"] == "news":
            persons_query = (
                "SELECT p.full_name FROM news_persons np JOIN persons p ON p.id = np.person_id "
                "WHERE np.news_id = ? ORDER BY p.full_name"
            )
        else:
            persons_query = (
                "SELECT p.full_name FROM recommendation_persons rp "
                "JOIN persons p ON p.id = rp.person_id "
                "WHERE rp.recommendation_id = ? ORDER BY p.full_name"
            )
        persons = [r["full_name"] for r in conn.execute(persons_query, (row["id"],))]

        headline_clean = html_module.unescape(row["headline"])
        body_clean = clean_text(row["body"])

        target_root = rec_dir if folder == "recommendations" else news_dir
        # Optional structural extras для frontmatter (NULL-keys будут отброшены
        # в _write_md — codex 06 P2.4).
        extras = {
            "target_price": row["target_price"],
            "recommendation_action": row["recommendation_action"],
            "potential_pct": row["potential_pct"],
            "multipliers_json": row["multipliers_json"],
        }
        md_path = _write_md(
            target_root, pub_local, nn,
            url=row["url"],
            headline=headline_clean,
            body=body_clean,
            mood=row["mood"],
            mood_reason=row["mood_reason"] or "",
            source_code=row["source_code"],
            persons=persons,
            item_type=item_type,
            extras=extras,
        )
        if md_path:
            md_files += 1

        date_str = pub_local.strftime("%Y_%m_%d")
        headline_for_xlsx = headline_clean
        persons_str = ", ".join(persons)
        if folder == "news":
            news_xlsx_rows.append({
                "date": date_str,
                "headline": headline_for_xlsx,
                "persons": persons_str,
                "mood": row["mood"],
                "item_type": item_type,
            })
        else:
            rec_xlsx_rows.append({
                "date": date_str,
                "headline": headline_for_xlsx,
                "persons": persons_str,
                "mood": row["mood"],
                "source": row["source_code"],
                "target_price": row["target_price"],
                "recommendation_action": row["recommendation_action"],
                "potential_pct": row["potential_pct"],
                "multipliers": row["multipliers_json"],
            })

    persons_count = _write_persons_csv(conn, company_id, affiliate_dir / "persons.csv")
    xlsx_written = _write_xlsx_atomic(
        news_list_dir / "data.xlsx",
        news_rows=news_xlsx_rows,
        rec_rows=rec_xlsx_rows,
    )
    xlsx_total = len(news_xlsx_rows) + len(rec_xlsx_rows)

    return ReportResult(
        company=company_name,
        md_files=md_files,
        persons_rows=persons_count,
        xlsx_rows=xlsx_total,
        xlsx_written=xlsx_written,
    )


# ------------------------------------------------------------- writers


def _write_md(
    root_dir: Path,
    pub_local: datetime,
    nn: int,
    *,
    url: str,
    headline: str,
    body: str,
    mood: str,
    mood_reason: str,
    source_code: str,
    persons: list[str],
    item_type: str = "news",
    extras: dict | None = None,
) -> Path | None:
    year = pub_local.strftime("%Y")
    year_month = pub_local.strftime("%Y_%m")
    target_dir = root_dir / year / year_month
    target_dir.mkdir(parents=True, exist_ok=True)

    date_prefix = pub_local.strftime("%Y_%m_%d")
    slug = make_slug(mood_reason or headline, max_chars=50)
    src_slug = _source_slug(source_code)
    filename = f"{date_prefix}_{src_slug}_{slug}_{nn:02d}.md"
    path = target_dir / filename

    frontmatter_data: dict = {
        "date": pub_local.strftime("%Y-%m-%d"),
        "datetime": pub_local.isoformat(),
        "source": source_code,
        "url": url,
        "mood": mood,
        "mood_reason": mood_reason,
        "item_type": item_type,
        "persons": persons,
    }
    # Структурные extras (target_price, ...) — добавляем ТОЛЬКО непустые
    # (codex 06 P2.4: ключи с None не должны появляться в frontmatter).
    if extras:
        for k, v in extras.items():
            if v is not None and v != "":
                frontmatter_data[k] = v

    frontmatter = _yaml_frontmatter(frontmatter_data)

    path.write_text(
        f"{frontmatter}\n# {headline}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _write_persons_csv(
    conn: sqlite3.Connection,
    company_id: int,
    path: Path,
) -> int:
    # CTE c UNION ALL — упоминания персон в news и в recommendations считаются
    # вместе. После γ-completion (когда finam-recs мигрируют) CTE сократится
    # до single source.
    sql = """
        WITH all_mentions AS (
            SELECT np.person_id AS person_id, n.mood AS mood
            FROM news_persons np
            JOIN news n ON n.id = np.news_id
            WHERE n.status = 'analyzed'
            UNION ALL
            SELECT rp.person_id AS person_id, r.mood AS mood
            FROM recommendation_persons rp
            JOIN recommendations r ON r.id = rp.recommendation_id
            WHERE r.status = 'analyzed'
        )
        SELECT
          p.full_name,
          p.status,
          p.brand,
          SUM(CASE WHEN m.mood='pos'     THEN 1 ELSE 0 END) AS pos_freq,
          SUM(CASE WHEN m.mood='neg'     THEN 1 ELSE 0 END) AS neg_freq,
          SUM(CASE WHEN m.mood='neutral' THEN 1 ELSE 0 END) AS zero_freq,
          SUM(CASE WHEN m.person_id IS NOT NULL THEN 1 ELSE 0 END) AS total_freq
        FROM persons p
        LEFT JOIN all_mentions m ON m.person_id = p.id
        WHERE p.company_id = ?
        GROUP BY p.id
        ORDER BY total_freq DESC, p.full_name ASC
    """
    rows = list(conn.execute(sql, (company_id,)))

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "person", "status", "brand",
            "pos_freq", "neg_freq", "zero_freq", "total_freq",
        ])
        for r in rows:
            writer.writerow([
                r["full_name"], r["status"] or "", r["brand"] or "",
                r["pos_freq"], r["neg_freq"], r["zero_freq"], r["total_freq"],
            ])
    os.replace(tmp, path)
    return len(rows)


def _write_xlsx_atomic(
    path: Path,
    *,
    news_rows: list[dict] | None = None,
    rec_rows: list[dict] | None = None,
    rows: list[dict] | None = None,  # legacy kwarg для backward-compat
) -> bool:
    """Write two-sheet workbook (news + recommendations) to ``path`` via temp.
    Return False if the final replace fails because Excel has the file open.

    Behavior change vs v2: первый лист 'news' содержит только news.item_type='news'
    (раньше — все строки включая finam-recs). Recommendations теперь на втором
    листе с дополнительными колонками target_price/recommendation_action/etc.
    Документировано в commit message + spec 06 P2.5.
    """
    # Legacy single-arg path (используется test_reporter.py существующими тестами)
    if rows is not None and news_rows is None and rec_rows is None:
        news_rows = rows
        rec_rows = []
    news_rows = news_rows or []
    rec_rows = rec_rows or []

    wb = Workbook()
    ws_news = wb.active
    ws_news.title = "news"
    ws_news.append(XLSX_COLUMNS_NEWS)
    for r in news_rows:
        ws_news.append([r.get(c) for c in XLSX_COLUMNS_NEWS])

    ws_recs = wb.create_sheet("recommendations")
    ws_recs.append(XLSX_COLUMNS_RECS)
    for r in rec_rows:
        ws_recs.append([r.get(c) for c in XLSX_COLUMNS_RECS])

    tmp_fd, tmp_name = tempfile.mkstemp(prefix="data_", suffix=".xlsx", dir=str(path.parent))
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    wb.save(tmp_path)

    try:
        os.replace(tmp_path, path)
        return True
    except PermissionError as exc:
        log.warning(
            "xlsx: cannot replace %s — looks like Excel has it open (%s). "
            "Wrote temp file to %s — close the workbook and rerun report.",
            path, exc, tmp_path,
        )
        return False


# ---------------------------------------------------------- helpers


_SLUG_NONWORD = re.compile(r"[^а-яёa-z0-9]+", re.IGNORECASE)


def make_slug(text: str, max_chars: int = 60, max_words: int = 5) -> str:
    """Lowercase Cyrillic/Latin slug из первых ``max_words`` слов.

    Non-alphanumeric runs collapse to a single ``_``. Output обрезается до
    ``max_chars`` и аккуратно подрезается до последнего полного слова
    (исключает «хвостовые» обрезанные токены в имени файла). Cyrillic
    preserved — NTFS, ext4, Obsidian handle natively.

    HTML-entities (&quot;, &amp;, &#39;, etc) разэскейпиваются перед
    нарезкой — иначе токен `quot` остаётся в имени файла."""
    text = html_module.unescape(text)
    words = re.split(r"\s+", text.strip())[:max_words]
    raw = " ".join(words).lower().replace("ё", "е")
    s = _SLUG_NONWORD.sub("_", raw)
    s = re.sub(r"_+", "_", s).strip("_")
    if len(s) > max_chars:
        cut = s[:max_chars]
        # Подрезаем до последнего `_` (полного слова), если он есть и не в самом начале
        last_us = cut.rfind("_")
        if last_us > max_chars // 2:
            cut = cut[:last_us]
        s = cut.strip("_")
    return s or "news"


def _yaml_frontmatter(data: dict) -> str:
    """Emit a minimal YAML frontmatter. Strings are quoted iff they contain
    special characters; lists are flow-style ``[a, b]``."""
    lines = ["---"]
    for k, v in data.items():
        if isinstance(v, list):
            inner = ", ".join(_yaml_quote(x) for x in v)
            lines.append(f"{k}: [{inner}]")
        else:
            lines.append(f"{k}: {_yaml_quote(v)}")
    lines.append("---")
    return "\n".join(lines)


_SAFE_RE = re.compile(r"^[\w\-:.+/]+$")


def _yaml_quote(v) -> str:
    if v is None:
        return ""
    s = str(v)
    if not s:
        return '""'
    if _SAFE_RE.match(s):
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _parse_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
