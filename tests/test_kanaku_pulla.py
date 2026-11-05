"""Regression tests for Kanaku Pulla's payment controls (offline: cached Crustdata, regex reader, template memos)."""

import json
import sqlite3
import subprocess
import sys

import httpx
import pytest

from conftest import ROOT
from kanaku_pulla import bank, config, db, extract, memo, pipeline
from kanaku_pulla.crustdata import Crustdata, CrustdataError

FIX = ROOT / "fixtures"
DEMO_TIME_PROVENANCE = "2026-11-05"  # cached Crustdata answers were retrieved on this date


# --- helpers ---------------------------------------------------------------------------

def load(name, *replacements):
    text = (FIX / name).read_text(encoding="utf-8")
    for old, new in replacements:
        assert old in text, f"{old!r} not in {name}"
        text = text.replace(old, new)
    return text


def process(conn, raw, name="test"):
    case_id = pipeline.create_case(conn, raw, source_name=name)
    pipeline.run(conn, case_id)
    return case_id, status(conn, case_id)


def run_until_act(conn, case_id):
    """Advance a case through decision and explanation, stopping before it acts."""
    from kanaku_pulla.crustdata import Crustdata as _C
    cd = _C(conn)
    while True:
        row, state = pipeline._load(conn, case_id)
        if row["step"] in ("act", "done"):
            return state
        nxt, st = pipeline.HANDLERS[row["step"]](conn, cd, case_id, row, state)
        pipeline._advance(conn, case_id, state, nxt, st)


def status(conn, case_id):
    return conn.execute("SELECT status FROM cases WHERE id = ?", (case_id,)).fetchone()[0]


def state_of(conn, case_id):
    return json.loads(conn.execute("SELECT state FROM cases WHERE id = ?", (case_id,)).fetchone()[0])


def payouts():
    return bank.all_payouts()


def add_receipt_to_po(env, po_number, receipt):
    pos = json.loads(env.purchase_orders.read_text(encoding="utf-8"))
    for po in pos:
        if po["po_number"] == po_number:
            po["receipts"].append(receipt)
    env.purchase_orders.write_text(json.dumps(pos), encoding="utf-8")


# --- behaviour to preserve ---------------------------------------------------------------

def test_five_fixtures_and_receipt_resume(env):
    c = env.conn
    assert process(c, load("01_razorpay_invoice.txt"))[1] == "PAID"
    assert process(c, load("02_bank_change_request.txt"))[1] == "REJECTED"
    assert process(c, load("03_resent_under_alias.txt"))[1] == "DUPLICATE"
    held, st = process(c, load("04_needs_receipt.txt"))
    assert st == "HOLD"
    pipeline.provide_receipt(c, held, "ops-lead@kanaku.local", "Payroll onboarding done 5 Nov")
    assert status(c, held) == "PAID"
    assert process(c, load("05_modified_resend.txt"))[1] == "REJECTED"
    assert len(payouts()) == 2


def test_subtotal_is_not_the_payable_amount():
    value, ambiguous = extract.best_amount("Subtotal: INR 1,000; Total: INR 1,180")
    assert value.value == "1180.00" and not ambiguous


def test_two_different_totals_are_ambiguous():
    _, ambiguous = extract.best_amount("Total: INR 1,180\nTotal: INR 1,280")
    assert ambiguous


def test_web_pages_render(env):
    from fastapi.testclient import TestClient
    from kanaku_pulla.web import app
    case_id, _ = process(env.conn, load("02_bank_change_request.txt"))
    held, _ = process(env.conn, load("04_needs_receipt.txt"))
    odd, _ = process(env.conn, load("01_razorpay_invoice.txt", ("1,18,000.00", "one lakh eighteen thousand")))
    client = TestClient(app)
    paths = ["/", "/new", "/exceptions", "/cases", "/cases?status=HOLD", "/ledger", "/rules", "/policy", "/demo"]
    for cid in (case_id, held, odd):
        paths += [f"/cases/{cid}", f"/cases/{cid}/evidence", f"/cases/{cid}/activity", f"/cases/{cid}/email"]
    for path in paths:
        assert client.get(path).status_code == 200, path
    assert client.get("/cases/C-NOPE").status_code == 200  # friendly "no such case" page
    assert held in client.get("/exceptions").text
    assert "Approve anyway" not in client.get(f"/cases/{held}").text
    r = client.post(f"/cases/{held}/resolve", data={"action": "approve"})
    assert "no &#x27;approve anyway&#x27;" in r.text or "approve anyway" in r.text.lower()
    assert payouts() == []


