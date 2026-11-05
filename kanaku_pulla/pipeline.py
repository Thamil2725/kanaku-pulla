"""The case runner: a resumable sequence of steps, each saved before the next starts.

If the process dies, `resume` picks each unfinished case up at the step it had
reached. Paying happens in three parts:

1. reserve: one write transaction (BEGIN IMMEDIATE) re-checks the purchase order's
   unspent authorisation and the receipt, then records the invoice and the receipt
   allocation. Two cases can never reserve the same receipt or overspend a PO.
2. pay: the bank call, outside any transaction, with a stable idempotency key.
3. settle: mark the reservation paid.

A reservation is never released because of a timeout or crash: the bank may
already have paid, and the retry with the same key finds out safely.
"""

import json
import os
import sys
import uuid

from . import bank, books, checks, config, db, llm, memo, policy
from .crustdata import Crustdata
from .extract import best_amount, currency_reading, extract, quote_is_grounded
from .money import fmt, rupees_to_paise, to_paise

STEPS = ["extract", "resolve", "verify_sender", "profile", "decide", "explain", "act", "notify", "done"]
FINAL = {"PAY": "PAID", "DUPLICATE": "DUPLICATE", "HOLD": "HOLD", "REJECTED": "REJECTED"}


def maybe_crash(point):
    if config.CRASH_AT and config.CRASH_AT == point:
        print(f"\n💥 Simulated crash at '{point}' (KANAKU_CRASH_AT). Run `python -m kanaku_pulla resume` to continue.")
        sys.stdout.flush()
        os._exit(137)


def create_case(conn, raw, source_name=None):
    case_id = "C-" + uuid.uuid4().hex[:6].upper()
    with conn:
        conn.execute(
            "INSERT INTO cases (id, created_at, updated_at, source_name, raw, step, status, state) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (case_id, db.now(), db.now(), source_name, raw, STEPS[0], "RUNNING", "{}"),
        )
    db.log_event(conn, case_id, "received", f"Received {source_name or 'invoice'}")
    return case_id


def _load(conn, case_id):
    row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    if not row:
        raise KeyError(case_id)
    return row, json.loads(row["state"])


def _advance(conn, case_id, state, next_step, status=None):
    with conn:
        conn.execute(
            "UPDATE cases SET state = ?, step = ?, status = COALESCE(?, status), updated_at = ? WHERE id = ?",
            (json.dumps(state, default=str), next_step, status, db.now(), case_id),
        )


def run(conn, case_id):
    """Run a case from wherever it stopped until it is done or waiting on a person."""
    cd = Crustdata(conn)
    while True:
        row, state = _load(conn, case_id)
        step = row["step"]
        if step == "done":
            return row["status"]
        try:
            next_step, status = HANDLERS[step](conn, cd, case_id, row, state)
        except Exception as exc:  # unexpected tool output, bank timeout or bug: stop, keep the step for resume
            if conn.in_transaction:
                conn.rollback()
            db.log_event(conn, case_id, step, f"Step failed: {exc.__class__.__name__}: {exc}", level="error")
            with conn:
                conn.execute("UPDATE cases SET status = 'ERROR', updated_at = ? WHERE id = ?", (db.now(), case_id))
            return "ERROR"
        _advance(conn, case_id, state, next_step, status)
        maybe_crash(f"after_{step}")


# --- steps ------------------------------------------------------------------------

