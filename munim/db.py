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
    amount         REAL NOT NULL,       -- display only; decisions use amount_paise
    amount_paise   INTEGER,
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
    case_id     TEXT,
    currency    TEXT
);
-- Each delivery receipt can back exactly one payment. Receipt ids are globally unique
-- (seeded ids like GRN-2026-311 and team-recorded GRN-xxxxxx share one namespace).
CREATE TABLE IF NOT EXISTS receipt_allocations (
    receipt_id   TEXT PRIMARY KEY,
    po_number    TEXT NOT NULL,
    payment_key  TEXT NOT NULL,
    case_id      TEXT NOT NULL,
    amount_paise INTEGER NOT NULL,
    currency     TEXT,
    created_at   TEXT NOT NULL
);
-- Problems found while upgrading an older database. Reported, never silently "fixed" by deleting rows.
CREATE TABLE IF NOT EXISTS migration_issues (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    detail     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (kind, detail)
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
    migrate(conn)
    return conn


def clear_cases(conn):
    """Start a fresh demo: remove cases, evidence, the ledger, receipts and messages.
    Keeps the Crustdata cache (so a rerun costs no credits) and any upgrade reports."""
    with conn:
        for table in ("cases", "events", "evidence", "payments", "receipt_allocations", "outbox", "receipts"):
            conn.execute(f"DELETE FROM {table}")


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn):
    """Upgrade databases created by earlier versions in place. Safe to run on every connect."""
    from .money import rupees_to_paise

    with conn:
        if "amount_paise" not in _columns(conn, "payments"):
            conn.execute("ALTER TABLE payments ADD COLUMN amount_paise INTEGER")
        for row in conn.execute("SELECT id, amount FROM payments WHERE amount_paise IS NULL").fetchall():
            conn.execute("UPDATE payments SET amount_paise = ? WHERE id = ?", (rupees_to_paise(row["amount"]), row["id"]))
        if "currency" not in _columns(conn, "receipts"):
            conn.execute("ALTER TABLE receipts ADD COLUMN currency TEXT")  # legacy rows stay NULL: currency unknown

        # Older versions recorded receipt_id on payments without a uniqueness rule. Allocate each
        # receipt to the first payment that used it; report any later reuse instead of deleting it.
        rows = conn.execute(
            "SELECT * FROM payments WHERE receipt_id IS NOT NULL AND status IN ('reserved', 'paid') ORDER BY id"
        ).fetchall()
        for row in rows:
            taken = conn.execute("SELECT * FROM receipt_allocations WHERE receipt_id = ?", (row["receipt_id"],)).fetchone()
            if taken is None:
                conn.execute(
                    "INSERT INTO receipt_allocations (receipt_id, po_number, payment_key, case_id, amount_paise, "
                    "currency, created_at) VALUES (?,?,?,?,?,?,?)",
                    (row["receipt_id"], row["po_number"] or "", row["payment_key"], row["case_id"],
                     row["amount_paise"], row["currency"], row["created_at"]))
            elif taken["payment_key"] != row["payment_key"]:
                conn.execute(
                    "INSERT OR IGNORE INTO migration_issues (kind, detail, created_at) VALUES (?,?,?)",
                    ("shared_receipt",
                     f"Receipt {row['receipt_id']} backs payment {taken['payment_key']} (case {taken['case_id']}) and "
                     f"also payment {row['payment_key']} (case {row['case_id']}); both payments are kept, the receipt "
                     f"is allocated to the first", now()))


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