def test_submitting_shows_live_progress_until_done(env):
    import time
    from fastapi.testclient import TestClient
    from kanaku_pulla.web import app
    client = TestClient(app)
    r = client.post("/cases", data={"raw": load("01_razorpay_invoice.txt")}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/progress")
    page = client.get(r.headers["location"])
    assert page.status_code == 200 and 'role="progressbar"' in page.text
    url = r.headers["location"] + ".json"
    for _ in range(100):
        d = client.get(url).json()
        if d["done"]:
            break
        time.sleep(0.05)
    assert d["done"] and d["status"] == "PAID" and d["percent"] == 100
    assert "Open the decision" in d["html"] and len(payouts()) == 1


# --- 1. receipts and PO capacity are reserved atomically -------------------------------------

def test_two_invoices_cannot_share_one_receipt(env):
    c = env.conn
    a = pipeline.create_case(c, load("01_razorpay_invoice.txt"), "a")
    b = pipeline.create_case(c, load("01_razorpay_invoice.txt", ("RZP/2026/0042", "RZP/2026/0043")), "b")
    sa, sb = run_until_act(c, a), run_until_act(c, b)
    assert sa["decision"] == sb["decision"] == "PAY"
    assert sa["match"]["receipt_id"] == sb["match"]["receipt_id"] == "GRN-2026-311"
    pipeline.run(c, a)
    pipeline.run(c, b)
    assert len(payouts()) == 1
    assert sorted([status(c, a), status(c, b)]) == ["HOLD", "PAID"]
    used = c.execute("SELECT COUNT(*) FROM receipt_allocations WHERE receipt_id = 'GRN-2026-311'").fetchone()[0]
    assert used == 1
    loser = a if status(c, a) == "HOLD" else b
    assert any(f["code"] == "receipt_taken" for f in state_of(c, loser)["findings"])


def test_shared_po_capacity_is_never_exceeded(env):
    c = env.conn
    # PO-2026-131 authorises ₹59,000. Two separate receipts, two separate invoices: only one fits.
    add_receipt_to_po(env, "PO-2026-131", {"id": "GRN-T-1", "amount": 59000, "currency": "INR",
                                           "note": "setup", "recorded_by": "ops-lead@kanaku.local",
                                           "recorded_at": "2026-11-05"})
    add_receipt_to_po(env, "PO-2026-131", {"id": "GRN-T-2", "amount": 59000, "currency": "INR",
                                           "note": "setup again", "recorded_by": "ops-lead@kanaku.local",
                                           "recorded_at": "2026-11-05"})
    a = pipeline.create_case(c, load("04_needs_receipt.txt"), "a")
    b = pipeline.create_case(c, load("04_needs_receipt.txt", ("RZP/2026/0057", "RZP/2026/0058")), "b")
    assert run_until_act(c, a)["decision"] == "PAY"
    assert run_until_act(c, b)["decision"] == "PAY"
    pipeline.run(c, a)
    pipeline.run(c, b)
    assert len(payouts()) == 1
    assert sorted([status(c, a), status(c, b)]) == ["HOLD", "PAID"]


def test_reservation_race_with_separate_connections(env):
    """Two connections validate the same receipt; the database lets only one reserve it."""
    c1, c2 = env.conn, db.connect()
    a = pipeline.create_case(c1, load("01_razorpay_invoice.txt"), "a")
    b = pipeline.create_case(c2, load("01_razorpay_invoice.txt", ("RZP/2026/0042", "RZP/2026/0044")), "b")
    run_until_act(c1, a)
    run_until_act(c2, b)
    pipeline.run(c2, b)
    pipeline.run(c1, a)
    assert len(payouts()) == 1
    c2.close()


def test_real_process_crash_after_bank_pays_once(env, subprocess_env):
    env.conn.close()
    crash = dict(subprocess_env, KANAKU_CRASH_AT="after_bank")
    r = subprocess.run([sys.executable, "-m", "kanaku_pulla", "run", str(FIX / "01_razorpay_invoice.txt")],
                       cwd=ROOT, env=crash, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 137, r.stdout + r.stderr
    conn = db.connect()
    row = conn.execute("SELECT * FROM cases").fetchone()
    assert row["step"] == "act" and row["status"] != "PAID"
    assert conn.execute("SELECT status FROM payments").fetchone()[0] == "reserved"
    assert len(payouts()) == 1
    # The page must not claim a completed payment before the ledger is settled.
    assert "Paid" not in pipeline.describe_status(conn, row["id"])
    # A changed copy arriving mid-recovery is rejected.
    assert process(conn, load("01_razorpay_invoice.txt", ("1,18,000.00", "1,81,000.00")))[1] == "REJECTED"
    conn.close()
    r = subprocess.run([sys.executable, "-m", "kanaku_pulla", "resume"], cwd=ROOT, env=subprocess_env,
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr
    conn = db.connect()
    assert conn.execute("SELECT status FROM cases WHERE id = ?", (row["id"],)).fetchone()[0] == "PAID"
    assert conn.execute("SELECT status FROM payments WHERE case_id = ?", (row["id"],)).fetchone()[0] == "paid"
    assert len(payouts()) == 1
    conn.close()


# --- 2. currency and the complete payment destination ----------------------------------------

def test_usd_invoice_is_not_paid(env):
    raw = load("01_razorpay_invoice.txt", ("Amount: INR 1,18,000.00 (incl. GST 18%)", "Currency: USD\nAmount: 1,18,000.00"))
    _, st = process(env.conn, raw)
    assert st != "PAID"
    assert payouts() == []


def test_changed_ifsc_is_not_paid(env):
    _, st = process(env.conn, load("01_razorpay_invoice.txt", ("HDFC0000123", "ICIC0000123")))
    assert st != "PAID"
    assert payouts() == []


def test_bank_replay_compares_the_full_payload(env):
    ref, created = bank.payout("key-1", "50200012345678", "HDFC0000123", 11800000, "INR")
    assert created
    assert bank.payout("key-1", "50200012345678", "HDFC0000123", 11800000, "INR") == (ref, False)
    for changed in (("50200099999999", "HDFC0000123", 11800000, "INR"),
                    ("50200012345678", "ICIC0000123", 11800000, "INR"),
                    ("50200012345678", "HDFC0000123", 11800001, "INR"),
                    ("50200012345678", "HDFC0000123", 11800000, "USD")):
        with pytest.raises(bank.IdempotencyConflict):
            bank.payout("key-1", *changed)
    assert len(payouts()) == 1


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "nan", "inf", ""])
def test_malformed_amounts_are_rejected(bad):
    from kanaku_pulla.money import to_paise
    assert to_paise(bad) is None


# --- 3. no blanket approval bypass -------------------------------------------------------------

def test_generic_approval_cannot_pay_a_held_invoice(env):
    case_id, st = process(env.conn, load("04_needs_receipt.txt"))
    assert st == "HOLD"
    with pytest.raises(ValueError):
        pipeline.human_resolve(env.conn, case_id, "approve")
    assert status(env.conn, case_id) == "HOLD"
    assert payouts() == []


def test_reject_moves_no_money(env):
    case_id, _ = process(env.conn, load("04_needs_receipt.txt"))
    pipeline.human_resolve(env.conn, case_id, "reject")
    assert status(env.conn, case_id) == "REJECTED"
    assert payouts() == []


def test_receipt_cannot_clear_an_unrelated_bank_hold(env):
    raw = load("04_needs_receipt.txt", ("A/c No: 50200012345678", "A/c No: 50200099999999"))
    case_id, st = process(env.conn, raw)
    assert st == "HOLD"
    pipeline.provide_receipt(env.conn, case_id, "ops-lead@kanaku.local", "delivered")
    assert status(env.conn, case_id) == "HOLD"
    assert payouts() == []


def test_receipt_must_come_from_the_team(env):
    case_id, _ = process(env.conn, load("04_needs_receipt.txt"))
    with pytest.raises(ValueError):
        pipeline.provide_receipt(env.conn, case_id, "stranger@example.com", "delivered")


# --- 4. provenance supports the claim -------------------------------------------------------------

def test_model_prose_never_becomes_a_verified_fact(env, monkeypatch):
    monkeypatch.setattr(config, "llm_providers", lambda: ["gemini"])
    monkeypatch.setattr("kanaku_pulla.extract.llm.structured", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(memo, "_model_summary", lambda *a, **k: ([{"text": "The moon is made of cheese.",
                                                                   "evidence": ["E1"]}], "gemini"))
    case_id, _ = process(env.conn, load("01_razorpay_invoice.txt"))
    m = state_of(env.conn, case_id)["memo"]
    evidence = {e["id"]: e for e in db.get_evidence(env.conn, case_id)}
    assert all("cheese" not in f["text"] for f in m["verified_facts"])
    for fact in m["verified_facts"]:
        (eid,) = fact["evidence"]
        assert evidence[eid]["kind"] == "fact" and fact["text"] == evidence[eid]["summary"]
    assert any("cheese" in s["text"] for s in m["model_summary"])  # shown, but labelled as model-written


def test_summary_with_unknown_evidence_is_rejected():
    assert memo.citation_problems([{"text": "x", "evidence": ["E99"]}], {"E1"})
    assert memo.citation_problems([{"text": "x", "evidence": []}], {"E1"})
    assert not memo.citation_problems([{"text": "x", "evidence": ["E1"]}], {"E1"})


def test_failed_lookup_is_unavailable_not_proof(env):
    raw = load("01_razorpay_invoice.txt", ("neha.kamath@razorpay.com", "neha.kamath@uncached-vendor.in"))
    case_id, _ = process(env.conn, raw)
    ev = db.get_evidence(env.conn, case_id)
    failed = [e for e in ev if "uncached-vendor.in" in e["summary"] and e["source"].startswith("crustdata")]
    assert failed and all(e["kind"] == "unavailable" for e in failed)
    m = state_of(env.conn, case_id)["memo"]
    assert not any("uncached-vendor.in" in f["text"] for f in m["verified_facts"])


def test_profile_wording_does_not_claim_current_employment(env):
    case_id, _ = process(env.conn, load("01_razorpay_invoice.txt"))
    for e in db.get_evidence(env.conn, case_id):
        assert "currently works" not in e["summary"]
    assert any(e["summary"].startswith("The public profile of") and "lists a current role" in e["summary"]
               for e in db.get_evidence(env.conn, case_id))


# --- extra cases from the Argon review ---------------------------------------------------------------

def test_renamed_reminder_is_held_until_a_new_delivery_is_confirmed(env):
    c = env.conn
    assert process(c, load("01_razorpay_invoice.txt"))[1] == "PAID"
    case_id, st = process(c, load("06_renamed_reminder.txt"))
    assert st == "HOLD"
    assert any(f["code"] == "duplicate_suspect" for f in state_of(c, case_id)["findings"])
    assert len(payouts()) == 1
    # Only a delivery confirmed for this specific invoice turns it into a new charge.
    pipeline.provide_receipt(c, case_id, "ops-lead@kanaku.local", "November gateway usage reconciled")
    assert status(c, case_id) == "PAID"
    assert len(payouts()) == 2


def test_named_sender_with_ended_role_on_the_real_domain_is_held(env):
    raw = load("01_razorpay_invoice.txt", ("Neha Kamath <neha.kamath@razorpay.com>",
                                           "Baddal Verma <baddal.verma@razorpay.com>"))
    case_id, st = process(env.conn, raw)
    assert st == "HOLD"
    assert any(f["code"] == "sender_left" for f in state_of(env.conn, case_id)["findings"])
    assert payouts() == []


# --- 5. the sender is a claim, not an authenticated identity ------------------------------------------

def test_pasted_sender_is_only_a_claim(env):
    case_id, _ = process(env.conn, load("01_razorpay_invoice.txt"))
    ev = db.get_evidence(env.conn, case_id)
    sender = [e for e in ev if e["kind"] == "claim" and "neha.kamath@razorpay.com" in e["summary"]]
    assert sender and "not authenticated" in sender[0]["summary"]
    assert not any(e["kind"] == "fact" and e["source"] == "email" for e in ev)


# --- Crustdata client failure handling (mocked; no network) -----------------------------------------

class _Resp:
    def __init__(self, status, body=None, headers=None, text=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def _client(env):
    return Crustdata(env.conn, api_key="test-key", use_cache=False, max_attempts=3)


def test_timeouts_retry_a_bounded_number_of_times(env, monkeypatch):
    calls = []
    monkeypatch.setattr("kanaku_pulla.crustdata.time.sleep", lambda s: None)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(httpx.ReadTimeout("t")))
    with pytest.raises(CrustdataError):
        _client(env).post("/company/identify", {"domains": ["x.com"]})
    assert len(calls) == 3


def test_429_then_success(env, monkeypatch):
    replies = [_Resp(429, {}, {"x-ratelimit-reset": "1"}), _Resp(200, [{"matched_on": "x.com", "matches": []}])]
    monkeypatch.setattr("kanaku_pulla.crustdata.time.sleep", lambda s: None)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: replies.pop(0))
    data, meta = _client(env).post("/company/identify", {"domains": ["x.com"]})
    assert data[0]["matched_on"] == "x.com"


def test_malformed_response_gives_up(env, monkeypatch):
    monkeypatch.setattr("kanaku_pulla.crustdata.time.sleep", lambda s: None)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(200, None, text="<html>oops"))
    with pytest.raises(CrustdataError):
        _client(env).post("/company/identify", {"domains": ["x.com"]})