def step_extract(conn, cd, case_id, row, state):
    raw = row["raw"]
    fields, method = extract(raw)
    data = fields.model_dump()
    flat = {k: (v["value"] if isinstance(v, dict) else v) for k, v in data.items()}
    if flat.get("currency"):
        flat["currency"] = flat["currency"].strip().upper()
    grounded = {k: quote_is_grounded(v.get("quote"), raw) for k, v in data.items() if isinstance(v, dict)}
    state.update(fields=flat, quotes={k: v.get("quote") for k, v in data.items() if isinstance(v, dict)},
                 grounded=grounded, extract_method=method)

    # Money gets a second, independent reading. If the two disagree, a person decides.
    second, ambiguous = best_amount(raw)
    regex_currency, currency_conflict = currency_reading(raw)
    extra = {}
    if ambiguous:
        extra["amount_ambiguous"] = True
    elif second.value and flat.get("amount") and to_paise(flat["amount"]) != to_paise(second.value):
        extra["amount_disagreement"] = f"{method} read {flat['amount']}, the label reader read {second.value}"
    if currency_conflict:
        extra["currency_conflict"] = "several currencies named in the email"
    elif regex_currency.value and flat.get("currency") and regex_currency.value != flat["currency"]:
        extra["currency_conflict"] = f"{method} read {flat['currency']}, the text names {regex_currency.value}"
    state["extract_checks"] = extra

    sender_ev = db.add_evidence(
        conn, case_id, "claim", "email",
        f"The email claims to be from {flat.get('sender_name')} <{flat.get('sender_email')}>. This is text in a pasted "
        f"email; there is no trusted mail channel here, so the sender is not authenticated.",
        data={"sender_name": flat.get("sender_name"), "sender_email": flat.get("sender_email")})
    invoice_ev = db.add_evidence(
        conn, case_id, "claim", "email",
        f"The invoice states: number {flat.get('invoice_number')}, {flat.get('currency')} {flat.get('amount')}, "
        f"payable to account {flat.get('bank_account_number')} (IFSC {flat.get('bank_ifsc')}), "
        f"PO {flat.get('po_number') or 'not given'}",
        data={"fields": flat, "quotes": state["quotes"]})
    reader = llm.name(method)
    db.log_event(conn, case_id, "extract",
                 f"Invoice {flat.get('invoice_number') or '(no number)'} read by {reader}",
                 evidence=[sender_ev, invoice_ev])
    return "resolve", None


def step_resolve(conn, cd, case_id, row, state):
    res = checks.resolve_vendor(conn, case_id, cd, state["fields"])
    state["resolution"] = res
    who = (res.get("vendor") or res.get("claimed") or {}).get("legal_name") or "an unknown company"
    db.log_event(conn, case_id, "resolve",
                 f"{who} · {res['sender_domain']} is "
                 + {"registered": "its registered domain", "lookalike": "a look-alike domain",
                    "unrelated": "not its domain", "listed_alternate": "only an alternate domain"}.get(
                     res["domain_status"], "an unknown domain"),
                 evidence=res["evidence"])
    return "verify_sender", None


def step_verify_sender(conn, cd, case_id, row, state):
    sender = checks.verify_sender(conn, case_id, cd, state["fields"], state["resolution"])
    state["sender"] = sender
    said = {"current": f"Profile lists a current role: {sender.get('title')}",
            "left": f"Profile lists the role as ended ({(sender.get('left_on') or '')[:7]})",
            "not_found": "No public profile found", "role_mailbox": "Shared mailbox; no profile to check",
            "error": "Profile lookup failed", "unverifiable": "No company to check against"}
    db.log_event(conn, case_id, "verify_sender", said.get(sender["status"], sender["status"]),
                 evidence=sender["evidence"])
    return "profile", None


def step_profile(conn, cd, case_id, row, state):
    res = state["resolution"]
    company_id = ((res.get("vendor") or {}).get("crustdata_company_id")
                  or (res.get("company") or {}).get("crustdata_company_id"))
    # Only new vendors need a full profile; known vendors were checked at onboarding.
    if company_id and not res.get("vendor"):
        state["profile"] = checks.company_profile(conn, case_id, cd, company_id)
        p = state["profile"]
        db.log_event(conn, case_id, "profile",
                     f"Founded {p.get('year_founded') or 'unknown'} · {p.get('headcount') or 'unknown'} employees",
                     evidence=p["evidence"])
    else:
        state["profile"] = {}
        db.log_event(conn, case_id, "profile", "Skipped: vendor already onboarded" if res.get("vendor")
                     else "Skipped: no company found to profile")
    return "decide", None


