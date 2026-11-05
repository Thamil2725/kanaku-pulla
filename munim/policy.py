"""The payment policy: what must be true before money moves, and what to ask for when it isn't.

The model never decides whether money moves. These rules do, so the same case
gets the same answer however the email is worded. Each finding that stops a
payment names, in `needs`, the smallest piece of evidence that would clear it
and who on our team to ask.
"""

import json

from . import config, db
from .checks import normalize_invoice_number, parse_amount

REQUIRED_TO_PAY = [
    "The vendor is in our register, matched by its registered domain or Crustdata company id",
    "The email came from the vendor's registered domain",
    "The bank account matches the one in our register",
    "An approved purchase order for this vendor covers the amount",
    "A receipt shows the goods or service were delivered",
    "This invoice number has not been paid or reserved before",
    "The amount is within the auto-pay limit",
    "The sender is an approved contact, verified at the vendor, or a vendor mailbox on the registered domain",
]


# --- purchase orders and receipts ----------------------------------------------------

def load_pos():
    if not config.PURCHASE_ORDERS.exists():
        return []
    return json.loads(config.PURCHASE_ORDERS.read_text(encoding="utf-8"))


def find_po(po_number):
    return next((p for p in load_pos()
                 if normalize_invoice_number(p["po_number"]) == normalize_invoice_number(po_number)), None)


def po_paid_total(conn, po_number, exclude_case=None):
    return conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE po_number = ? AND case_id != ?",
        (po_number, exclude_case or "")).fetchone()[0]


def open_receipts(conn, po, amount, exclude_case=None):
    """Receipts against this PO for exactly this amount that no other payment has used."""
    seeded = [dict(r, source="purchase-orders") for r in po.get("receipts", [])]
    added = [dict(r, source="team") for r in conn.execute(
        "SELECT * FROM receipts WHERE po_number = ?", (po["po_number"],))]
    used = {r[0] for r in conn.execute(
        "SELECT receipt_id FROM payments WHERE receipt_id IS NOT NULL AND case_id != ?", (exclude_case or "",))}
    return [r for r in seeded + added
            if round(float(r["amount"]), 2) == round(amount, 2) and r["id"] not in used]


# --- the decision ----------------------------------------------------------------------