def test_stale_cache_is_not_used(env, monkeypatch):
    monkeypatch.setattr(config, "CACHE_MAX_AGE_HOURS", 1)
    with env.conn:
        env.conn.execute("UPDATE api_cache SET retrieved_at = '2020-01-01T00:00:00+00:00'")
    with pytest.raises(CrustdataError):  # offline: a stale answer is not served, and a refetch is impossible
        Crustdata(env.conn).identify_domain("razorpay.com")


# --- schema upgrade without data loss --------------------------------------------------------------------

def test_old_database_upgrades_and_reports_shared_receipts(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
    CREATE TABLE payments (id INTEGER PRIMARY KEY AUTOINCREMENT, payment_key TEXT NOT NULL UNIQUE, case_id TEXT NOT NULL,
      vendor_id TEXT NOT NULL, invoice_number TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL,
      account TEXT NOT NULL, ifsc TEXT, po_number TEXT, receipt_id TEXT, status TEXT NOT NULL, bank_ref TEXT,
      created_at TEXT NOT NULL, paid_at TEXT);
    CREATE TABLE receipts (id TEXT PRIMARY KEY, po_number TEXT NOT NULL, amount REAL NOT NULL, note TEXT,
      recorded_by TEXT NOT NULL, recorded_at TEXT NOT NULL, case_id TEXT);
    INSERT INTO payments VALUES (1,'k1','C-1','V-RZP','RZP-2026-42',118000.0,'INR','50200012345678','HDFC0000123',
      'PO-2026-118','GRN-2026-311','paid','PAYOUT-1','2026-11-05',NULL);
    INSERT INTO payments VALUES (2,'k2','C-2','V-RZP','RZP-2026-43',118000.0,'INR','50200012345678','HDFC0000123',
      'PO-2026-118','GRN-2026-311','paid','PAYOUT-2','2026-11-05',NULL);
    """)
    old.commit()
    old.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 2  # nothing deleted
    assert [r[0] for r in conn.execute("SELECT amount_paise FROM payments ORDER BY id")] == [11800000, 11800000]
    assert conn.execute("SELECT COUNT(*) FROM receipt_allocations").fetchone()[0] == 1
    issues = conn.execute("SELECT detail FROM migration_issues").fetchall()
    assert issues and "GRN-2026-311" in issues[0][0]
    db.connect().close()  # running the upgrade again is harmless
    assert conn.execute("SELECT COUNT(*) FROM migration_issues").fetchone()[0] == len(issues)


# --- hand-edited books, re-check, and the model path ----------------------------------------------------

def _po(env, number, amount, receipts):
    pos = json.loads(env.purchase_orders.read_text(encoding="utf-8"))
    pos.append({"po_number": number, "vendor_id": "V-RZP", "description": "Gateway integration project",
                "approved_amount": amount, "currency": "INR", "approved_by": "finance-head@kanaku.local",
                "approved_on": "2026-10-01", "requested_by": "ops-lead@kanaku.local", "status": "approved",
                "receipts": [{"id": f"GRN-{number}-{i}", "amount": r, "currency": "INR", "note": "delivered",
                              "recorded_by": "ops-lead@kanaku.local", "recorded_at": "2026-11-01"}
                             for i, r in enumerate(receipts)]})
    env.purchase_orders.write_text(json.dumps(pos), encoding="utf-8")


def _bill(number, po, amount="50,000.00"):
    return load("01_razorpay_invoice.txt", ("RZP/2026/0042", number), ("PO-2026-118", po),
                ("INR 1,18,000.00 (incl. GST 18%)", f"INR {amount}"))


def test_lowering_a_paid_ledger_amount_frees_no_room_on_the_po(env):
    c = env.conn
    _po(env, "PO-2026-900", 100000, [50000, 50000, 50000])
    assert process(c, _bill("RZP/2026/0901", "PO-2026-900"))[1] == "PAID"
    assert process(c, _bill("RZP/2026/0902", "PO-2026-900"))[1] == "PAID"
    with c:
        c.execute("UPDATE payments SET amount_paise = 0, amount = 0 WHERE id = (SELECT MIN(id) FROM payments)")
    case_id, st = process(c, _bill("RZP/2026/0903", "PO-2026-900"))
    assert st == "HOLD"
    assert any(f["code"] == "po_exhausted" for f in state_of(c, case_id)["findings"])
    assert sum(p["amount_paise"] for p in payouts()) == 10000000  # still ₹1,00,000


def test_payments_pause_while_books_and_bank_disagree(env):
    c = env.conn
    assert process(c, load("01_razorpay_invoice.txt"))[1] == "PAID"
    with c:  # someone edits the ledger by hand
        c.execute("UPDATE payments SET account = '99999999999999'")
    _po(env, "PO-2026-901", 100000, [50000])
    case_id, st = process(c, _bill("RZP/2026/0911", "PO-2026-901"))
    assert st == "HOLD"
    assert any(f["code"] == "books_mismatch" for f in state_of(c, case_id)["findings"])
    assert len(payouts()) == 1
    with c:  # reconciled: the record is put back
        c.execute("UPDATE payments SET account = '50200012345678'")
    assert pipeline.recheck(c, case_id) == "PAID"
    assert len(payouts()) == 2


def test_recheck_clears_a_hold_once_the_register_is_corrected(env):
    c = env.conn
    case_id, st = process(c, load("04_needs_receipt.txt"))
    assert st == "HOLD"
    add_receipt_to_po(env, "PO-2026-131", {"id": "GRN-FIX-1", "amount": 59000, "currency": "INR", "note": "setup done",
                                           "recorded_by": "ops-lead@kanaku.local", "recorded_at": "2026-11-05"})
    assert pipeline.recheck(c, case_id) == "PAID"
    with pytest.raises(ValueError):
        pipeline.recheck(c, case_id)  # only held cases can be re-checked


def _gemini_reply_from_regex(raw):
    return extract._extract_with_regex(raw).model_dump_json()


def test_gemini_reads_the_email_when_its_key_is_set(env, monkeypatch):
    from kanaku_pulla import llm
    calls = []
    monkeypatch.setattr(config, "llm_providers", lambda: ["gemini"])

    def fake_gemini(system, user, schema):
        calls.append(schema.__name__)
        if schema.__name__ == "InvoiceFields":
            return _gemini_reply_from_regex(user.removeprefix("<email>\n").removesuffix("\n</email>"))
        return json.dumps({"points": [{"text": "Paid: every rule passed.", "evidence": ["E3"]}]})

    monkeypatch.setattr(llm, "_gemini_json", fake_gemini)
    case_id, st = process(env.conn, load("01_razorpay_invoice.txt"))
    s = state_of(env.conn, case_id)
    assert st == "PAID" and s["extract_method"] == "gemini"
    assert s["memo_method"] == "rules + Gemini summary" and s["memo"]["model_summary"]
    assert calls == ["InvoiceFields", "Summary"]


def test_gemini_failure_falls_back_to_the_rule_based_reader(env, monkeypatch):
    from kanaku_pulla import llm
    monkeypatch.setattr(config, "llm_providers", lambda: ["gemini"])
    monkeypatch.setattr(llm, "_gemini_json", lambda *a: (_ for _ in ()).throw(RuntimeError("429 quota")))
    case_id, st = process(env.conn, load("01_razorpay_invoice.txt"))
    s = state_of(env.conn, case_id)
    assert st == "PAID" and s["extract_method"] == "regex" and s["memo_method"] == "rules"


def test_model_amount_must_agree_with_the_second_reading(env, monkeypatch):
    from kanaku_pulla import llm
    monkeypatch.setattr(config, "llm_providers", lambda: ["gemini"])

    def wrong_amount(system, user, schema):
        if schema.__name__ != "InvoiceFields":
            raise RuntimeError("no summary")
        data = json.loads(_gemini_reply_from_regex(user.removeprefix("<email>\n").removesuffix("\n</email>")))
        data["amount"]["value"] = "11800.00"  # a dropped digit
        return json.dumps(data)

    monkeypatch.setattr(llm, "_gemini_json", wrong_amount)
    case_id, st = process(env.conn, load("01_razorpay_invoice.txt"))
    assert st == "HOLD"
    assert any(f["code"] == "amount_disagreement" for f in state_of(env.conn, case_id)["findings"])
    assert payouts() == []


def test_a_failing_gemini_key_falls_through_to_openrouter(env, monkeypatch):
    from kanaku_pulla import llm
    monkeypatch.setattr(config, "llm_providers", lambda: ["gemini", "openrouter"])
    monkeypatch.setattr(llm, "_gemini_json", lambda *a: (_ for _ in ()).throw(RuntimeError("API key not valid")))

    def fake_openrouter(system, user, schema):
        if schema.__name__ != "InvoiceFields":
            raise RuntimeError("no summary")
        return schema.model_validate_json(_gemini_reply_from_regex(user.removeprefix("<email>\n").removesuffix("\n</email>")))

    monkeypatch.setattr(llm, "_openrouter", fake_openrouter)
    case_id, st = process(env.conn, load("01_razorpay_invoice.txt"))
    assert st == "PAID" and state_of(env.conn, case_id)["extract_method"] == "openrouter"


def test_openrouter_reply_is_cleaned_and_repaired(monkeypatch):
    from kanaku_pulla import llm
    from kanaku_pulla.memo import Summary
    good = json.dumps({"points": [{"text": "Paid.", "evidence": ["E1"]}]})
    replies = [
        {"choices": [{"message": {"content": "<think>hmm</think>```json\n{\"points\": \"oops\"}\n```"}}]},
        {"choices": [{"message": {"content": "Here you go:\n```json\n" + good + "\n```"}}]},
    ]
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return _Resp(200, replies.pop(0))

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(config, "openrouter_key", lambda: "test-key")
    parsed = llm._openrouter("system", "user", Summary)
    assert parsed.points[0].text == "Paid."
    assert len(sent) == 2 and "did not match the schema" in sent[1]["messages"][-1]["content"]
    assert sent[0]["model"] == config.OPENROUTER_MODEL
