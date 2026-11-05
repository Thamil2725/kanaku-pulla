"""A sandbox bank that stands in for the real payout API (Razorpay X, Stripe and so on).

Like those APIs it honours an idempotency key: sending the same payout twice
returns the first payout instead of moving money again, and reusing a key with
different details is refused rather than paid.
"""

import sqlite3
import uuid

from . import config, db

SCHEMA = """
CREATE TABLE IF NOT EXISTS payouts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    reference       TEXT NOT NULL,
    account         TEXT NOT NULL,
    ifsc            TEXT,
    amount          REAL NOT NULL,
    currency        TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
"""


class IdempotencyConflict(Exception):
    """The key was used before with a different amount or account."""


def _connect():
    conn = sqlite3.connect(config.BANK_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def payout(idempotency_key, account, ifsc, amount, currency):
    """Move money once per key. Returns (reference, created); created is False on a replay."""
    conn = _connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO payouts (idempotency_key, reference, account, ifsc, amount, currency, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (idempotency_key, "PAYOUT-" + uuid.uuid4().hex[:10].upper(), account, ifsc, amount, currency, db.now()),
            )
            created = cur.rowcount == 1
        row = conn.execute("SELECT * FROM payouts WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        if (row["account"], round(row["amount"], 2), row["currency"]) != (account, round(amount, 2), currency):
            raise IdempotencyConflict(
                f"key {idempotency_key[:10]}… was already used for ₹{row['amount']:,.2f} to a/c …{row['account'][-4:]}")
        return row["reference"], created
    finally:
        conn.close()


def all_payouts():
    conn = _connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM payouts ORDER BY id")]
    finally:
        conn.close()
