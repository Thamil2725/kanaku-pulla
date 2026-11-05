"""A sandbox bank that stands in for the real payout API (Razorpay X, Stripe and so on).

Like those APIs it honours an idempotency key: sending the same payout twice
returns the first payout instead of moving money again. Reusing a key with any
different detail (account, IFSC, amount or currency) is refused, never paid.
"""

import sqlite3
import uuid

from . import config, db
from .money import rupees_to_paise

SCHEMA = """
CREATE TABLE IF NOT EXISTS payouts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    reference       TEXT NOT NULL,
    account         TEXT NOT NULL,
    ifsc            TEXT,
    amount          REAL NOT NULL,      -- display only
    amount_paise    INTEGER,
    currency        TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
"""


class IdempotencyConflict(Exception):
    """The key was used before with different payment details."""


def _connect():
    conn = sqlite3.connect(config.BANK_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    with conn:  # upgrade payouts written by earlier versions
        if "amount_paise" not in {r[1] for r in conn.execute("PRAGMA table_info(payouts)")}:
            conn.execute("ALTER TABLE payouts ADD COLUMN amount_paise INTEGER")
        for r in conn.execute("SELECT id, amount FROM payouts WHERE amount_paise IS NULL").fetchall():
            conn.execute("UPDATE payouts SET amount_paise = ? WHERE id = ?", (rupees_to_paise(r["amount"]), r["id"]))
    return conn


def payout(idempotency_key, account, ifsc, amount_paise, currency):
    """Move money once per key. Returns (reference, created); created is False on a replay."""
    if not isinstance(amount_paise, int) or amount_paise <= 0:
        raise ValueError("amount_paise must be a positive integer")
    request = (account, (ifsc or "").upper(), amount_paise, currency)
    conn = _connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO payouts (idempotency_key, reference, account, ifsc, amount, amount_paise, "
                "currency, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (idempotency_key, "PAYOUT-" + uuid.uuid4().hex[:10].upper(), account, request[1],
                 amount_paise / 100, amount_paise, currency, db.now()),
            )
            created = cur.rowcount == 1
        row = conn.execute("SELECT * FROM payouts WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        stored = (row["account"], (row["ifsc"] or "").upper(), row["amount_paise"], row["currency"])
        if stored != request:
            raise IdempotencyConflict(
                f"key {idempotency_key[:10]}… was already used for a different payment "
                f"(a/c …{row['account'][-4:]}, IFSC {row['ifsc']}, {row['currency']} {row['amount_paise'] / 100:,.2f})")
        return row["reference"], created
    finally:
        conn.close()


def clear():
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM payouts")
    finally:
        conn.close()


def all_payouts():
    conn = _connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM payouts ORDER BY id")]
    finally:
        conn.close()
