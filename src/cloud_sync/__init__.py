"""Supabase Postgres one-way mirror for the local SQLite DB.

Public API:
    init_schema(db_url)  — apply DDL (idempotent).
    push_all(sqlite_path, db_url, company=None) -> PushStats — upsert local rows.

See work_directory/01_specs/05_supabase_sync_spec.md and
work_directory/02_plans/05_claude_supabase_sync_plan.md for design rationale.
"""

from src.cloud_sync.pusher import PushStats, init_schema, push_all

__all__ = ["PushStats", "init_schema", "push_all"]
