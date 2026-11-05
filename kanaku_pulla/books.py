"""Our ledger against the bank's payouts.

The bank (here, the sandbox bank) is the record we cannot edit, so it is the check on ours. Payments stop
while the two disagree, and spending against a purchase order counts whichever of the two is larger.
"""

from . import bank


def bank_amounts():
    """Payout amounts by payment key, as the bank recorded them."""
    return {b["idempotency_key"]: b for b in bank.all_payouts()}


def reconcile(conn):
    """Compare our ledger with the bank's payouts. Returns (problems, pending, ledger_count, bank_count)."""
    ours = {p["payment_key"]: p for p in conn.execute("SELECT * FROM payments")}
    theirs = bank_amounts()
    problems, pending = [], []
    for key, p in ours.items():
        b = theirs.get(key)
        if p["status"] == "reserved":
            pending.append(p)
        if p["status"] == "paid" and not b:
            problems.append(f"Ledger entry #{p['id']} is marked paid but the bank has no payout for it")
        if b and (b["amount_paise"], b["account"], (b["ifsc"] or "").upper(), b["currency"]) != \
                (p["amount_paise"], p["account"], (p["ifsc"] or "").upper(), p["currency"]):
            problems.append(f"Ledger entry #{p['id']} and bank payout {b['reference']} disagree on the details")
    for key, b in theirs.items():
        if key not in ours:
            problems.append(f"Bank payout {b['reference']} has no ledger entry")
    return problems, pending, len(ours), len(theirs)
