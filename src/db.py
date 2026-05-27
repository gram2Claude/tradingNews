from __future__ import annotations

import csv
import logging
import sqlite3
from pathlib import Path

from src.config import Config, PROJECT_ROOT

log = logging.getLogger(__name__)

SCHEMA_VERSION = 4

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS companies (
    id          INTEGER PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    start_date  TEXT,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sources (
    id        INTEGER PRIMARY KEY,
    code      TEXT UNIQUE NOT NULL,
    name      TEXT NOT NULL,
    base_url  TEXT NOT NULL,
    enabled   INTEGER DEFAULT 1
);

-- v4 (task 08, δ-completion): колонка item_type удалена. Все рекомендации
-- теперь живут в отдельной таблице recommendations; analyzer per-item dispatch
-- решает куда писать (см. analyzer._analyze_news).
CREATE TABLE IF NOT EXISTS news (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES companies(id),
    source_id     INTEGER NOT NULL REFERENCES sources(id),
    url           TEXT NOT NULL,
    headline      TEXT NOT NULL,
    body          TEXT,
    published_at  TEXT NOT NULL,
    fetched_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    mood          TEXT,
    mood_reason   TEXT,
    status        TEXT DEFAULT 'new',
    error_msg     TEXT,
    retry_count   INTEGER DEFAULT 0,
    tokens_used   INTEGER,
    UNIQUE (source_id, url)
);

CREATE INDEX IF NOT EXISTS idx_news_company_date ON news(company_id, published_at);
CREATE INDEX IF NOT EXISTS idx_news_status ON news(status);

CREATE TABLE IF NOT EXISTS persons (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES companies(id),
    full_name   TEXT NOT NULL,
    status      TEXT,
    brand       TEXT,
    from_seed   INTEGER DEFAULT 0,
    UNIQUE (company_id, full_name)
);

CREATE TABLE IF NOT EXISTS news_persons (
    news_id    INTEGER NOT NULL REFERENCES news(id) ON DELETE CASCADE,
    person_id  INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    PRIMARY KEY (news_id, person_id)
);

-- v3: торговые рекомендации хранятся в отдельной таблице.
-- После δ-completion (v4, task 08): все источники recommendation'ов пишут сюда.
-- Recommendation-only источники (lmsic) — через fetcher dispatch по
-- item_destination. Mixed-stream источники (finam) — через analyzer per-item
-- dispatch когда LLM классифицирует item как recommendation.
CREATE TABLE IF NOT EXISTS recommendations (
    id                    INTEGER PRIMARY KEY,
    company_id            INTEGER NOT NULL REFERENCES companies(id),
    source_id             INTEGER NOT NULL REFERENCES sources(id),
    url                   TEXT NOT NULL,
    headline              TEXT NOT NULL,
    body                  TEXT,
    published_at          TEXT NOT NULL,
    fetched_at            TEXT DEFAULT CURRENT_TIMESTAMP,
    mood                  TEXT,
    mood_reason           TEXT,
    target_price          REAL,
    recommendation_action TEXT,                 -- 'buy' | 'hold' | 'sell' | NULL
    potential_pct         REAL,
    multipliers_json      TEXT,                 -- '{"EV/EBITDA":4.1,"P/E":6.8,...}'
    status                TEXT DEFAULT 'new',
    error_msg             TEXT,
    retry_count           INTEGER DEFAULT 0,
    tokens_used           INTEGER,
    UNIQUE (source_id, url)
);

CREATE INDEX IF NOT EXISTS idx_recommendations_company_date ON recommendations(company_id, published_at);
CREATE INDEX IF NOT EXISTS idx_recommendations_status ON recommendations(status);

CREATE TABLE IF NOT EXISTS recommendation_persons (
    recommendation_id  INTEGER NOT NULL REFERENCES recommendations(id) ON DELETE CASCADE,
    person_id          INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    PRIMARY KEY (recommendation_id, person_id)
);

CREATE INDEX IF NOT EXISTS idx_recommendation_persons_person ON recommendation_persons(person_id);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(cfg: Config) -> dict[str, int]:
    """Create schema, seed companies/sources/persons. Idempotent.

    Returns counts: {'companies': N, 'sources': N, 'persons': N}.
    """
    conn = connect(cfg.db_path)
    try:
        # Column-presence-aware migration (codex 04 P1.4):
        # `CREATE TABLE IF NOT EXISTS` не добавляет колонки на существующих таблицах.
        # Поэтому если v1 БД уже есть, нужен ALTER. Делаем перед executescript чтобы
        # порядок был детерминированный.
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if user_version < 2:
            _migrate_to_v2(conn)

        conn.executescript(SCHEMA_SQL)
        # δ-completion v3 → v4 must run AFTER executescript так как
        # recommendations table может потребоваться создать первой (IF NOT EXISTS
        # из SCHEMA_SQL обеспечит существование), а migration двигает строки и
        # ребилдит news. На fresh DB это no-op (column item_type не существует).
        if user_version < 4:
            _migrate_to_v4(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        # Sources from config
        for code, src in cfg.sources.items():
            conn.execute(
                "INSERT INTO sources (code, name, base_url, enabled) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name, base_url=excluded.base_url, "
                "enabled=excluded.enabled",
                (code, src.name, src.base_url, int(src.enabled)),
            )

        # Companies + seed persons
        for company in cfg.companies:
            conn.execute(
                "INSERT INTO companies (name, start_date) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET start_date=excluded.start_date",
                (company.name, company.start_date),
            )
            cid = conn.execute(
                "SELECT id FROM companies WHERE name = ?", (company.name,)
            ).fetchone()["id"]

            if company.seed_persons:
                seed_file = (PROJECT_ROOT / company.seed_persons).resolve()
                if seed_file.exists():
                    _load_seed_persons(conn, cid, seed_file)
                else:
                    log.warning("seed file missing for %s: %s", company.name, seed_file)

        conn.commit()

        counts = {
            "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
            "sources": conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            "persons": conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0],
        }
        return counts
    finally:
        conn.close()


def ensure_migrated(cfg: Config) -> None:
    """Lightweight migration check. Called at start of cmd_fetch/analyze/report/cycle
    so an older DB picked up after `git pull` migrates without explicit `init-db`.

    Idempotent: PRAGMA user_version check skips no-op cases in ~1ms. Does NOT
    re-seed sources/persons (that's `init_db`'s job). Migration chain runs in
    order: v1 → v2 → v3. Each step bumps user_version inside its own transaction;
    a crash mid-step leaves user_version at the previous level, so the next call
    retries cleanly.
    """
    conn = connect(cfg.db_path)
    try:
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if user_version >= SCHEMA_VERSION:
            return
        if user_version < 2:
            _migrate_to_v2(conn)
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
        if user_version < 3:
            _migrate_to_v3(conn)
            # _migrate_to_v3 already sets user_version=3 inside its own transaction
        if user_version < 4:
            _migrate_to_v4(conn)
            # _migrate_to_v4 already sets user_version=4 inside its own transaction
    finally:
        conn.close()


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2: add news.item_type if column doesn't exist. Idempotent.

    Если таблица `news` ещё не создана (fresh DB) — выходим, последующий
    `executescript(SCHEMA_SQL)` создаст её сразу с колонкой item_type.
    """
    # Check existence of news table
    has_news = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='news'"
    ).fetchone()
    if not has_news:
        return  # fresh DB; SCHEMA_SQL создаст с item_type сразу
    # Check existence of item_type column
    columns = {row[1] for row in conn.execute("PRAGMA table_info(news)")}
    if "item_type" not in columns:
        conn.execute(
            "ALTER TABLE news ADD COLUMN item_type TEXT NOT NULL DEFAULT 'news'"
        )
        log.info("db: migrated v1 → v2 (added news.item_type)")


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """v2 → v3: add `recommendations` and `recommendation_persons` tables.

    Транзакционная и идемпотентная:
    - `CREATE TABLE IF NOT EXISTS` — повторный запуск no-op.
    - `PRAGMA user_version = 3` ставится **последним** в той же транзакции.
    - При сбое любой операции внутри — ROLLBACK откатывает всё, включая DDL.
      user_version не сдвигается, partial state не возникает,
      `ensure_migrated` починит на следующем запуске.
    - На fresh DB вызывается уже после executescript(SCHEMA_SQL) — все таблицы
      созданы, операции no-op, version выставляется идемпотентно.

    Используем SAVEPOINT вместо BEGIN/COMMIT: Python sqlite3 в legacy isolation
    mode делает implicit commit перед DDL, ломая `with conn:` для CREATE TABLE.
    SAVEPOINT уважает DDL независимо от isolation_level.
    """
    conn.execute("SAVEPOINT v3_migration")
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recommendations (
                id                    INTEGER PRIMARY KEY,
                company_id            INTEGER NOT NULL REFERENCES companies(id),
                source_id             INTEGER NOT NULL REFERENCES sources(id),
                url                   TEXT NOT NULL,
                headline              TEXT NOT NULL,
                body                  TEXT,
                published_at          TEXT NOT NULL,
                fetched_at            TEXT DEFAULT CURRENT_TIMESTAMP,
                mood                  TEXT,
                mood_reason           TEXT,
                target_price          REAL,
                recommendation_action TEXT,
                potential_pct         REAL,
                multipliers_json      TEXT,
                status                TEXT DEFAULT 'new',
                error_msg             TEXT,
                retry_count           INTEGER DEFAULT 0,
                tokens_used           INTEGER,
                UNIQUE (source_id, url)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recommendation_persons (
                recommendation_id  INTEGER NOT NULL REFERENCES recommendations(id) ON DELETE CASCADE,
                person_id          INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
                PRIMARY KEY (recommendation_id, person_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recommendations_company_date "
            "ON recommendations(company_id, published_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recommendations_status "
            "ON recommendations(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recommendation_persons_person "
            "ON recommendation_persons(person_id)"
        )
        conn.execute("PRAGMA user_version = 3")
    except Exception:
        conn.execute("ROLLBACK TO v3_migration")
        conn.execute("RELEASE v3_migration")
        raise
    else:
        conn.execute("RELEASE v3_migration")
        conn.commit()
    log.info("db: migrated v2 → v3 (added recommendations + recommendation_persons)")


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    """v3 → v4: δ-completion task 06.

    1. Перенести news.item_type='recommendation' rows → recommendations table
       (structural fields target_price/etc остаются NULL — legacy finam-recs
       не имели парсинга структурных полей).
    2. Перенести соответствующие news_persons → recommendation_persons.
    3. DELETE из news (CASCADE удалит остаточные news_persons).
    4. Rebuild news table без колонки item_type (SQLite < 3.35 не имеет
       ALTER TABLE DROP COLUMN; делаем create-new + insert + drop + rename).

    Идемпотентная (через `IF EXISTS` проверки) и атомарная (SAVEPOINT).
    На fresh БД (где news создана уже без item_type через SCHEMA_SQL) —
    операция no-op: проверка `if "item_type" not in cols` пропустит rebuild.
    """
    # Если column item_type уже отсутствует — мы здесь после нормального fresh init.
    # init_db делает PRAGMA user_version=SCHEMA_VERSION в конце, так что ensure_migrated
    # не должен сюда попасть на свежей БД. Но защищаемся: пропускаем если column нет.
    has_news = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='news'"
    ).fetchone()
    if not has_news:
        # Fresh DB up the migration chain — init_db.executescript ещё не отработал.
        # Не делаем ничего: ensure_migrated не вызывается раньше init_db обычно.
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(news)")}
    if "item_type" not in cols:
        # Уже мигрировали; просто фиксируем версию.
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        return

    # FK enforcement on DROP TABLE cascades через news_persons.news_id и стирает
    # ВСЕ junction-строки до RENAME. PRAGMA foreign_keys внутри транзакции —
    # no-op (SQLite docs), поэтому отключаем ДО SAVEPOINT'а. conn.commit() гарантирует
    # отсутствие pending tx; PRAGMA сама транзакцию не открывает.
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("SAVEPOINT v4_migration")
    try:
        # 1. Перенос finam-recs строк (item_type='recommendation') в recommendations
        conn.execute(
            """
            INSERT OR IGNORE INTO recommendations (
                company_id, source_id, url, headline, body, published_at,
                fetched_at, mood, mood_reason, status, error_msg, retry_count,
                tokens_used, target_price, recommendation_action,
                potential_pct, multipliers_json
            )
            SELECT
                company_id, source_id, url, headline, body, published_at,
                fetched_at, mood, mood_reason, status, error_msg, retry_count,
                tokens_used, NULL, NULL, NULL, NULL
            FROM news WHERE item_type = 'recommendation'
            """
        )

        # 2. Перенос junction rows
        conn.execute(
            """
            INSERT OR IGNORE INTO recommendation_persons (recommendation_id, person_id)
            SELECT r.id, np.person_id
            FROM news_persons np
            JOIN news n ON n.id = np.news_id
            JOIN recommendations r
              ON r.source_id = n.source_id AND r.url = n.url
            WHERE n.item_type = 'recommendation'
            """
        )

        # 3. Explicit cleanup junction строк для удаляемых rec-news. FK CASCADE
        # сейчас выключен (см. PRAGMA выше), полагаться на каскад нельзя.
        conn.execute(
            "DELETE FROM news_persons WHERE news_id IN "
            "(SELECT id FROM news WHERE item_type = 'recommendation')"
        )
        conn.execute("DELETE FROM news WHERE item_type = 'recommendation'")

        # 4. Rebuild news без item_type column.
        # SQLite < 3.35 не имеет ALTER TABLE DROP COLUMN. Хак: create + copy + drop + rename.
        # Note: FK references на news.id остаются validными т.к. внутри транзакции
        # rowid'ы переезжают через INSERT INTO news_new SELECT id, ... — id сохраняется
        # как PRIMARY KEY column. news_persons.news_id продолжает указывать на правильные id.
        conn.execute(
            """
            CREATE TABLE news_new (
                id            INTEGER PRIMARY KEY,
                company_id    INTEGER NOT NULL REFERENCES companies(id),
                source_id     INTEGER NOT NULL REFERENCES sources(id),
                url           TEXT NOT NULL,
                headline      TEXT NOT NULL,
                body          TEXT,
                published_at  TEXT NOT NULL,
                fetched_at    TEXT DEFAULT CURRENT_TIMESTAMP,
                mood          TEXT,
                mood_reason   TEXT,
                status        TEXT DEFAULT 'new',
                error_msg     TEXT,
                retry_count   INTEGER DEFAULT 0,
                tokens_used   INTEGER,
                UNIQUE (source_id, url)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO news_new (
                id, company_id, source_id, url, headline, body, published_at,
                fetched_at, mood, mood_reason, status, error_msg,
                retry_count, tokens_used
            )
            SELECT
                id, company_id, source_id, url, headline, body, published_at,
                fetched_at, mood, mood_reason, status, error_msg,
                retry_count, tokens_used
            FROM news
            """
        )
        # Table swap. С foreign_keys=ON, DROP TABLE news каскадно стёр бы все
        # news_persons (FK action на parent drop); поэтому FK отключен ВЫШЕ
        # до открытия SAVEPOINT'а (PRAGMA внутри транзакции — no-op).
        # news_persons.news_id остаётся валидным т.к. id колонка сохранена
        # через INSERT INTO news_new SELECT id, ... выше.
        conn.execute("DROP TABLE news")
        conn.execute("ALTER TABLE news_new RENAME TO news")
        # Пересоздать indices (они были на старой news, не переехали)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_news_company_date "
            "ON news(company_id, published_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_news_status ON news(status)"
        )
        conn.execute("PRAGMA user_version = 4")
    except Exception:
        conn.execute("ROLLBACK TO v4_migration")
        conn.execute("RELEASE v4_migration")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        raise
    else:
        conn.execute("RELEASE v4_migration")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
    log.info("db: migrated v3 → v4 (dropped news.item_type, moved finam-recs to recommendations)")


def _load_seed_persons(conn: sqlite3.Connection, company_id: int, csv_path: Path) -> None:
    with csv_path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            conn.execute(
                "INSERT INTO persons (company_id, full_name, status, brand, from_seed) "
                "VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT(company_id, full_name) DO UPDATE SET "
                "status=excluded.status, brand=excluded.brand, from_seed=1",
                (company_id, row["full_name"], row.get("status"), row.get("brand")),
            )


def status_counts(cfg: Config, company: str | None = None) -> list[sqlite3.Row]:
    """Return counts by status across both `news` and `recommendations` tables.

    Each row has columns: company, kind, status, cnt.
    `kind` ∈ {'news', 'recommendations'}. Optionally filter by company name.
    """
    conn = connect(cfg.db_path)
    try:
        sql = (
            "SELECT c.name AS company, 'news' AS kind, n.status AS status, COUNT(*) AS cnt "
            "FROM news n JOIN companies c ON c.id = n.company_id "
        )
        sql_recs = (
            "SELECT c.name AS company, 'recommendations' AS kind, r.status AS status, COUNT(*) AS cnt "
            "FROM recommendations r JOIN companies c ON c.id = r.company_id "
        )
        params: tuple = ()
        if company:
            sql += "WHERE c.name = ? "
            sql_recs += "WHERE c.name = ? "
            params = (company, company)
        sql += "GROUP BY c.name, n.status "
        sql_recs += "GROUP BY c.name, r.status "
        full = f"{sql} UNION ALL {sql_recs} ORDER BY company, kind, status"
        return list(conn.execute(full, params))
    finally:
        conn.close()
