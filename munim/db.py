"""SQLite storage: cases, the audit trail, evidence, the payment ledger and the outbox."""

import json
import sqlite3
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    source_name TEXT,
    raw         TEXT NOT NULL,
    step        TEXT NOT NULL,          -- next step to run; 'done' when finished
    status      TEXT NOT NULL,          -- RUNNING, PAID, DUPLICATE, HOLD, REJECTED, ERROR
    state       TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id  TEXT NOT NULL,
    ts       TEXT NOT NULL,
    step     TEXT,
    level    TEXT,
    message  TEXT,
    evidence TEXT
);
CREATE TABLE IF NOT EXISTS evidence (
    case_id      TEXT NOT NULL,
    n            INTEGER NOT NULL,
    kind         TEXT NOT NULL,         -- fact (from a source) or inference (our reasoning)
    source       TEXT NOT NULL,         -- crustdata:/company/identify, register, email, ...
    summary      TEXT NOT NULL,
    data         TEXT,
    url          TEXT,
    retrieved_at TEXT,
    PRIMARY KEY (case_id, n)
);
-- One row per (vendor, invoice number). Reserved before the bank is called,
-- marked paid after, so a crash in between can never lead to a second payout.
CREATE TABLE IF NOT EXISTS payments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_key    TEXT NOT NULL UNIQUE,
    case_id        TEXT NOT NULL,
    vendor_id      TEXT NOT NULL,
    invoice_number TEXT NOT NULL,
    amount         REAL NOT NULL,
    currency       TEXT NOT NULL,
    account        TEXT NOT NULL,
    ifsc           TEXT,
    po_number      TEXT,
    receipt_id     TEXT,
    status         TEXT NOT NULL,       -- reserved, paid
    bank_ref       TEXT,
    created_at     TEXT NOT NULL,
    paid_at        TEXT
);
-- Goods/services receipts recorded by the team (the third leg of the three-way match).
CREATE TABLE IF NOT EXISTS receipts (
    id          TEXT PRIMARY KEY,
    po_number   TEXT NOT NULL,
    amount      REAL NOT NULL,
    note        TEXT,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    case_id     TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_key    TEXT NOT NULL UNIQUE,
    case_id    TEXT NOT NULL,
    to_addr    TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_cache (
    key          TEXT PRIMARY KEY,
    endpoint     TEXT NOT NULL,
    request      TEXT NOT NULL,
    response     TEXT NOT NULL,
    credits      REAL,
    retrieved_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def log_event(conn, case_id, step, message, level="info", evidence=None):
    with conn:
        conn.execute(
            "INSERT INTO events (case_id, ts, step, level, message, evidence) VALUES (?,?,?,?,?,?)",
            (case_id, now(), step, level, message, json.dumps(evidence or [])),
        )


def add_evidence(conn, case_id, kind, source, summary, data=None, url=None, retrieved_at=None) -> str:
    """Record one piece of evidence for a case and return its id, e.g. 'E3'."""
    with conn:
        n = conn.execute(
            "SELECT COALESCE(MAX(n), 0) + 1 FROM evidence WHERE case_id = ?", (case_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO evidence (case_id, n, kind, source, summary, data, url, retrieved_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (case_id, n, kind, source, summary, json.dumps(data) if data is not None else None,
             url, retrieved_at or now()),
        )
    return f"E{n}"


def get_evidence(conn, case_id):
    rows = conn.execute("SELECT * FROM evidence WHERE case_id = ? ORDER BY n", (case_id,)).fetchall()
    return [dict(r) | {"id": f"E{r['n']}"} for r in rows]
