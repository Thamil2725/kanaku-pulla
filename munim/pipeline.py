"""The case runner: a resumable sequence of steps, each saved before the next starts.

If the process dies, `resume` picks each unfinished case up at the step it
had reached. Money moves only through the sandbox bank's idempotency key, and
messages only through the outbox's unique key, so a replayed step never pays
or sends twice.
"""

import json
import os
import sys
import uuid

from . import bank, checks, config, db, memo, policy
from .crustdata import Crustdata
from .extract import best_amount, extract, quote_is_grounded

STEPS = ["extract", "resolve", "verify_sender", "profile", "decide", "explain", "act", "notify", "done"]


def maybe_crash(point):
    if config.CRASH_AT and config.CRASH_AT == point:
        print(f"\n💥 Simulated crash at '{point}' (MUNIM_CRASH_AT). Run `python -m munim resume` to continue.")
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
        handler = HANDLERS[step]
        try:
            next_step, status = handler(conn, cd, case_id, row, state)
        except Exception as exc:  # unexpected tool output or bug: stop safely, keep the step for resume
            db.log_event(conn, case_id, step, f"Step failed: {exc.__class__.__name__}: {exc}", level="error")
            with conn:
                conn.execute("UPDATE cases SET status = 'ERROR', updated_at = ? WHERE id = ?", (db.now(), case_id))
            return "ERROR"
        _advance(conn, case_id, state, next_step, status)
        maybe_crash(f"after_{step}")


# --- steps ------------------------------------------------------------------------

def step_extract(conn, cd, case_id, row, state):
    fields, method = extract(row["raw"])
    data = fields.model_dump()
    flat = {k: (v["value"] if isinstance(v, dict) else v) for k, v in data.items()}
    grounded = {k: quote_is_grounded(v.get("quote"), row["raw"]) for k, v in data.items() if isinstance(v, dict)}
    state.update(fields=flat, quotes={k: v.get("quote") for k, v in data.items() if isinstance(v, dict)},
                 grounded=grounded, extract_method=method)

    # Money gets a second, independent reading. If the two disagree, a person decides.
    second, ambiguous = best_amount(row["raw"])
    extra = {}
    if ambiguous:
        extra["amount_ambiguous"] = True
    elif second.value and flat.get("amount") and checks.parse_amount(flat["amount"]) != checks.parse_amount(second.value):
        extra["amount_disagreement"] = f"{method} read {flat['amount']}, the label reader read {second.value}"
    state["extract_checks"] = extra
    ev = db.add_evidence(
        conn, case_id, "fact", "email",
        f"Email from {flat.get('sender_name')} <{flat.get('sender_email')}>: invoice "
        f"{flat.get('invoice_number')} for {flat.get('currency')} {flat.get('amount')}, payable to account "
        f"{flat.get('bank_account_number')} ({flat.get('bank_ifsc')})",
        data={"fields": flat, "quotes": state["quotes"]})
    db.log_event(conn, case_id, "extract", f"Read the email and invoice ({method})", evidence=[ev])
    return "resolve", None


def step_resolve(conn, cd, case_id, row, state):
    res = checks.resolve_vendor(conn, case_id, cd, state["fields"])
    state["resolution"] = res
    who = (res.get("vendor") or res.get("claimed") or {}).get("legal_name") or "an unknown company"
    db.log_event(conn, case_id, "resolve",
                 f"Vendor: {who}; sending domain {res['sender_domain']} is {res['domain_status']}",
                 evidence=res["evidence"])
    return "verify_sender", None


def step_verify_sender(conn, cd, case_id, row, state):
    sender = checks.verify_sender(conn, case_id, cd, state["fields"], state["resolution"])
    state["sender"] = sender
    db.log_event(conn, case_id, "verify_sender", f"Sender check: {sender['status']}", evidence=sender["evidence"])
    return "profile", None


