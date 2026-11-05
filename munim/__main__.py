"""Command line: python -m munim run|resume|show|provide|reject|ledger|issues|reset"""

import argparse
import json
import sys
from pathlib import Path

from . import bank, config, db, pipeline
from .money import fmt

ICON = {"PAID": "✅", "DUPLICATE": "♻️ ", "HOLD": "✋", "REJECTED": "⛔", "ERROR": "⚠️ ", "RUNNING": "…"}


def show(conn, case_id):
    row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    state = json.loads(row["state"])
    print(f"\n{ICON.get(row['status'], '')} {case_id}  {row['status']}  ({row['source_name']})")
    print(f"   {pipeline.describe_status(conn, case_id)}")
    m = state.get("memo") or {}
    if m:
        print(f"   {m.get('headline')}")
        for section, label in (("verified_facts", "Verified"), ("claims", "Email claims"),
                               ("inferences", "Reasoning"), ("could_not_verify", "Could not verify"),
                               ("model_summary", "Model-written summary")):
            for c in m.get(section) or []:
                print(f"   - [{label}] {c['text']}  {' '.join(c['evidence'])}")
        print(f"   Next: {m.get('next_step')}")
    print("   Evidence:")
    for e in db.get_evidence(conn, case_id):
        print(f"     {e['id']:>4} {e['kind']:<9} {e['source']:<28} {e['retrieved_at'][:16]}  {e['summary'][:110]}")


def ledger(conn):
    print("\nOur ledger:")
    for p in conn.execute("SELECT * FROM payments ORDER BY id"):
        print(f"  #{p['id']} {p['status']:<8} {p['vendor_id']} invoice {p['invoice_number']} {fmt(p['amount_paise'])} "
              f"-> a/c …{p['account'][-4:]}  {p['bank_ref'] or '-'}  PO {p['po_number']}  (case {p['case_id']})")
    print("Sandbox bank payouts:")
    for p in bank.all_payouts():
        print(f"  {p['reference']} {fmt(p['amount_paise'])} {p['currency']} -> a/c …{p['account'][-4:]} "
              f"IFSC {p['ifsc']}  key {p['idempotency_key'][:10]}…")
    print("Outbox (team only):")
    for m in conn.execute("SELECT * FROM outbox ORDER BY id"):
        print(f"  to {m['to_addr']}: {m['subject']}")


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="munim")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="process one or more invoice emails")
    r.add_argument("files", nargs="+")
    sub.add_parser("resume", help="finish every case a crash left unfinished")
    s = sub.add_parser("show")
    s.add_argument("case_id")
    rj = sub.add_parser("reject", help="reject a held case (there is no 'approve anyway')")
    rj.add_argument("case_id")
    rj.add_argument("--note", default="")
    sub.add_parser("issues", help="problems found while upgrading an older database")
    pv = sub.add_parser("provide", help="supply the missing evidence a held case asked for")
    pv.add_argument("case_id")
    pv.add_argument("--receipt", required=True, help="what was delivered, e.g. 'Payroll onboarding done 3 Nov'")
    pv.add_argument("--by", required=True, help="team email of the person confirming delivery")
    sub.add_parser("ledger")
    sub.add_parser("reset", help="delete all cases, the ledger and the sandbox bank")
    args = p.parse_args(argv)

    if args.cmd == "reset":
        for path in (config.DB_PATH, config.BANK_DB_PATH):
            for suffix in ("", "-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
        print("Reset.")
        return

    conn = db.connect()
    if args.cmd == "run":
        for f in args.files:
            raw = Path(f).read_text(encoding="utf-8")
            case_id = pipeline.create_case(conn, raw, source_name=Path(f).name)
            print(f"→ {Path(f).name}: case {case_id}")
            pipeline.run(conn, case_id)
            show(conn, case_id)
    elif args.cmd == "resume":
        pending = pipeline.unfinished(conn)
        print(f"Resuming {len(pending)} unfinished case(s)")
        for case_id in pending:
            row = conn.execute("SELECT step FROM cases WHERE id = ?", (case_id,)).fetchone()
            print(f"→ {case_id} from step '{row['step']}'")
            db.log_event(conn, case_id, row["step"], "Resumed after interruption", level="warn")
            pipeline.run(conn, case_id)
            show(conn, case_id)
    elif args.cmd == "show":
        show(conn, args.case_id)
    elif args.cmd == "provide":
        pipeline.provide_receipt(conn, args.case_id, args.by, args.receipt)
        show(conn, args.case_id)
    elif args.cmd == "reject":
        pipeline.human_resolve(conn, args.case_id, "reject", note=args.note)
        show(conn, args.case_id)
    elif args.cmd == "issues":
        rows = conn.execute("SELECT * FROM migration_issues ORDER BY id").fetchall()
        print(f"{len(rows)} upgrade issue(s)")
        for r in rows:
            print(f"  [{r['kind']}] {r['detail']}")
    elif args.cmd == "ledger":
        ledger(conn)


if __name__ == "__main__":
    main()