def step_decide(conn, cd, case_id, row, state):
    decision, findings, match = policy.decide(conn, case_id, state["fields"], state["grounded"],
                                              state["resolution"], state["sender"], state.get("extract_checks"))
    state.update(decision=decision, findings=findings, match=match)
    state.pop("final_decision", None)
    stops = sum(1 for f in findings if f["severity"] in ("block", "hold", "duplicate"))
    db.log_event(conn, case_id, "decide",
                 {"PAY": "All rules pass: pay", "HOLD": "Hold", "REJECTED": "Reject",
                  "DUPLICATE": "Duplicate"}[decision] + (f" · {stops} problem{'s' if stops != 1 else ''}" if stops else ""),
                 evidence=sorted({e for f in findings for e in f["evidence"]}))
    return "explain", None


def step_explain(conn, cd, case_id, row, state):
    f = state["fields"]
    summary = {k: f.get(k) for k in ("vendor_name", "invoice_number", "amount", "currency", "sender_email")}
    state["memo"], state["memo_method"] = memo.write_memo(
        state["decision"], state["findings"], db.get_evidence(conn, case_id), summary)
    db.log_event(conn, case_id, "explain", f"Explanation written ({state['memo_method']})")
    status = None if state["decision"] == "PAY" else FINAL[state["decision"]]
    return "act", status


def step_act(conn, cd, case_id, row, state):
    if (state.get("human_decision") or {}).get("action") == "reject":
        state["final_decision"] = "REJECTED"
        return "notify", "REJECTED"
    if state["decision"] != "PAY":
        db.log_event(conn, case_id, "act", "Nothing paid")
        return "notify", FINAL[state["decision"]]  # nothing to pay

    conflict, payment = _reserve(conn, case_id, state)
    if conflict:
        return _stop_before_paying(conn, case_id, state, *conflict)
    if payment["status"] == "paid":
        return "notify", "PAID"  # resumed after the ledger was already settled

    try:
        ref, created = bank.payout(payment["payment_key"], payment["account"], payment["ifsc"],
                                   payment["amount_paise"], payment["currency"])
    except bank.IdempotencyConflict as exc:  # keep the reservation: the bank already holds something for this key
        return _stop_before_paying(conn, case_id, state, "bank_refused", "hold",
                                   f"The bank refused the payout: {exc}",
                                   {"evidence": "a person to reconcile this payout with the bank", "ask": "ap-lead"})
    db.log_event(conn, case_id, "act",
                 f"Bank payout {ref} for {fmt(payment['amount_paise'])}" +
                 ("" if created else " (replayed: the bank returned the existing payout, no new money moved)"))
    maybe_crash("after_bank")  # the dangerous moment: money moved, our ledger still says 'reserved'

    with conn:  # settle the ledger and the case together
        conn.execute("UPDATE payments SET status = 'paid', bank_ref = ?, paid_at = ? WHERE payment_key = ?",
                     (ref, db.now(), payment["payment_key"]))
        state["bank_ref"] = ref
        conn.execute("UPDATE cases SET state = ?, step = 'notify', status = 'PAID', updated_at = ? WHERE id = ?",
                     (json.dumps(state, default=str), db.now(), case_id))
    return "notify", "PAID"


