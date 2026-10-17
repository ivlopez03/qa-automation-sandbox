import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    email         TEXT NOT NULL UNIQUE,
    full_name     TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    base_score    INTEGER NOT NULL,
    cost_per_lead REAL NOT NULL
);

-- Staging table: everything that arrives, stored untouched (the "E" in ETL)
CREATE TABLE IF NOT EXISTS raw_leads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     TEXT NOT NULL,
    channel      TEXT NOT NULL CHECK (channel IN ('api', 'csv', 'feed', 'seed')),
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'processed', 'rejected', 'duplicate')),
    error        TEXT,
    lead_id      INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    received_at  TEXT NOT NULL,
    processed_at TEXT
);

-- Clean, normalized target table (the "L" in ETL)
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_lead_id     INTEGER REFERENCES raw_leads(id),
    first_name      TEXT NOT NULL,
    last_name       TEXT NOT NULL DEFAULT '',
    email           TEXT NOT NULL UNIQUE,
    phone           TEXT,
    company         TEXT,
    source_id       INTEGER NOT NULL REFERENCES sources(id),
    score           INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    estimated_value REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new', 'contacted', 'qualified', 'converted', 'lost')),
    owner_id        INTEGER REFERENCES users(id),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_leads_owner  ON leads(owner_id);
CREATE INDEX IF NOT EXISTS ix_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS ix_raw_status   ON raw_leads(status);

CREATE TABLE IF NOT EXISTS lead_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT,
    actor_id    INTEGER REFERENCES users(id),
    note        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deals (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id   INTEGER NOT NULL UNIQUE REFERENCES leads(id) ON DELETE CASCADE,
    owner_id  INTEGER REFERENCES users(id),
    amount    REAL NOT NULL CHECK (amount > 0),
    closed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS etl_jobs (
    id           TEXT PRIMARY KEY,
    job_type     TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    requested_by INTEGER REFERENCES users(id),
    total        INTEGER NOT NULL DEFAULT 0,
    processed    INTEGER NOT NULL DEFAULT 0,
    rejected     INTEGER NOT NULL DEFAULT 0,
    duplicates   INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id   INTEGER,
    action     TEXT NOT NULL,
    entity     TEXT NOT NULL,
    entity_id  TEXT,
    details    TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         TEXT PRIMARY KEY,
    status_code INTEGER NOT NULL,
    response    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

TABLES = ["idempotency_keys", "audit_log", "settings", "etl_jobs", "deals",
          "lead_events", "raw_leads", "leads", "sources", "users"]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect() -> sqlite3.Connection:
    path = config.db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextmanager
def get_conn():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)


def drop_all(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    for t in TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.execute("PRAGMA foreign_keys = ON")


def audit(conn, actor_id, action, entity, entity_id=None, details=None):
    conn.execute(
        "INSERT INTO audit_log (actor_id, action, entity, entity_id, details, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (actor_id, action, entity, None if entity_id is None else str(entity_id), details, now_iso()),
    )


def rows(cursor) -> list[dict]:
    return [dict(r) for r in cursor.fetchall()]
