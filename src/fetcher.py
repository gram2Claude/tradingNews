"""Orchestrator: walks companies × enabled sources, INSERTs new news rows.

Idempotent: relies on UNIQUE(source_id, url). On dup → ignored, count not bumped.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timezone

from src import db
from src.config import Config
from src.sources.base import RawItem, Source
from src.sources.x5_ir import X5IRSource

log = logging.getLogger(__name__)


SOURCE_REGISTRY: dict[str, type[Source]] = {
    "x5_ir": X5IRSource,
}


@dataclass
class FetchResult:
    company: str
    source: str
    fetched: int       # how many RawItems source produced
    inserted: int      # how many new rows in DB
    errors: int


def fetch_all(cfg: Config, company_filter: str | None = None) -> list[FetchResult]:
    results: list[FetchResult] = []
    conn = db.connect(cfg.db_path)
    try:
        companies = _select_companies(conn, cfg, company_filter)
        if not companies:
            log.warning("no companies to fetch (filter=%r)", company_filter)
            return results

        for company_row in companies:
            company_cfg = next((c for c in cfg.companies if c.name == company_row["name"]), None)
            if not company_cfg:
                continue
            since = _resolve_since(company_cfg.start_date, cfg.start_date)
            for src_code in company_cfg.sources:
                src_cfg = cfg.sources.get(src_code)
                if not src_cfg or not src_cfg.enabled:
                    log.info("skip disabled/unknown source %s for %s", src_code, company_row["name"])
                    continue
                cls = SOURCE_REGISTRY.get(src_cfg.parser)
                if cls is None:
                    log.warning("no parser registered for code=%s", src_cfg.parser)
                    continue

                src_row = conn.execute("SELECT id FROM sources WHERE code = ?", (src_code,)).fetchone()
                if src_row is None:
                    log.warning("source %s not in DB — run init-db", src_code)
                    continue

                # Persistent HTTP client lives in the Source; close it
                # deterministically after the per-source fetch finishes.
                with cls(base_url=src_cfg.base_url) as source_instance:
                    res = _fetch_one(
                        conn,
                        company_id=company_row["id"],
                        company_name=company_row["name"],
                        source_id=src_row["id"],
                        source_code=src_code,
                        source=source_instance,
                        since=since,
                    )
                results.append(res)
                # Commit per source so a crash in source N doesn't lose
                # rows already fetched from sources 1..N-1.
                conn.commit()
    finally:
        conn.close()
    return results


def _select_companies(conn: sqlite3.Connection, cfg: Config, name: str | None) -> list[sqlite3.Row]:
    if name:
        return list(conn.execute("SELECT id, name FROM companies WHERE name = ?", (name,)))
    return list(conn.execute("SELECT id, name FROM companies"))


def _resolve_since(company_start, global_start) -> datetime:
    raw = company_start or global_start
    # YAML may parse `2026-05-01` as datetime.date — handle both.
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, date):
        return datetime.combine(raw, time.min, tzinfo=timezone.utc)
    s = str(raw)
    if len(s) == 10:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(s)


def _fetch_one(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    company_name: str,
    source_id: int,
    source_code: str,
    source: Source,
    since: datetime,
) -> FetchResult:
    fetched = 0
    inserted = 0
    errors = 0
    log.info("fetch start: company=%s source=%s since=%s", company_name, source_code, since.isoformat())
    try:
        for item in source.fetch(since):
            fetched += 1
            if _insert(conn, company_id=company_id, source_id=source_id, item=item):
                inserted += 1
    except Exception as exc:
        log.exception("fetch failed: %s/%s: %s", company_name, source_code, exc)
        errors += 1
    log.info(
        "fetch done: company=%s source=%s fetched=%d inserted=%d errors=%d",
        company_name, source_code, fetched, inserted, errors,
    )
    return FetchResult(
        company=company_name, source=source_code,
        fetched=fetched, inserted=inserted, errors=errors,
    )


def _insert(conn: sqlite3.Connection, *, company_id: int, source_id: int, item: RawItem) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO news "
        "(company_id, source_id, url, headline, body, published_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            company_id,
            source_id,
            item.url,
            item.headline,
            item.body,
            item.published_at.astimezone(timezone.utc).isoformat(),
        ),
    )
    return cur.rowcount > 0
