"""Generate read-only reports from SQLite.

For each company we produce, under ``<output_root>/<COMPANY>/``:

* ``news/<YYYY>/<YYYY_MM>/<yyyy_mm_dd>_<slug>_<NN>.md`` — Obsidian-ready
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
import logging
import os
import re
import sqlite3
import shutil
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from openpyxl import Workbook

from src import db
from src.config import Config

log = logging.getLogger(__name__)

XLSX_COLUMNS = ["date", "headline", "persons", "mood"]


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
    affiliate_dir = company_dir / "affiliate"
    news_list_dir = company_dir / "news_list"

    # Idempotency: wipe just the news/ tree so ordinal numbering is stable.
    # We do NOT wipe affiliate/ or news_list/ — those are single files we
    # overwrite atomically below.
    if news_dir.exists():
        shutil.rmtree(news_dir)
    news_dir.mkdir(parents=True, exist_ok=True)
    affiliate_dir.mkdir(parents=True, exist_ok=True)
    news_list_dir.mkdir(parents=True, exist_ok=True)

    rows = list(conn.execute(
        "SELECT n.id, n.url, n.headline, n.body, n.published_at, n.mood, n.mood_reason, "
        "       s.code AS source_code "
        "FROM news n JOIN sources s ON s.id = n.source_id "
        "WHERE n.company_id = ? AND n.status = 'analyzed' "
        "ORDER BY n.published_at ASC, n.id ASC",
        (company_id,),
    ))

    md_files = 0
    xlsx_rows: list[dict] = []
    day_counter: dict[str, int] = defaultdict(int)

    for row in rows:
        pub_utc = _parse_utc(row["published_at"])
        pub_local = pub_utc.astimezone(tz)
        date_key = pub_local.strftime("%Y_%m_%d")
        day_counter[date_key] += 1
        nn = day_counter[date_key]

        persons = [
            r["full_name"]
            for r in conn.execute(
                "SELECT p.full_name FROM news_persons np JOIN persons p ON p.id = np.person_id "
                "WHERE np.news_id = ? ORDER BY p.full_name",
                (row["id"],),
            )
        ]

        md_path = _write_md(
            news_dir, pub_local, nn,
            url=row["url"],
            headline=row["headline"],
            body=row["body"] or "",
            mood=row["mood"],
            mood_reason=row["mood_reason"] or "",
            source_code=row["source_code"],
            persons=persons,
        )
        if md_path:
            md_files += 1

        xlsx_rows.append({
            "date": pub_local.strftime("%Y_%m_%d"),
            "headline": row["headline"],
            "persons": ", ".join(persons),
            "mood": row["mood"],
        })

    persons_count = _write_persons_csv(conn, company_id, affiliate_dir / "persons.csv")
    xlsx_written = _write_xlsx_atomic(news_list_dir / "data.xlsx", xlsx_rows)

    return ReportResult(
        company=company_name,
        md_files=md_files,
        persons_rows=persons_count,
        xlsx_rows=len(xlsx_rows),
        xlsx_written=xlsx_written,
    )


# ------------------------------------------------------------- writers


def _write_md(
    news_dir: Path,
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
) -> Path | None:
    year = pub_local.strftime("%Y")
    year_month = pub_local.strftime("%Y_%m")
    target_dir = news_dir / year / year_month
    target_dir.mkdir(parents=True, exist_ok=True)

    date_prefix = pub_local.strftime("%Y_%m_%d")
    slug = make_slug(headline)
    filename = f"{date_prefix}_{slug}_{nn:02d}.md"
    path = target_dir / filename

    frontmatter = _yaml_frontmatter({
        "date": pub_local.strftime("%Y-%m-%d"),
        "datetime": pub_local.isoformat(),
        "source": source_code,
        "url": url,
        "mood": mood,
        "mood_reason": mood_reason,
        "persons": persons,
    })

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
    sql = """
        SELECT
          p.full_name,
          p.status,
          p.brand,
          SUM(CASE WHEN n.mood='pos'     THEN 1 ELSE 0 END) AS pos_freq,
          SUM(CASE WHEN n.mood='neg'     THEN 1 ELSE 0 END) AS neg_freq,
          SUM(CASE WHEN n.mood='neutral' THEN 1 ELSE 0 END) AS zero_freq,
          COUNT(n.id) AS total_freq
        FROM persons p
        LEFT JOIN news_persons np ON np.person_id = p.id
        LEFT JOIN news n          ON n.id = np.news_id AND n.status='analyzed'
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


def _write_xlsx_atomic(path: Path, rows: list[dict]) -> bool:
    """Write rows to ``path`` via a temp file. Return False if the final
    replace fails because Excel has the file open."""
    wb = Workbook()
    ws = wb.active
    ws.title = "news"
    ws.append(XLSX_COLUMNS)
    for r in rows:
        ws.append([r[c] for c in XLSX_COLUMNS])

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


def make_slug(headline: str) -> str:
    """First 5 words of the headline → lowercase Cyrillic/Latin slug.

    Non-alphanumeric runs collapse to a single ``_``. Output is capped at
    60 chars to keep filesystem entries readable. Cyrillic is preserved
    — NTFS, ext4 and Obsidian all handle it natively, and Russian slugs
    stay searchable for the human reader."""
    words = re.split(r"\s+", headline.strip())[:5]
    raw = " ".join(words).lower().replace("ё", "е")
    s = _SLUG_NONWORD.sub("_", raw)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:60] or "news"


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
