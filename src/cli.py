from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

from src import db
from src.config import PROJECT_ROOT, load_config


def _setup_logging(verbose: bool = False) -> None:
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"run-{datetime.now():%Y-%m-%d}.log"

    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"

    handlers: list[logging.Handler] = [
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
    # Defense-in-depth: even with -v / DEBUG on the root logger, never let
    # httpx log request/response headers — they may contain Authorization.
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.INFO)
    # psycopg INFO max — protect against connection string / parameter leaks at DEBUG.
    logging.getLogger("psycopg").setLevel(logging.INFO)


def cmd_init_db(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    counts = db.init_db(cfg)
    print(
        f"DB initialized at {cfg.db_path}\n"
        f"  companies: {counts['companies']}\n"
        f"  sources:   {counts['sources']}\n"
        f"  persons:   {counts['persons']}"
    )
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    from src import fetcher
    cfg = load_config(args.config)
    db.ensure_migrated(cfg)
    results = fetcher.fetch_all(cfg, company_filter=args.company)
    if not results:
        print("Nothing fetched.")
        return 0
    print(f"{'company':<12} {'source':<10} {'fetched':>8} {'inserted':>9} {'errors':>7}")
    print("-" * 50)
    for r in results:
        print(f"{r.company:<12} {r.source:<10} {r.fetched:>8} {r.inserted:>9} {r.errors:>7}")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    from src import analyzer
    cfg = load_config(args.config)
    db.ensure_migrated(cfg)
    if not cfg.openai_api_key:
        print("ERROR: OPENAI_API_KEY is not set in .env — analyze cannot run.", file=sys.stderr)
        return 2
    r = analyzer.analyze_all(cfg, company_filter=args.company)
    print(
        f"analyzed: {r.analyzed}\n"
        f"errored:  {r.errored}\n"
        f"skipped (retry_count>=3): {r.skipped_maxed_out}\n"
        f"tokens:   {r.tokens_total}"
    )
    if r.aborted:
        print(
            f"\nERROR: batch aborted — {r.abort_reason}\n"
            "Fix the OpenAI config (api key / model name / permissions) and rerun.",
            file=sys.stderr,
        )
        return 3
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from src import reporter
    cfg = load_config(args.config)
    db.ensure_migrated(cfg)
    results = reporter.report_all(cfg, company_filter=args.company)
    if not results:
        print("Nothing to report.")
        return 0
    print(f"{'company':<12} {'md':>5} {'persons':>8} {'xlsx_rows':>10} {'xlsx_written':>13}")
    print("-" * 52)
    for r in results:
        print(f"{r.company:<12} {r.md_files:>5} {r.persons_rows:>8} {r.xlsx_rows:>10} {str(r.xlsx_written):>13}")
    return 0


def cmd_cycle(args: argparse.Namespace) -> int:
    from src import analyzer, fetcher, reporter
    cfg = load_config(args.config)
    db.ensure_migrated(cfg)
    log = logging.getLogger(__name__)
    log.info("manual run, auto_run=%s in config", cfg.auto_run)

    # Fail fast: don't fetch new rows we can't analyze, otherwise the user
    # ends up with status='new' rows and a cryptic traceback at the analyze step.
    if not cfg.openai_api_key:
        print("ERROR: OPENAI_API_KEY is not set in .env — cycle aborted before fetch.", file=sys.stderr)
        return 2

    fetch_results = fetcher.fetch_all(cfg, company_filter=args.company)
    analyze_result = analyzer.analyze_all(cfg, company_filter=args.company)
    report_results = reporter.report_all(cfg, company_filter=args.company)

    print(f"fetch:    inserted={sum(r.inserted for r in fetch_results)}")
    print(f"analyze:  analyzed={analyze_result.analyzed} errored={analyze_result.errored} "
          f"tokens={analyze_result.tokens_total}")
    print(f"report:   md={sum(r.md_files for r in report_results)} "
          f"xlsx_written={all(r.xlsx_written for r in report_results)}")

    # Cloud mirror: silent skip if SUPABASE_DB_URL is not configured.
    # Network or DB errors are logged but do NOT fail the cycle — the local
    # pipeline already succeeded and exit code 0 is correct.
    db_url = os.environ.get("SUPABASE_DB_URL")
    if db_url:
        try:
            from src import cloud_sync
            stats = cloud_sync.push_all(cfg.db_path, db_url, company=args.company)
            print(f"cloud:    {stats}")
        except Exception as exc:
            log.warning("cloud_sync skipped: %s: %s", type(exc).__name__, exc)
            print(f"cloud:    skipped ({type(exc).__name__})", file=sys.stderr)
    else:
        log.debug("cloud_sync: SUPABASE_DB_URL not set, skipping")
    return 0


def cmd_init_cloud_db(args: argparse.Namespace) -> int:
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set in .env.", file=sys.stderr)
        return 2
    from src import cloud_sync
    try:
        cloud_sync.init_schema(db_url)
    except Exception as exc:
        print(f"ERROR: init-cloud-db failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    print("Cloud schema applied (trading_news.*).")
    return 0


def cmd_sync_cloud(args: argparse.Namespace) -> int:
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set in .env.", file=sys.stderr)
        return 2
    cfg = load_config(args.config)
    db.ensure_migrated(cfg)
    from src import cloud_sync
    try:
        stats = cloud_sync.push_all(cfg.db_path, db_url, company=args.company)
    except Exception as exc:
        print(f"ERROR: sync-cloud failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    print(f"cloud push ok: {stats}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    db.ensure_migrated(cfg)
    rows = db.status_counts(cfg, args.company)
    if not rows:
        print("No items in DB yet.")
        return 0
    print(f"{'company':<20} {'kind':<16} {'status':<12} {'count':>8}")
    print("-" * 58)
    for r in rows:
        print(f"{r['company']:<20} {r['kind']:<16} {r['status']:<12} {r['cnt']:>8}")
    return 0


def _add_company_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--company", help="Filter by company name (default: all enabled)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading_news", description="Trading news aggregator")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-db", help="Create schema, import seed")
    p_init.set_defaults(func=cmd_init_db)

    p_fetch = sub.add_parser("fetch", help="Pull news from sources")
    _add_company_arg(p_fetch)
    p_fetch.set_defaults(func=cmd_fetch)

    p_an = sub.add_parser("analyze", help="LLM-analyze 'new' news")
    _add_company_arg(p_an)
    p_an.set_defaults(func=cmd_analyze)

    p_rep = sub.add_parser("report", help="Generate Obsidian MD + Excel + persons.csv")
    _add_company_arg(p_rep)
    p_rep.set_defaults(func=cmd_report)

    p_cyc = sub.add_parser("cycle", help="fetch -> analyze -> report -> cloud push")
    _add_company_arg(p_cyc)
    p_cyc.set_defaults(func=cmd_cycle)

    p_icd = sub.add_parser("init-cloud-db", help="Apply DDL to Supabase Postgres mirror")
    p_icd.set_defaults(func=cmd_init_cloud_db)

    p_sc = sub.add_parser("sync-cloud", help="Push local SQLite rows to Supabase")
    _add_company_arg(p_sc)
    p_sc.set_defaults(func=cmd_sync_cloud)

    p_st = sub.add_parser("status", help="Show news status counts")
    _add_company_arg(p_st)
    p_st.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Load .env early so SUPABASE_DB_URL is visible to commands that don't
    # call load_config (init-cloud-db). Config-loading commands re-call
    # load_dotenv internally; it is idempotent.
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)
