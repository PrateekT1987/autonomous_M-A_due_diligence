"""Persistence layer with a SQLite default and PostgreSQL for production.

The public API (`q`, `q1`, `x`, `insert`, `tx`, `audit`) is deliberately small so
the agents and API never depend on a driver. SQLite keeps the local demo and the
test suite dependency-free; setting ``DATABASE_URL`` switches every call to
PostgreSQL, including the durable job queue.

SQL is written with ``?`` placeholders (SQLite style). The PostgreSQL adapter
translates them to ``%s`` and uses dict rows, so call sites stay portable.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable

from . import config
from .config import DB_PATH

try:  # Optional: only required when DATABASE_URL points at PostgreSQL.
    import psycopg
    from psycopg.rows import dict_row
except Exception:  # pragma: no cover - SQLite-only installs
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]

_local = threading.local()
_init_lock = threading.Lock()

IS_POSTGRES = config.DATABASE_URL.startswith(("postgres://", "postgresql://"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    email TEXT,
    name TEXT,
    role TEXT NOT NULL DEFAULT 'analyst',
    created_at TEXT NOT NULL,
    last_seen_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(tenant_id, email);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    csrf TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS auth_states (
    state TEXT PRIMARY KEY,
    nonce TEXT NOT NULL,
    verifier TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deals (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    name TEXT NOT NULL,
    company TEXT,
    ticker TEXT,
    cik TEXT,
    deal_type TEXT,
    sector TEXT,
    client TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    config TEXT NOT NULL DEFAULT '{}',
    summary TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_deals_tenant ON deals(tenant_id, updated_at);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT NOT NULL,
    deal_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    publisher TEXT,
    url TEXT,
    published_at TEXT,
    form TEXT,
    accession TEXT,
    locator TEXT,
    content TEXT,
    meta TEXT NOT NULL DEFAULT '{}',
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (deal_id, id)
);
CREATE INDEX IF NOT EXISTS idx_sources_deal ON sources(deal_id);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT NOT NULL,
    deal_id TEXT NOT NULL,
    source_id TEXT,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    value TEXT,
    numeric REAL,
    unit TEXT,
    period TEXT,
    quote TEXT,
    locator TEXT,
    confidence REAL NOT NULL DEFAULT 0.8,
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    PRIMARY KEY (deal_id, id)
);
CREATE INDEX IF NOT EXISTS idx_evidence_deal ON evidence(deal_id);
CREATE INDEX IF NOT EXISTS idx_evidence_kind ON evidence(deal_id, kind);

CREATE TABLE IF NOT EXISTS risks (
    id TEXT NOT NULL,
    deal_id TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    severity INTEGER NOT NULL,
    likelihood INTEGER NOT NULL,
    score INTEGER NOT NULL,
    exposure TEXT,
    mitigants TEXT NOT NULL DEFAULT '[]',
    questions TEXT NOT NULL DEFAULT '[]',
    evidence_ids TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.7,
    provenance TEXT NOT NULL DEFAULT 'rule',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    PRIMARY KEY (deal_id, id)
);
CREATE INDEX IF NOT EXISTS idx_risks_deal ON risks(deal_id);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT NOT NULL,
    deal_id TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    version INTEGER NOT NULL DEFAULT 1,
    sections TEXT NOT NULL,
    trust TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (deal_id, id)
);
CREATE INDEX IF NOT EXISTS idx_documents_deal ON documents(deal_id);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    deal_id TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT,
    progress REAL NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'full',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    trace_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_deal ON runs(deal_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_run ON runs(deal_id) WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    deal_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    stage TEXT,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    trace_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON run_events(run_id, id);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    section TEXT,
    ordinal INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    embedding BLOB,
    tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chunks_deal ON chunks(deal_id);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id TEXT NOT NULL,
    doc_id TEXT,
    action TEXT NOT NULL,
    actor TEXT,
    note TEXT,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id TEXT NOT NULL,
    run_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    polish INTEGER,
    available_at TEXT NOT NULL,
    locked_by TEXT,
    locked_at TEXT,
    heartbeat_at TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    trace_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, available_at, id);
CREATE INDEX IF NOT EXISTS idx_jobs_deal ON jobs(deal_id, status);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    actor TEXT,
    action TEXT NOT NULL,
    deal_id TEXT,
    run_id TEXT,
    request_id TEXT,
    trace_id TEXT,
    ip TEXT,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_deal ON audit_events(deal_id, id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events(action, id);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_events(tenant_id, id);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    tenant_id TEXT DEFAULT 'default',
    deal_id TEXT,
    run_id TEXT,
    provider TEXT,
    model TEXT,
    purpose TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 1,
    error TEXT,
    request_hash TEXT,
    response_hash TEXT,
    trace_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_run ON llm_calls(run_id, id);

CREATE TABLE IF NOT EXISTS document_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    run_id TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    sections TEXT NOT NULL,
    trust TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    reviewer_comments TEXT NOT NULL DEFAULT '{}',
    change_summary TEXT NOT NULL DEFAULT '',
    previous_sections TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_doc_versions ON document_versions(deal_id, doc_id, version DESC);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    deal_id TEXT,
    response TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def loads(value: str | None, default: Any = None):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ connection
class _PGConnection:
    """Thin adapter: ``?`` placeholders plus the methods used by ``tx()``."""

    def __init__(self, raw) -> None:
        self.raw = raw

    def execute(self, sql: str, params: Iterable[Any] = ()):
        return self.raw.execute(sql.replace("?", "%s"), tuple(params))

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()


def _pg_connect():
    if psycopg is None:
        raise RuntimeError(
            "DATABASE_URL points at PostgreSQL but psycopg is not installed; "
            "install requirements.lock"
        )
    raw = psycopg.connect(config.DATABASE_URL, row_factory=dict_row)
    return _PGConnection(raw)


def conn():
    c = getattr(_local, "conn", None)
    if c is None:
        if IS_POSTGRES:
            c = _pg_connect()
        else:
            c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
        _local.conn = c
    return c


def _pg_schema() -> str:
    return (
        SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
        .replace(" embedding BLOB", " embedding BYTEA")
    )


def _statements(schema: str) -> tuple[list[str], list[str]]:
    tables: list[str] = []
    indexes: list[str] = []
    for raw in schema.split(";"):
        statement = raw.strip()
        if not statement:
            continue
        (indexes if statement.upper().startswith("CREATE INDEX") or statement.upper().startswith("CREATE UNIQUE INDEX") else tables).append(statement)
    return tables, indexes


def _run_schema(c, schema: str) -> None:
    tables, _ = _statements(schema)
    for statement in tables:
        c.execute(statement)


def _run_indexes(c, schema: str) -> None:
    _, indexes = _statements(schema)
    for statement in indexes:
        c.execute(statement)


def init_db() -> None:
    with _init_lock:
        c = conn()
        schema = _pg_schema() if IS_POSTGRES else SCHEMA
        if not IS_POSTGRES:
            _migrate_composite_keys(c)
        # Tables first, then additive columns, then indexes. This ordering lets
        # an older database gain tenant columns before an index references them.
        _run_schema(c, schema)
        _ensure_columns(c)
        _run_indexes(c, schema)
        # Seeding is idempotent and keeps a usable default tenant for local runs.
        if IS_POSTGRES:
            c.execute(
                "INSERT INTO tenants (id, name, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT (id) DO NOTHING",
                ("default", "Default", now()),
            )
        else:
            c.execute(
                "INSERT OR IGNORE INTO tenants (id, name, created_at) VALUES (?, ?, ?)",
                ("default", "Default", now()),
            )
        c.commit()


def _ensure_columns(c) -> None:
    """Additive migrations for deployments created before a column existed."""
    wanted = {
        "runs": {"cancel_requested": "INTEGER NOT NULL DEFAULT 0", "trace_id": "TEXT"},
        "deals": {"archived_at": "TEXT", "tenant_id": "TEXT NOT NULL DEFAULT 'default'"},
        "documents": {"run_id": "TEXT"},
        "jobs": {"polish": "INTEGER", "trace_id": "TEXT"},
        "run_events": {"trace_id": "TEXT"},
        "llm_calls": {"tenant_id": "TEXT DEFAULT 'default'", "trace_id": "TEXT"},
        "audit_events": {"tenant_id": "TEXT NOT NULL DEFAULT 'default'", "trace_id": "TEXT"},
        "document_versions": {"reviewer_comments": "TEXT NOT NULL DEFAULT '{}'", "change_summary": "TEXT NOT NULL DEFAULT ''", "previous_sections": "TEXT NOT NULL DEFAULT '{}'"},
        "risks": {"created_at": "TEXT NOT NULL DEFAULT ''"},
    }
    if IS_POSTGRES:
        for table, columns in wanted.items():
            for name, ddl in columns.items():
                c.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl}")
        c.commit()
        return

    for table, columns in wanted.items():
        existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, ddl in columns.items():
            if name not in existing:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    c.commit()


_COMPOSITE_TABLES = ("sources", "evidence", "risks", "documents")


def _migrate_composite_keys(c: sqlite3.Connection) -> None:
    """Upgrade older databases from a global `id` PK to (deal_id, id).

    IDs are allocated per deal, so a global primary key collides as soon as a
    second deal exists. The rebuild is lossless and only runs once.
    """
    legacy = []
    for table in _COMPOSITE_TABLES:
        row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if row and "PRIMARY KEY (deal_id" not in (row[0] or ""):
            legacy.append((table, row[0]))
    if not legacy:
        return

    for table, ddl in legacy:
        columns = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
        c.execute(f"ALTER TABLE {table} RENAME TO {table}__legacy")
        new_ddl = re.sub(
            rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);",
            lambda m: m.group(0).replace(f"CREATE TABLE IF NOT EXISTS {table}", f"CREATE TABLE {table}__new"),
            SCHEMA,
            flags=re.S,
        )
        m = re.search(rf"CREATE TABLE {table}__new \(.*?\n\);", new_ddl, re.S)
        if not m:
            raise RuntimeError(f"Could not build migration DDL for {table}")
        c.execute(m.group(0))
        cols = ", ".join(columns)
        c.execute(f"INSERT INTO {table}__new ({cols}) SELECT {cols} FROM {table}__legacy")
        c.execute(f"DROP TABLE {table}__legacy")
        c.execute(f"ALTER TABLE {table}__new RENAME TO {table}")
    c.commit()


@contextmanager
def tx():
    c = conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


def _read(sql: str, params: Iterable[Any]):
    c = conn()
    row = c.execute(sql, tuple(params))
    return row


def q(sql: str, params: Iterable[Any] = ()) -> list[Any]:
    c = conn()
    rows = c.execute(sql, tuple(params)).fetchall()
    if IS_POSTGRES:
        c.commit()  # keep the pooled connection out of idle transactions
    return rows


def q1(sql: str, params: Iterable[Any] = ()) -> Any:
    c = conn()
    row = c.execute(sql, tuple(params)).fetchone()
    if IS_POSTGRES:
        c.commit()
    return row


def x(sql: str, params: Iterable[Any] = ()) -> None:
    """Single write with a short retry on lock/deadlock contention."""
    for attempt in range(3):
        try:
            with tx() as c:
                c.execute(sql, tuple(params))
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 2:
                raise
            time.sleep(0.15 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001 - PostgreSQL concurrency errors
            name = type(exc).__name__
            if name not in {"DeadlockDetected", "SerializationFailure", "OperationalError"} or attempt == 2:
                raise
            time.sleep(0.15 * (attempt + 1))


def insert(table: str, row: dict) -> None:
    cols = ", ".join(row.keys())
    marks = ", ".join("?" for _ in row)
    x(f"INSERT INTO {table} ({cols}) VALUES ({marks})", row.values())


def next_id(deal_id: str, table: str, prefix: str, width: int = 4) -> str:
    row = q1(f"SELECT id FROM {table} WHERE deal_id = ? ORDER BY id DESC LIMIT 1", (deal_id,))
    n = 1
    if row:
        try:
            n = int(str(row["id"]).split("-")[-1]) + 1
        except ValueError:
            n = len(q(f"SELECT id FROM {table} WHERE deal_id = ?", (deal_id,))) + 1
    return f"{prefix}-{n:0{width}d}"


def audit(
    action: str,
    *,
    actor: str | None = None,
    tenant_id: str | None = None,
    deal_id: str | None = None,
    run_id: str | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
    ip: str | None = None,
    detail: dict | None = None,
) -> None:
    """Append-only operational audit event; never raises into the request path."""
    try:
        insert(
            "audit_events",
            {
                "ts": now(),
                "tenant_id": (tenant_id or "default")[:80],
                "actor": (actor or "system")[:120],
                "action": action,
                "deal_id": deal_id,
                "run_id": run_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "ip": ip,
                "detail": dumps(detail or {}),
            },
        )
    except Exception:  # noqa: BLE001 - auditing must not break the operation
        pass
