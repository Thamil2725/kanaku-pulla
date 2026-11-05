"""The payment policy: what must be true before money moves, and what to ask for when it isn't.

The model never decides whether money moves. These rules do, so the same case
gets the same answer however the email is worded. Each finding that stops a
payment names, in `needs`, the smallest piece of evidence that would clear it
and who on our team to ask.

What authorises a payment is our own trusted records: the vendor register (who
the vendor is, where its money goes), an approved purchase order, and a receipt
from our team. What the email says about its sender is a claim; sender checks can
only stop a payment, never authorise one.
"""

import json

from . import books, config, db
from .checks import normalize_invoice_number, payment_key
from .money import SUPPORTED_CURRENCIES, fmt, rupees_to_paise, to_paise

REQUIRED_TO_PAY = [
    "The vendor is in our register, matched by its registered domain or Crustdata company id",
    "The email's claimed sender address is on the vendor's registered domain "
    "(a consistency check on what the email says, not proof of who sent it)",
    "The invoice is in a supported currency (INR only for now) that matches the vendor, purchase order and receipt",
    "The bank account number and IFSC both match our register",
    "An approved purchase order for this vendor has enough unspent authorisation (reserved and paid amounts count)",
    "A receipt from our team shows the goods or service were delivered, and no other payment has used it",
    "This invoice number has not been paid or reserved before",
    "The amount is within the auto-pay limit",
    "Nothing about the named sender contradicts the claim (no look-alike domain, no ended role, no mismatch)",
]


# --- purchase orders and receipts ----------------------------------------------------

def load_pos():
    if not config.PURCHASE_ORDERS.exists():
        return []
    return json.loads(config.PURCHASE_ORDERS.read_text(encoding="utf-8"))


def find_po(po_number):
    if not po_number:
        return None
    return next((p for p in load_pos()
                 if normalize_invoice_number(p["po_number"]) == normalize_invoice_number(po_number)), None)


def po_allocated_paise(conn, po_number, exclude_key=None):
    """Reserved plus paid amounts against a PO, excluding one payment key (the case being decided).

    Each entry counts the larger of our ledger amount and what the bank actually paid for it, so lowering
    a ledger amount by hand frees no room on the PO.
    """
    paid_by_bank = books.bank_amounts()
    rows = conn.execute(
        "SELECT payment_key, amount_paise FROM payments WHERE po_number = ? AND status IN ('reserved', 'paid') "
        "AND payment_key != ?", (po_number, exclude_key or "")).fetchall()
    return sum(max(r["amount_paise"] or 0, (paid_by_bank.get(r["payment_key"]) or {}).get("amount_paise") or 0)
               for r in rows)


def receipt_records(conn, po):
    """Receipts against a PO: seeded in the PO register, or recorded by the team in the app."""
    out = []
    for r in po.get("receipts", []):
        out.append({"id": r["id"], "amount_paise": rupees_to_paise(r["amount"]), "currency": r.get("currency"),
                    "note": r.get("note"), "recorded_by": r.get("recorded_by", "unknown"),
                    "recorded_at": r.get("recorded_at", ""), "source": "purchase-orders"})
    for r in conn.execute("SELECT * FROM receipts WHERE po_number = ?", (po["po_number"],)):
        out.append({"id": r["id"], "amount_paise": rupees_to_paise(r["amount"]), "currency": r["currency"],
                    "note": r["note"], "recorded_by": r["recorded_by"], "recorded_at": r["recorded_at"],
                    "source": "team"})
    return out


def open_receipts(conn, po, amount_paise, currency, exclude_key=None):
    """Receipts for exactly this amount and currency that no other payment has been allocated."""
    taken = {r["receipt_id"]: r["payment_key"] for r in conn.execute("SELECT * FROM receipt_allocations")}
    return [r for r in receipt_records(conn, po)
            if r["amount_paise"] == amount_paise and r["currency"] == currency
            and taken.get(r["id"], exclude_key) == exclude_key]


# --- the decision ----------------------------------------------------------------------