def decide(conn, case_id, fields, grounded, resolution, sender, extra=None):
    """Return (decision, findings, match). Decision is PAY, DUPLICATE, HOLD or REJECTED."""
    extra = extra or {}
    findings = []
    match = {}

    def add(code, severity, text, evidence=None, needs=None):
        if evidence is None:
            evidence = [db.add_evidence(conn, case_id, "inference", "munim", text)]
        findings.append({"code": code, "severity": severity, "text": text, "evidence": evidence, "needs": needs})

    # Reading: required fields present, found in the text, and the amount unambiguous
    for name in ("sender_email", "invoice_number", "amount", "bank_account_number"):
        label = name.replace("_", " ")
        if not fields.get(name):
            add("missing_" + name, "hold", f"The invoice does not clearly state the {label}",
                needs={"evidence": f"the {label}, confirmed with the vendor's registered contact", "ask": "ap-lead"})
        elif not grounded.get(name):
            add("ungrounded_" + name, "hold", f"The {label} that was read could not be found in the email text",
                needs={"evidence": f"a person to confirm the {label}", "ask": "ap-lead"})
    if extra.get("amount_ambiguous"):
        add("amount_ambiguous", "hold", "The invoice shows more than one total with different values",
            needs={"evidence": "which total is payable", "ask": "ap-lead"})
    if extra.get("amount_disagreement"):
        add("amount_disagreement", "hold",
            f"Two independent readings of the amount disagree ({extra['amount_disagreement']})",
            needs={"evidence": "a person to confirm the payable amount", "ask": "ap-lead"})
    for text in fields.get("instructions_to_automated_systems") or []:
        add("embedded_instructions", "warn",
            f"The email contains instructions aimed at automated systems, which were ignored: \"{text[:120]}\"")

    vendor = resolution.get("vendor")
    claimed = resolution.get("claimed")
    status = resolution.get("domain_status")
    dom_ev = resolution.get("evidence", [])

    # Who is it from?
    if status == "lookalike":
        add("lookalike_domain", "block",
            f"The sender's domain {resolution['sender_domain']} imitates {claimed['legal_name']}'s registered "
            f"domain {claimed['registered_domain']}", dom_ev)
    elif status in ("unrelated", "listed_alternate") and claimed:
        add("domain_mismatch", "hold",
            f"The invoice claims to be from {claimed['legal_name']} but was sent from "
            f"{resolution['sender_domain']}, not the registered domain {claimed['registered_domain']}", dom_ev,
            needs={"evidence": f"a call-back to {claimed['legal_name']}'s registered contact confirming they sent it",
                   "ask": "ap-lead"})
    if not vendor and status != "lookalike":
        if resolution.get("name_ambiguous"):
            add("ambiguous_vendor", "hold",
                "The vendor name matches several companies equally well and the sending domain does not settle "
                "which one", dom_ev)
        add("new_vendor", "hold", "This vendor is not in our register", dom_ev,
            needs={"evidence": "vendor onboarding: registered domain, bank account and an approved contact",
                   "ask": "ap-lead"})

    # Is the sender who they claim?
    approved = sender.get("approved_contact")
    if sender["status"] == "left":
        add("sender_left", "hold",
            f"The sender left the vendor ({(sender.get('left_on') or '')[:7]}) and now works elsewhere",
            sender["evidence"],
            needs={"evidence": "confirmation from the vendor's registered contact that this invoice is genuine",
                   "ask": "ap-lead"})
    elif sender["status"] == "current" and not sender.get("finance_role") and not approved:
        add("sender_not_finance", "warn",
            f"The sender works at the vendor but not in a finance role ('{sender.get('title')}')", sender["evidence"])
    elif sender["status"] in ("not_found", "error", "unverifiable", "role_mailbox") and not approved:
        if status == "registered":
            add("sender_unverified", "warn",
                "The individual sender could not be verified from public data, but the email came from the "
                "vendor's registered domain", sender["evidence"])
        elif vendor or claimed:
            add("sender_unverified", "hold", "Neither the sender nor the sending domain could be verified",
                sender["evidence"],
                needs={"evidence": "a call-back to the vendor's registered contact", "ask": "ap-lead"})

    # Where does the money go?
    account = (fields.get("bank_account_number") or "").replace(" ", "")
    if claimed and account and account != claimed.get("bank_account_number"):
        add("bank_changed", "hold",
            f"The invoice asks for payment to account ending {account[-4:]}, but our register has account ending "
            f"{claimed['bank_account_number'][-4:]} for {claimed['legal_name']}",
            needs={"evidence": f"confirmation of the new account by phone to {claimed['legal_name']}'s registered "
                               f"contact (the number on file, never one from this email)", "ask": "ap-lead"})

    amount = parse_amount(fields.get("amount"))
    if amount is not None and amount > config.AUTO_PAY_LIMIT_INR:
        add("over_limit", "hold",
            f"₹{amount:,.2f} is above the auto-pay limit of ₹{config.AUTO_PAY_LIMIT_INR:,.0f}",
            needs={"evidence": "approval from someone with authority for this amount", "ask": "ap-lead"})

    # Has this debt been paid (or changed) before? Same vendor + same invoice number = same debt.
    vendor_id = (vendor or claimed or {}).get("vendor_id")
    inv = normalize_invoice_number(fields.get("invoice_number"))
    if vendor_id and inv:
        prior = conn.execute(
            "SELECT * FROM payments WHERE vendor_id = ? AND invoice_number = ? AND case_id != ?",
            (vendor_id, inv, case_id)).fetchone()
        if prior:
            same = amount is not None and round(prior["amount"], 2) == amount and prior["account"] == account
            ev = db.add_evidence(
                conn, case_id, "fact", "ledger",
                f"Invoice {inv} from {(vendor or claimed)['legal_name']} is already in our ledger: "
                f"₹{prior['amount']:,.2f} to account ending {prior['account'][-4:]}, {prior['status']} "
                f"(entry #{prior['id']}, case {prior['case_id']}, {prior['created_at'][:16]})",
                data=dict(prior))
            if same:
                findings.insert(0, {"code": "duplicate", "severity": "duplicate", "evidence": [ev], "needs": None,
                                    "text": f"Already {prior['status']}: ledger entry #{prior['id']}"})
            else:
                add("changed_resend", "block",
                    f"This copy of invoice {inv} asks for ₹{amount or 0:,.2f} to account ending {account[-4:]}, "
                    f"but the same invoice is already {prior['status']} for ₹{prior['amount']:,.2f} to account "
                    f"ending {prior['account'][-4:]}. A real change comes as a new invoice or a debit note.", [ev])

    # Is the money owed? Three-way match: purchase order, receipt, invoice.
    if vendor and amount is not None and not any(f["severity"] in ("duplicate", "block") for f in findings):
        _three_way_match(conn, case_id, vendor, amount, fields, add, match)

    severities = {f["severity"] for f in findings}
    if "duplicate" in severities:
        decision = "DUPLICATE"
    elif "block" in severities:
        decision = "REJECTED"
    elif "hold" in severities:
        decision = "HOLD"
    else:
        decision = "PAY"
    return decision, findings, match


