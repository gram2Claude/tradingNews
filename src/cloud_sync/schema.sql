-- Schema for Supabase Postgres mirror of the local SQLite DB.
-- All objects live in the `trading_news` schema to isolate them from other
-- workloads in the same Supabase project.
--
-- Idempotent: every CREATE uses IF NOT EXISTS. Safe to run multiple times.
-- See work_directory/01_specs/05_supabase_sync_spec.md for design notes.

CREATE SCHEMA IF NOT EXISTS trading_news;

CREATE TABLE IF NOT EXISTS trading_news.companies (
    name        TEXT PRIMARY KEY,
    start_date  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trading_news.sources (
    code      TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    base_url  TEXT NOT NULL,
    enabled   BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS trading_news.persons (
    company_name  TEXT NOT NULL REFERENCES trading_news.companies(name) ON DELETE CASCADE,
    full_name     TEXT NOT NULL,
    status        TEXT,
    brand         TEXT,
    from_seed     BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (company_name, full_name)
);

CREATE TABLE IF NOT EXISTS trading_news.news (
    source_code   TEXT NOT NULL REFERENCES trading_news.sources(code) ON DELETE CASCADE,
    url           TEXT NOT NULL,
    company_name  TEXT NOT NULL REFERENCES trading_news.companies(name) ON DELETE CASCADE,
    headline      TEXT NOT NULL,
    body          TEXT,
    published_at  TIMESTAMPTZ NOT NULL,
    fetched_at    TIMESTAMPTZ,
    mood          TEXT CHECK (mood IS NULL OR mood IN ('pos','neutral','neg')),
    mood_reason   TEXT,
    item_type     TEXT NOT NULL DEFAULT 'news'
                  CHECK (item_type IN ('news','recommendation')),
    status        TEXT NOT NULL DEFAULT 'new'
                  CHECK (status IN ('new','analyzed','error')),
    error_msg     TEXT,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    tokens_used   INTEGER,
    PRIMARY KEY (source_code, url)
);

CREATE INDEX IF NOT EXISTS idx_tn_news_company_date
    ON trading_news.news (company_name, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_tn_news_status
    ON trading_news.news (status);

CREATE TABLE IF NOT EXISTS trading_news.news_persons (
    source_code     TEXT NOT NULL,
    url             TEXT NOT NULL,
    company_name    TEXT NOT NULL,
    person_full_name TEXT NOT NULL,
    PRIMARY KEY (source_code, url, company_name, person_full_name),
    FOREIGN KEY (source_code, url)
        REFERENCES trading_news.news (source_code, url) ON DELETE CASCADE,
    FOREIGN KEY (company_name, person_full_name)
        REFERENCES trading_news.persons (company_name, full_name) ON DELETE CASCADE
);