def decide(conn, case_id, fields, grounded, resolution, sender, extra=None):
    """Return (decision, findings, match). Decision is PAY, DUPLICATE, HOLD or REJECTED."""
    extra = extra or {}
    findings = []
    match = {}

    def add(code, severity, text, evidence=None, needs=None):
        if evidence is None:
            evidence = [db.add_evidence(conn, case_id, "inference", "rules", text)]
        findings.append({"code": code, "severity": severity, "text": text, "evidence": evidence, "needs": needs})

    # Reading: required fields present, found in the text, and money unambiguous
    for name in ("sender_email", "invoice_number", "amount", "currency", "bank_account_number", "bank_ifsc"):
        label = name.replace("_", " ").replace("bank ifsc", "IFSC")
        if not fields.get(name):
            add("missing_" + name, "hold", f"The invoice does not clearly state the {label}",
                needs={"evidence": f"the {label}, confirmed with the vendor's registered contact", "ask": "ap-lead"})
        elif not grounded.get(name):
            add("ungrounded_" + name, "hold", f"The {label} that was read could not be found in the email text",
                needs={"evidence": f"a person to confirm the {label}", "ask": "ap-lead"})
    amount = to_paise(fields.get("amount"))
    if fields.get("amount") and amount is None:
        add("malformed_amount", "hold", f"The amount '{fields.get('amount')}' is not a valid positive amount",
            needs={"evidence": "the payable amount", "ask": "ap-lead"})
    if extra.get("amount_ambiguous"):
        add("amount_ambiguous", "hold", "The invoice shows more than one total with different values",
            needs={"evidence": "which total is payable", "ask": "ap-lead"})
    if extra.get("amount_disagreement"):
        add("amount_disagreement", "hold",
            f"Two independent readings of the amount disagree ({extra['amount_disagreement']})",
            needs={"evidence": "a person to confirm the payable amount", "ask": "ap-lead"})
    if extra.get("currency_conflict"):
        add("currency_conflict", "hold", f"The email mentions more than one currency ({extra['currency_conflict']})",
            needs={"evidence": "which currency the invoice is in", "ask": "ap-lead"})
    currency = (fields.get("currency") or "").upper() or None
    if currency and currency not in SUPPORTED_CURRENCIES:
        add("unsupported_currency", "hold",
            f"The invoice is in {currency}; Kanaku Pulla only pays {', '.join(sorted(SUPPORTED_CURRENCIES))} invoices "
            f"automatically", needs={"evidence": f"a person to handle this {currency} invoice", "ask": "ap-lead"})
    for text in fields.get("instructions_to_automated_systems") or []:
        add("embedded_instructions", "warn",
            f"The email contains instructions aimed at automated systems, which were ignored: \"{text[:120]}\"")

    vendor = resolution.get("vendor")
    claimed = resolution.get("claimed")
    status = resolution.get("domain_status")
    dom_ev = resolution.get("evidence", [])

    # Who does it claim to be from, and does that claim hold together?
    if status == "lookalike":
        add("lookalike_domain", "block",
            f"The claimed sender domain {resolution['sender_domain']} imitates {claimed['legal_name']}'s registered "
            f"domain {claimed['registered_domain']}", dom_ev)
    elif status in ("unrelated", "listed_alternate") and claimed:
        add("domain_mismatch", "hold",
            f"The invoice claims to be from {claimed['legal_name']} but the claimed sender address is on "
            f"{resolution['sender_domain']}, not the registered domain {claimed['registered_domain']}", dom_ev,
            needs={"evidence": f"a call-back to {claimed['legal_name']}'s registered contact confirming they sent it",
                   "ask": "ap-lead"})
    if not vendor and status != "lookalike":
        if resolution.get("name_ambiguous"):
            add("ambiguous_vendor", "hold",
                "The vendor name matches several companies equally well and the sending domain does not settle "
                "which one", dom_ev)
        add("new_vendor", "hold", "This vendor is not in our register", dom_ev,
            needs={"evidence": "vendor onboarding: registered domain, bank details and an approved contact",
                   "ask": "ap-lead"})
    if vendor and currency and vendor.get("currency") and currency != vendor["currency"]:
        add("vendor_currency", "hold", f"{vendor['legal_name']} is set up to bill in {vendor['currency']}, "
                                       f"but this invoice is in {currency}",
            needs={"evidence": "confirmation of the billing currency", "ask": "ap-lead"})

    # Does the named sender's public profile contradict the claim? (It can stop a payment, never authorise one.)
    approved = sender.get("approved_contact")
    if sender["status"] == "left":
        add("sender_left", "hold",
            f"The named sender's public profile lists their role at the vendor as ended "
            f"({(sender.get('left_on') or '')[:7]})", sender["evidence"],
            needs={"evidence": "confirmation from the vendor's registered contact that this invoice is genuine",
                   "ask": "ap-lead"})
    elif sender["status"] == "current" and not sender.get("finance_role") and not approved:
        add("sender_not_finance", "warn",
            f"The named sender's profile lists a non-finance role at the vendor ('{sender.get('title')}')",
            sender["evidence"])
    elif sender["status"] in ("not_found", "error", "unverifiable", "role_mailbox") and not approved:
        if status == "registered":
            add("sender_unverified", "warn",
                "No public profile confirms the named sender, but the claimed address is on the vendor's "
                "registered domain", sender["evidence"])
        elif vendor or claimed:
            add("sender_unverified", "hold", "Neither the named sender nor the claimed domain checks out",
                sender["evidence"],
                needs={"evidence": "a call-back to the vendor's registered contact", "ask": "ap-lead"})

    # Where does the money go? Account number and IFSC must both match the register.
    account = (fields.get("bank_account_number") or "").replace(" ", "")
    ifsc = (fields.get("bank_ifsc") or "").replace(" ", "").upper()
    if claimed and (account or ifsc):
        reg_acct, reg_ifsc = claimed.get("bank_account_number", ""), (claimed.get("bank_ifsc") or "").upper()
        changed = []
        if account and account != reg_acct:
            changed.append(f"account ending {account[-4:]} instead of …{reg_acct[-4:]}")
        if ifsc and ifsc != reg_ifsc:
            changed.append(f"IFSC {ifsc} instead of {reg_ifsc}")
        if changed:
            add("bank_changed", "hold",
                f"The invoice asks for payment to {' and '.join(changed)}, which our register has for "
                f"{claimed['legal_name']}",
                needs={"evidence": f"confirmation of the new bank details by phone to {claimed['legal_name']}'s "
                                   f"registered contact (the number on file, never one from this email)",
                       "ask": "ap-lead"})

    limit = rupees_to_paise(config.AUTO_PAY_LIMIT_INR)
    if amount is not None and amount > limit:
        add("over_limit", "hold", f"{fmt(amount)} is above the auto-pay limit of {fmt(limit)}",
            needs={"evidence": "approval from someone with authority for this amount", "ask": "ap-lead"})

    # Has this debt been paid or reserved before? Same vendor + same invoice number = same debt.
    vendor_id = (vendor or claimed or {}).get("vendor_id")
    inv = normalize_invoice_number(fields.get("invoice_number"))
    key = payment_key(vendor_id, fields.get("invoice_number")) if vendor_id and inv else None
    if key:
        prior = conn.execute("SELECT * FROM payments WHERE payment_key = ? AND case_id != ?", (key, case_id)).fetchone()
        if prior:
            same = (amount == prior["amount_paise"] and account == prior["account"]
                    and ifsc == (prior["ifsc"] or "").upper() and currency == prior["currency"])
            ev = db.add_evidence(
                conn, case_id, "fact", "ledger",
                f"Invoice {inv} from {(vendor or claimed)['legal_name']} is already in our ledger: "
                f"{fmt(prior['amount_paise'])} {prior['currency']} to account ending {prior['account'][-4:]} "
                f"(IFSC {prior['ifsc']}), {prior['status']} (entry #{prior['id']}, case {prior['case_id']}, "
                f"{prior['created_at'][:16]})", data=dict(prior))
            if same:
                findings.insert(0, {"code": "duplicate", "severity": "duplicate", "evidence": [ev], "needs": None,
                                    "text": f"Already {prior['status']}: ledger entry #{prior['id']}"})
            else:
                add("changed_resend", "block",
                    f"This copy of invoice {inv} asks for {fmt(amount)} {currency or ''} to account ending "
                    f"{account[-4:]} (IFSC {ifsc or 'none'}), but the same invoice is already {prior['status']} for "
                    f"{fmt(prior['amount_paise'])} {prior['currency']} to account ending {prior['account'][-4:]} "
                    f"(IFSC {prior['ifsc']}). A real change comes as a new invoice or a debit note.", [ev])

    # A renamed resend ("RZP-2026-42-REMINDER", "RZP-2026-42-R1") of an invoice we already paid, for the same amount.
    # Same amount alone proves nothing (monthly bills repeat), so only a number that extends a paid one counts, and
    # it clears when the team records a delivery for this specific invoice, which is what makes it a new charge.
    if vendor_id and inv and amount is not None and not any(f["code"] in ("duplicate", "changed_resend") for f in findings):
        mine = inv.split("-")
        team_receipt_here = conn.execute("SELECT 1 FROM receipts WHERE case_id = ?", (case_id,)).fetchone()
        for p in conn.execute("SELECT * FROM payments WHERE vendor_id = ? AND case_id != ? AND status IN "
                              "('reserved', 'paid') ORDER BY id", (vendor_id, case_id)):
            theirs = p["invoice_number"].split("-")
            n = min(len(mine), len(theirs))
            if mine != theirs and mine[:n] == theirs[:n] and p["amount_paise"] == amount and not team_receipt_here:
                ev = db.add_evidence(
                    conn, case_id, "fact", "ledger",
                    f"Invoice {inv} extends the number of invoice {p['invoice_number']}, which is already {p['status']} "
                    f"for the same {fmt(amount)} (entry #{p['id']}, case {p['case_id']})", data=dict(p))
                add("duplicate_suspect", "hold",
                    f"Invoice {inv} looks like a renamed resend of {p['invoice_number']}, already {p['status']} for the "
                    f"same amount", [ev],
                    needs={"evidence": f"a receipt from the team for this invoice, confirming a new delivery (not a "
                                       f"resend of {p['invoice_number']})",
                           "ask": "ap-lead", "provide_as": "receipt", "po_number": p["po_number"],
                           "amount_paise": amount, "currency": p["currency"]})
                break

    # Is the money owed? Three-way match: purchase order, receipt, invoice.
    if (vendor and amount is not None and currency in SUPPORTED_CURRENCIES
            and not any(f["severity"] in ("duplicate", "block") for f in findings)):
        _three_way_match(conn, case_id, vendor, amount, currency, key, fields, add, match)

    severities = {f["severity"] for f in findings}
    if "duplicate" in severities:
        decision = "DUPLICATE"
    elif "block" in severities:
        decision = "REJECTED"
    elif "hold" in severities:
        decision = "HOLD"
    else:
        decision = "PAY"
    match.update(amount_paise=amount, currency=currency, payment_key=key)
    return decision, findings, match