def step_profile(conn, cd, case_id, row, state):
    res = state["resolution"]
    company_id = ((res.get("vendor") or {}).get("crustdata_company_id")
                  or (res.get("company") or {}).get("crustdata_company_id"))
    # Only new vendors need a full profile; known vendors were checked at onboarding.
    if company_id and not res.get("vendor"):
        state["profile"] = checks.company_profile(conn, case_id, cd, company_id)
        db.log_event(conn, case_id, "profile", "Pulled the company profile", evidence=state["profile"]["evidence"])
    else:
        state["profile"] = {}
    return "decide", None


def step_decide(conn, cd, case_id, row, state):
    decision, findings, match = policy.decide(conn, case_id, state["fields"], state["grounded"],
                                              state["resolution"], state["sender"], state.get("extract_checks"))
    state.update(decision=decision, findings=findings, match=match)
    state.pop("final_decision", None)
    db.log_event(conn, case_id, "decide", f"Decision: {decision}",
                 evidence=sorted({e for f in findings for e in f["evidence"]}))
    return "explain", None


def step_explain(conn, cd, case_id, row, state):
    f = state["fields"]
    summary = {k: f.get(k) for k in ("vendor_name", "invoice_number", "amount", "currency", "sender_email")}
    state["memo"], state["memo_method"] = memo.write_memo(
        state["decision"], state["findings"], db.get_evidence(conn, case_id), summary)
    db.log_event(conn, case_id, "explain", f"Wrote the decision memo ({state['memo_method']})")
    status = {"PAY": None, "DUPLICATE": "DUPLICATE", "HOLD": "HOLD", "REJECTED": "REJECTED"}[state["decision"]]
    return "act", status


def step_act(conn, cd, case_id, row, state):
    decision = state["decision"]
    human = state.get("human_decision")
    if decision == "HOLD" and human:
        decision = "PAY" if human["action"] == "approve" else "REJECTED"
        state["final_decision"] = decision
    if decision != "PAY":
        return "notify", decision  # HOLD, DUPLICATE or REJECTED: nothing to pay

    f = state["fields"]
    vendor = state["resolution"]["vendor"]
    amount = checks.parse_amount(f["amount"])
    currency = f.get("currency") or "INR"
    account = f["bank_account_number"].replace(" ", "")
    inv = checks.normalize_invoice_number(f["invoice_number"])
    key = checks.payment_key(vendor["vendor_id"], f["invoice_number"])
    match = state.get("match") or {}
    state["payment_key"] = key

    # 1. Reserve the debt in our ledger first. The unique key means a second case for the same
    #    vendor + invoice number cannot reserve it again, whatever amount or account it carries.
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO payments (payment_key, case_id, vendor_id, invoice_number, amount, currency, "
            "account, ifsc, po_number, receipt_id, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,'reserved',?)",
            (key, case_id, vendor["vendor_id"], inv, amount, currency, account, f.get("bank_ifsc"),
             match.get("po_number"), match.get("receipt_id"), db.now()))
        held = conn.execute("SELECT * FROM payments WHERE payment_key = ?", (key,)).fetchone()
    if held["case_id"] != case_id or round(held["amount"], 2) != amount or held["account"] != account:
        db.log_event(conn, case_id, "act", f"Invoice {inv} is already reserved by case {held['case_id']} "
                                           f"with different details; not paying", level="warn")
        state["final_decision"] = "REJECTED"
        return "notify", "REJECTED"
    if held["status"] == "paid":
        return "notify", "PAID"  # resumed after the ledger was already settled

    # 2. Pay. The bank honours the same key, so a retry after a crash returns the first payout.
    try:
        ref, created = bank.payout(key, account, f.get("bank_ifsc"), amount, currency)
    except bank.IdempotencyConflict as exc:
        db.log_event(conn, case_id, "act", f"Bank refused: {exc}", level="error")
        state["final_decision"] = "REJECTED"
        return "notify", "REJECTED"
    db.log_event(conn, case_id, "act",
                 f"Bank payout {ref} for ₹{amount:,.2f}" + ("" if created else " (replayed: the bank returned the "
                                                                    "existing payout, no new money moved)"))
    maybe_crash("after_bank")  # the dangerous moment: money moved, our ledger still says 'reserved'

    # 3. Settle the ledger and the case together.
    with conn:
        conn.execute("UPDATE payments SET status = 'paid', bank_ref = ?, paid_at = ? WHERE payment_key = ?",
                     (ref, db.now(), key))
        state["bank_ref"] = ref
        conn.execute("UPDATE cases SET state = ?, step = 'notify', status = 'PAID', updated_at = ? WHERE id = ?",
                     (json.dumps(state, default=str), db.now(), case_id))
    return "notify", "PAID"