def _three_way_match(conn, case_id, vendor, amount, fields, add, match):
    pos = [p for p in load_pos() if p.get("status", "approved") == "approved"]
    cited = fields.get("po_number")
    if cited:
        po = find_po(cited)
        if not po or po.get("status", "approved") != "approved":
            add("unknown_po", "hold", f"The invoice cites {cited}, which is not an approved purchase order",
                needs={"evidence": f"an approved purchase order for ₹{amount:,.2f} from {vendor['legal_name']}",
                       "ask": "ap-lead"})
            return
        if po["vendor_id"] != vendor["vendor_id"]:
            add("po_wrong_vendor", "hold", f"{po['po_number']} was raised for a different vendor",
                needs={"evidence": "the purchase order this invoice belongs to", "ask": "ap-lead"})
            return
    else:
        candidates = [p for p in pos if p["vendor_id"] == vendor["vendor_id"]
                      and p["approved_amount"] - po_paid_total(conn, p["po_number"], case_id) >= amount]
        if len(candidates) != 1:
            add("no_po", "hold",
                "The invoice cites no purchase order and "
                + ("none of ours covers it" if not candidates else "several of ours could cover it"),
                needs={"evidence": f"the purchase order for this ₹{amount:,.2f} invoice", "ask": "ap-lead"})
            return
        po = candidates[0]

    remaining = po["approved_amount"] - po_paid_total(conn, po["po_number"], case_id)
    po_ev = db.add_evidence(
        conn, case_id, "fact", "purchase-orders",
        f"{po['po_number']} ({po['description']}) was approved by {po['approved_by']} on {po['approved_on']} "
        f"for ₹{po['approved_amount']:,.0f}; ₹{remaining:,.0f} remains after earlier payments",
        data={k: po.get(k) for k in ("po_number", "approved_amount", "approved_by", "approved_on", "requested_by")})
    match["po_number"] = po["po_number"]
    if amount > remaining:
        add("po_exhausted", "hold", f"₹{amount:,.2f} is more than the ₹{remaining:,.0f} left on {po['po_number']}",
            [po_ev], needs={"evidence": f"an amendment raising {po['po_number']}", "ask": "ap-lead"})
        return

    receipts = open_receipts(conn, po, amount, case_id)
    if not receipts:
        add("no_receipt", "hold",
            f"Nobody has confirmed that '{po['description']}' was delivered for this ₹{amount:,.2f} invoice",
            [po_ev],
            needs={"evidence": f"a receipt confirming delivery of '{po['description']}' (₹{amount:,.2f})",
                   "ask": po.get("requested_by") or "ops-lead", "provide_as": "receipt",
                   "po_number": po["po_number"], "amount": amount})
        return
    r = receipts[0]
    match["receipt_id"] = r["id"]
    db.add_evidence(
        conn, case_id, "fact", "receipts",
        f"Receipt {r['id']} against {po['po_number']}: {r.get('note') or 'delivered'} "
        f"(₹{float(r['amount']):,.2f}, recorded by {r.get('recorded_by', 'unknown')} on "
        f"{(r.get('recorded_at') or '')[:10]})")