def _three_way_match(conn, case_id, vendor, amount, currency, key, fields, add, match):
    pos = [p for p in load_pos() if p.get("status", "approved") == "approved"]
    cited = fields.get("po_number")
    if cited:
        po = find_po(cited)
        if not po or po.get("status", "approved") != "approved":
            add("unknown_po", "hold", f"The invoice cites {cited}, which is not an approved purchase order",
                needs={"evidence": f"an approved purchase order for {fmt(amount)} from {vendor['legal_name']}",
                       "ask": "ap-lead"})
            return
        if po["vendor_id"] != vendor["vendor_id"]:
            add("po_wrong_vendor", "hold", f"{po['po_number']} was raised for a different vendor",
                needs={"evidence": "the purchase order this invoice belongs to", "ask": "ap-lead"})
            return
    else:
        candidates = [p for p in pos if p["vendor_id"] == vendor["vendor_id"] and p.get("currency") == currency
                      and rupees_to_paise(p["approved_amount"]) - po_allocated_paise(conn, p["po_number"], key) >= amount]
        if len(candidates) != 1:
            add("no_po", "hold",
                "The invoice cites no purchase order and "
                + ("none of ours covers it" if not candidates else "several of ours could cover it"),
                needs={"evidence": f"the purchase order for this {fmt(amount)} invoice", "ask": "ap-lead"})
            return
        po = candidates[0]

    if po.get("currency") != currency:
        add("po_currency", "hold",
            f"{po['po_number']} is in {po.get('currency') or 'an unstated currency'}, the invoice in {currency}",
            needs={"evidence": f"a purchase order in {currency}", "ask": "ap-lead"})
        return
    authorised = rupees_to_paise(po["approved_amount"])
    remaining = authorised - po_allocated_paise(conn, po["po_number"], key)
    po_ev = db.add_evidence(
        conn, case_id, "fact", "purchase-orders",
        f"{po['po_number']} ({po['description']}) was approved by {po['approved_by']} on {po['approved_on']} for "
        f"{fmt(authorised)} {po['currency']}; {fmt(remaining)} is not yet reserved or paid",
        data={k: po.get(k) for k in ("po_number", "approved_amount", "currency", "approved_by", "approved_on",
                                     "requested_by")})
    match["po_number"] = po["po_number"]
    if amount > remaining:
        add("po_exhausted", "hold", f"{fmt(amount)} is more than the {fmt(remaining)} left on {po['po_number']}",
            [po_ev], needs={"evidence": f"an amendment raising {po['po_number']}", "ask": "ap-lead"})
        return

    receipts = open_receipts(conn, po, amount, currency, key)
    if not receipts:
        add("no_receipt", "hold",
            f"No unused receipt confirms that '{po['description']}' was delivered for this {fmt(amount)} invoice",
            [po_ev],
            needs={"evidence": f"a receipt confirming delivery of '{po['description']}' ({fmt(amount)})",
                   "ask": po.get("requested_by") or "ops-lead", "provide_as": "receipt",
                   "po_number": po["po_number"], "amount_paise": amount, "currency": currency})
        return
    r = receipts[0]
    match["receipt_id"] = r["id"]
    who = r["recorded_by"] + (" (sandbox identity, typed in, not authenticated)" if r["source"] == "team" else "")
    db.add_evidence(
        conn, case_id, "fact", "receipts",
        f"Receipt {r['id']} against {po['po_number']}: {r.get('note') or 'delivered'} "
        f"({fmt(r['amount_paise'])} {r['currency']}, recorded by {who} on {(r.get('recorded_at') or '')[:10]})")