def step_notify(conn, cd, case_id, row, state):
    decision = state.get("final_decision") or state["decision"]
    m = state.get("memo") or {}
    subject = {
        "PAY": f"Paid invoice {state['fields'].get('invoice_number')}",
        "DUPLICATE": f"Blocked duplicate invoice {state['fields'].get('invoice_number')}",
        "HOLD": f"Needs your decision: invoice {state['fields'].get('invoice_number')}",
        "REJECTED": f"Blocked suspected fraud: invoice {state['fields'].get('invoice_number')}",
    }[decision]
    needs = [f["needs"] for f in state.get("findings", []) if f.get("needs")] if decision == "HOLD" else []
    body = f"{m.get('headline', '')}\n\nNext: {m.get('next_step', '')}"
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
    final = {"PAY": "PAID", "DUPLICATE": "DUPLICATE", "HOLD": "HOLD", "REJECTED": "REJECTED"}[decision]
    return "done", final


HANDLERS = {
    "extract": step_extract, "resolve": step_resolve, "verify_sender": step_verify_sender,
    "profile": step_profile, "decide": step_decide, "explain": step_explain,
    "act": step_act, "notify": step_notify,
}


def human_resolve(conn, case_id, action, who="AP lead", note=""):
    """A person approves or rejects a held case; the run continues from 'act'."""
    row, state = _load(conn, case_id)
    if row["status"] != "HOLD":
        raise ValueError(f"case {case_id} is {row['status']}, not on hold")
    if action == "approve" and not (state.get("resolution") or {}).get("vendor"):
        raise ValueError("cannot pay a vendor that is not in the register; onboard it first")
    state["human_decision"] = {"action": action, "by": who, "note": note, "at": db.now()}
    state["round"] = state.get("round", 0) + 1
    with conn:
        conn.execute("UPDATE cases SET state = ?, step = 'act', status = 'RUNNING', updated_at = ? WHERE id = ?",
                     (json.dumps(state, default=str), db.now(), case_id))
    db.log_event(conn, case_id, "human", f"{who} chose to {action}" + (f": {note}" if note else ""))
    return run(conn, case_id)


def provide_receipt(conn, case_id, by, note):
    """The team supplies the missing delivery receipt; Munim checks it and re-decides the same case."""
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
            "INSERT INTO receipts (id, po_number, amount, note, recorded_by, recorded_at, case_id) VALUES (?,?,?,?,?,?,?)",
            (receipt_id, po["po_number"], need["amount"], note, by, db.now(), case_id))
    db.log_event(conn, case_id, "human", f"{by} recorded receipt {receipt_id}: {note}")
    state["round"] = state.get("round", 0) + 1
    with conn:
        conn.execute("UPDATE cases SET state = ?, step = 'decide', status = 'RUNNING', updated_at = ? WHERE id = ?",
                     (json.dumps(state, default=str), db.now(), case_id))
    return run(conn, case_id)


def unfinished(conn):
    return [r["id"] for r in conn.execute("SELECT id FROM cases WHERE step != 'done' ORDER BY created_at")]