def _reserve(conn, case_id, state):
    """Re-check and reserve in one write transaction. Returns (conflict, payment_row)."""
    f, match = state["fields"], state.get("match") or {}
    vendor = state["resolution"]["vendor"]
    key, amount, currency = match["payment_key"], match["amount_paise"], match["currency"]
    account = (f.get("bank_account_number") or "").replace(" ", "")
    ifsc = (f.get("bank_ifsc") or "").replace(" ", "").upper()
    inv = checks.normalize_invoice_number(f.get("invoice_number"))
    po_number, receipt_id = match.get("po_number"), match.get("receipt_id")

    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")  # one writer at a time: nobody else can validate the same capacity now
    try:
        existing = conn.execute("SELECT * FROM payments WHERE payment_key = ?", (key,)).fetchone()
        if existing:
            conn.rollback()
            same = (existing["case_id"] == case_id and existing["amount_paise"] == amount
                    and existing["account"] == account and (existing["ifsc"] or "").upper() == ifsc
                    and existing["currency"] == currency)
            if same:
                return None, existing  # our own reservation, found again after a crash
            other_same_details = (existing["amount_paise"] == amount and existing["account"] == account
                                  and (existing["ifsc"] or "").upper() == ifsc and existing["currency"] == currency)
            return ("duplicate" if other_same_details else "changed_resend",
                    "duplicate" if other_same_details else "block",
                    f"Invoice {inv} is already {existing['status']} by case {existing['case_id']} "
                    f"({fmt(existing['amount_paise'])} {existing['currency']} to account ending "
                    f"{existing['account'][-4:]})", None), None

        po = policy.find_po(po_number)
        problem = None
        mismatches = books.reconcile(conn)[0]
        if mismatches:  # fail closed: someone or something changed our books; no new money moves until fixed
            problem = ("books_mismatch", "hold",
                       f"Payments are paused: our ledger and the bank disagree ({mismatches[0]}"
                       + (f", and {len(mismatches) - 1} more" if len(mismatches) > 1 else "") + ")",
                       {"evidence": "a reconciliation of our ledger with the bank", "ask": "ap-lead"})
        elif (not po or po.get("status", "approved") != "approved" or po["vendor_id"] != vendor["vendor_id"]
                or po.get("currency") != currency):
            problem = ("po_changed", "hold", f"{po_number} is no longer an approved {currency} purchase order "
                                             f"for this vendor", {"evidence": "a valid purchase order", "ask": "ap-lead"})
        else:
            authorised = rupees_to_paise(po["approved_amount"])
            used = policy.po_allocated_paise(conn, po["po_number"])
            if used + amount > authorised:
                problem = ("po_capacity", "hold",
                           f"{po['po_number']} authorises {fmt(authorised)} and {fmt(used)} is already reserved or "
                           f"paid, so this {fmt(amount)} invoice does not fit",
                           {"evidence": f"an amendment raising {po['po_number']}", "ask": "ap-lead"})
            else:
                receipt = next((r for r in policy.receipt_records(conn, po) if r["id"] == receipt_id), None)
                taken = conn.execute("SELECT * FROM receipt_allocations WHERE receipt_id = ?", (receipt_id,)).fetchone()
                if not receipt or receipt["amount_paise"] != amount or receipt["currency"] != currency:
                    problem = ("receipt_changed", "hold", f"Receipt {receipt_id} no longer matches this invoice",
                               _receipt_need(po, amount, currency))
                elif taken and taken["payment_key"] != key:
                    problem = ("receipt_taken", "hold",
                               f"Receipt {receipt_id} was already used to pay another invoice (case {taken['case_id']}); "
                               f"one delivery confirmation can back only one payment",
                               _receipt_need(po, amount, currency))
        if problem:
            conn.rollback()
            return problem, None

        now = db.now()
        conn.execute(
            "INSERT INTO payments (payment_key, case_id, vendor_id, invoice_number, amount, amount_paise, currency, "
            "account, ifsc, po_number, receipt_id, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'reserved',?)",
            (key, case_id, vendor["vendor_id"], inv, amount / 100, amount, currency, account, ifsc,
             po["po_number"], receipt_id, now))
        conn.execute(
            "INSERT INTO receipt_allocations (receipt_id, po_number, payment_key, case_id, amount_paise, currency, "
            "created_at) VALUES (?,?,?,?,?,?,?)", (receipt_id, po["po_number"], key, case_id, amount, currency, now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return None, conn.execute("SELECT * FROM payments WHERE payment_key = ?", (key,)).fetchone()


def _receipt_need(po, amount, currency):
    return {"evidence": f"a separate receipt confirming delivery of '{po['description']}' ({fmt(amount)})",
            "ask": po.get("requested_by") or "ops-lead", "provide_as": "receipt",
            "po_number": po["po_number"], "amount_paise": amount, "currency": currency}


def _stop_before_paying(conn, case_id, state, code, severity, text, needs):
    """Record why the payment did not go ahead, and send the case back through the explanation."""
    ev = db.add_evidence(conn, case_id, "fact", "ledger", text)
    state["findings"].append({"code": code, "severity": severity, "text": text, "evidence": [ev], "needs": needs})
    state["decision"] = {"duplicate": "DUPLICATE", "block": "REJECTED"}.get(severity, "HOLD")
    db.log_event(conn, case_id, "act", f"Not paying: {text}", level="warn", evidence=[ev])
    return "explain", None


def step_notify(conn, cd, case_id, row, state):
    decision = state.get("final_decision") or state["decision"]
    subject = {
        "PAY": f"Paid invoice {state['fields'].get('invoice_number')}",
        "DUPLICATE": f"Blocked duplicate invoice {state['fields'].get('invoice_number')}",
        "HOLD": f"Needs one thing: invoice {state['fields'].get('invoice_number')}",
        "REJECTED": f"Rejected invoice {state['fields'].get('invoice_number')}",
    }[decision]
    needs = [f["needs"] for f in state.get("findings", []) if f.get("needs")] if decision == "HOLD" else []
    body = f"{describe_status(conn, case_id, final=FINAL[decision])}\n\nNext: {(state.get('memo') or {}).get('next_step', '')}"
    if needs:
        body += "\n\nTo pay this I need:\n" + "\n".join(f"- {n['evidence']} (ask: {n['ask']})" for n in needs)
    body += f"\n\nCase {case_id}"
    recipients = config.TEAM_EMAILS
    asked = {n["ask"] for n in needs}
    if asked:  # send the request to whoever can answer it, if they are on the team
        named = [t for t in config.TEAM_EMAILS if t in asked or t.split("@")[0] in asked]
        recipients = named or config.TEAM_EMAILS
    for to in recipients:
        if to not in config.TEAM_EMAILS:  # hackathon rule: our own team only, never the vendor
            continue
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO outbox (msg_key, case_id, to_addr, subject, body, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (f"{case_id}:{decision}:{state.get('round', 0)}:{to}", case_id, to, subject, body, db.now()))
    db.log_event(conn, case_id, "notify", f"Told the team: {subject}")
    return "done", FINAL[decision]


HANDLERS = {
    "extract": step_extract, "resolve": step_resolve, "verify_sender": step_verify_sender,
    "profile": step_profile, "decide": step_decide, "explain": step_explain,
    "act": step_act, "notify": step_notify,
}


# --- what the team can do -------------------------------------------------------------

def human_resolve(conn, case_id, action, who="ap-lead", note=""):
    """A person can reject a held case. There is no blanket approval: holds clear only with evidence."""
    if action != "reject":
        raise ValueError("Kanaku Pulla has no 'approve anyway'. Supply the specific missing evidence the hold asks for.")
    row, state = _load(conn, case_id)
    if row["status"] != "HOLD":
        raise ValueError(f"case {case_id} is {row['status']}, not on hold")
    state["human_decision"] = {"action": "reject", "by": who, "note": note, "at": db.now()}
    state["round"] = state.get("round", 0) + 1
    with conn:
        conn.execute("UPDATE cases SET state = ?, step = 'act', status = 'RUNNING', updated_at = ? WHERE id = ?",
                     (json.dumps(state, default=str), db.now(), case_id))
    db.log_event(conn, case_id, "human", f"{who} rejected the invoice" + (f": {note}" if note else ""))
    return run(conn, case_id)


def recheck(conn, case_id, who="ap-lead"):
    """Run every payment rule again on a held case, for when a record was corrected outside the app.

    Nothing is waived: it is the same decision step, against the current registers and ledger.
    """
    row, state = _load(conn, case_id)
    if row["status"] != "HOLD":
        raise ValueError(f"case {case_id} is {row['status']}, not on hold")
    state["round"] = state.get("round", 0) + 1
    with conn:
        conn.execute("UPDATE cases SET state = ?, step = 'decide', status = 'RUNNING', updated_at = ? WHERE id = ?",
                     (json.dumps(state, default=str), db.now(), case_id))
    db.log_event(conn, case_id, "human", f"{who} asked for a re-check")
    return run(conn, case_id)


def provide_receipt(conn, case_id, by, note):
    """The team supplies the missing delivery receipt; Kanaku Pulla checks it and re-decides the same case.

    `by` is a sandbox identity: it is checked against the team list but not authenticated.
    """
    row, state = _load(conn, case_id)
    if row["status"] != "HOLD":
        raise ValueError(f"case {case_id} is {row['status']}, not on hold")
    need = next((f["needs"] for f in state.get("findings", [])
                 if (f.get("needs") or {}).get("provide_as") == "receipt"), None)
    if not need:
        raise ValueError(f"case {case_id} is not waiting for a receipt")
    by = (by or "").strip().lower()
    if by not in config.TEAM_EMAILS:
        raise ValueError(f"{by or 'nobody'} is not on the team; receipts must come from a team member")
    po = policy.find_po(need["po_number"])
    if not po:
        raise ValueError(f"{need['po_number']} is no longer an approved purchase order")
    receipt_id = "GRN-" + uuid.uuid4().hex[:6].upper()
    with conn:
        conn.execute(
            "INSERT INTO receipts (id, po_number, amount, note, recorded_by, recorded_at, case_id, currency) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (receipt_id, po["po_number"], need["amount_paise"] / 100, note, by, db.now(), case_id, need["currency"]))
    db.log_event(conn, case_id, "human", f"{by} (sandbox identity) recorded receipt {receipt_id}: {note}")
    state["round"] = state.get("round", 0) + 1
    with conn:
        conn.execute("UPDATE cases SET state = ?, step = 'decide', status = 'RUNNING', updated_at = ? WHERE id = ?",
                     (json.dumps(state, default=str), db.now(), case_id))
    return run(conn, case_id)


def unfinished(conn):
    return [r["id"] for r in conn.execute("SELECT id FROM cases WHERE step != 'done' ORDER BY created_at")]


def describe_status(conn, case_id, final=None):
    """One line about where the money stands, read from the ledger, not from the memo."""
    row, state = _load(conn, case_id)
    status = final or row["status"]
    pay = conn.execute("SELECT * FROM payments WHERE case_id = ? ORDER BY id DESC LIMIT 1", (case_id,)).fetchone()
    if pay and pay["status"] == "paid":
        return (f"Paid {fmt(pay['amount_paise'])} {pay['currency']} to account ending {pay['account'][-4:]} "
                f"(bank reference {pay['bank_ref']}).")
    if pay and pay["status"] == "reserved":
        return (f"Payment of {fmt(pay['amount_paise'])} reserved; bank settlement not yet confirmed"
                + (" (interrupted; resume will finish it safely)." if row["step"] != "done" else "."))
    if status == "HOLD":
        needs = [f["needs"]["evidence"] for f in state.get("findings", []) if f.get("needs")]
        return "On hold, nothing paid. Needs: " + ("; ".join(needs) or "a decision") + "."
    if status == "DUPLICATE":
        return "Not paid: this invoice is already in the ledger."
    if status == "REJECTED":
        return "Rejected, nothing paid."
    if status == "ERROR":
        return f"Stopped by an error at step '{row['step']}'; nothing new was paid. Resume will retry."
    return f"In progress (step '{row['step']}'); nothing paid yet."
