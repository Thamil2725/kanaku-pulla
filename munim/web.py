"""Web UI: uvicorn munim.web:app --reload

Server-rendered on purpose: no build step, and every bit of email text is
escaped before it reaches the page (the emails are untrusted input).
"""

import json
from html import escape
from pathlib import Path

from fastapi import FastAPI, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from . import bank, config, db, pipeline, policy
from .money import fmt, to_paise

app = FastAPI(title="Munim")
FIXTURES = config.ROOT / "fixtures"

STATUS = {
    "PAID": ("Paid", "ok"), "DUPLICATE": ("Duplicate, not paid", "dup"), "HOLD": ("On hold", "hold"),
    "REJECTED": ("Rejected", "bad"), "ERROR": ("Error", "bad"), "RUNNING": ("Running", "dup"),
}

CSS = """
:root{--bg:#f7f6f2;--card:#fff;--ink:#1d1d1b;--muted:#6b6a65;--line:#e4e2db;--ok:#1f7a4d;--okbg:#e3f3ea;
--hold:#9a6200;--holdbg:#fdf1d8;--bad:#b3261e;--badbg:#fbe4e2;--dup:#3b5bab;--dupbg:#e6ecfa;--chip:#efeee9}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--card:#1f1f1c;--ink:#ecebe6;--muted:#a3a29b;--line:#34332f;
--okbg:#173a28;--ok:#7fd3a6;--holdbg:#3d2f10;--hold:#f2c46b;--badbg:#45201d;--bad:#f2a49e;--dupbg:#1e2a48;--dup:#a9bdf2;--chip:#2b2a26}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
a{color:inherit}header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;gap:18px;align-items:baseline;flex-wrap:wrap}
header b{font-size:18px}header nav a{margin-right:14px;color:var(--muted);text-decoration:none}
main{max-width:1050px;margin:0 auto;padding:20px 16px 60px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:14px 0}
h1{font-size:22px;margin:4px 0 2px}h2{font-size:16px;margin:0 0 10px}.muted{color:var(--muted)}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:7px 8px;border-top:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500;font-size:13px;border-top:0}
.badge{display:inline-block;padding:2px 9px;border-radius:99px;font-size:13px;font-weight:600;white-space:nowrap}
.ok{background:var(--okbg);color:var(--ok)}.hold{background:var(--holdbg);color:var(--hold)}.bad{background:var(--badbg);color:var(--bad)}.dup{background:var(--dupbg);color:var(--dup)}
.chip{display:inline-block;background:var(--chip);border-radius:6px;padding:0 6px;margin-left:4px;font:12px ui-monospace,monospace;text-decoration:none}
.banner{border-radius:10px;padding:14px 18px;margin:14px 0;font-size:17px}ul{margin:6px 0;padding-left:20px}li{margin:4px 0}
textarea,input,select{width:100%;font:inherit;padding:8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink)}
textarea{min-height:150px;font:13px ui-monospace,monospace}button{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:var(--ink);color:var(--bg);cursor:pointer}
button.ghost{background:transparent;color:var(--ink)}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media (max-width:720px){.grid2{grid-template-columns:1fr}td,th{padding:6px 4px}}pre{white-space:pre-wrap;font:12.5px ui-monospace,monospace;margin:0}
.ev:target{outline:2px solid var(--dup);border-radius:6px}.kind{font-size:12px;color:var(--muted)}details summary{cursor:pointer;color:var(--muted)}
"""


def page(title, body):
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)} · Munim</title>
<style>{CSS}</style></head><body><header><b>Munim</b><span class="muted">accounts payable, done</span>
<nav><a href="/">Cases</a><a href="/ledger">Ledger</a><a href="/policy">Policy</a></nav></header>
<main>{body}</main></body></html>""")


def badge(status):
    label, cls = STATUS.get(status, (status, "dup"))
    return f'<span class="badge {cls}">{escape(label)}</span>'


def chips(ids):
    return "".join(f'<a class="chip" href="#{escape(i)}">{escape(i)}</a>' for i in ids)


@app.get("/")
def home():
    conn = db.connect()
    rows = conn.execute("SELECT * FROM cases ORDER BY created_at DESC").fetchall()
    trs = []
    for r in rows:
        s = json.loads(r["state"])
        f = s.get("fields") or {}
        amt = fmt(to_paise(f.get("amount"))) if to_paise(f.get("amount")) else "—"
        head = pipeline.describe_status(conn, r["id"])
        trs.append(f"<tr><td><a href='/cases/{r['id']}'>{r['id']}</a></td><td>{badge(r['status'])}</td>"
                   f"<td>{escape(f.get('sender_email') or '')}<div class='muted'>{escape(f.get('invoice_number') or '')}"
                   f"</div></td><td>{amt}</td><td>{escape(head)}</td></tr>")
    fixtures = "".join(
        f"<button class='ghost' name='fixture' value='{escape(p.name)}'>{escape(p.stem.replace('_', ' '))}</button>"
        for p in sorted(FIXTURES.glob("*.txt")))
    body = f"""
<div class="card"><h2>New invoice</h2>
<form method="post" action="/cases" enctype="multipart/form-data">
<textarea name="raw" placeholder="Paste the vendor's email and invoice here"></textarea>
<div class="row" style="margin-top:8px"><button>Process</button><span class="muted">or a sample:</span>{fixtures}</div>
</form></div>
<div class="card"><div class="row" style="justify-content:space-between"><h2>Cases</h2>
<form method="post" action="/resume"><button class="ghost">Resume interrupted cases</button></form></div>
<table><tr><th>Case</th><th>Status</th><th>From</th><th>Amount</th><th>Decision</th></tr>{''.join(trs) or
'<tr><td colspan=5 class=muted>No cases yet.</td></tr>'}</table></div>"""
    return page("Cases", body)


@app.post("/cases")
async def new_case(raw: str = Form(""), fixture: str = Form(""), file: UploadFile | None = None):
    name = "pasted email"
    if fixture:
        path = (FIXTURES / Path(fixture).name)
        raw, name = path.read_text(encoding="utf-8"), path.name
    elif file is not None and file.filename:
        raw, name = (await file.read()).decode("utf-8", errors="replace"), file.filename
    if not raw.strip():
        return RedirectResponse("/", status_code=303)
    conn = db.connect()
    case_id = pipeline.create_case(conn, raw, source_name=name)
    pipeline.run(conn, case_id)
    return RedirectResponse(f"/cases/{case_id}", status_code=303)


@app.get("/cases/{case_id}")
def show_case(case_id: str):
    conn = db.connect()
    row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    if not row:
        return page("Not found", "<p>No such case.</p>")
    s = json.loads(row["state"])
    m = s.get("memo") or {}
    f = s.get("fields") or {}
    _, cls = STATUS.get(row["status"], ("", "dup"))

    def section(title, key):
        items = m.get(key) or []
        if not items:
            return ""
        lis = "".join(f"<li>{escape(c['text'])}{chips(c['evidence'])}</li>" for c in items)
        return f"<h2>{title}</h2><ul>{lis}</ul>"

    action = ""
    if row["status"] == "HOLD":
        needs = [x["needs"] for x in s.get("findings", []) if x.get("needs")]
        need_list = "".join(f"<li>{escape(n['evidence'])} <span class='muted'>(ask {escape(n['ask'])})</span></li>"
                            for n in needs)
        receipt_form = ""
        if any(n.get("provide_as") == "receipt" for n in needs):
            opts = "".join(f"<option>{escape(t)}</option>" for t in config.TEAM_EMAILS)
            receipt_form = f"""<form method="post" action="/cases/{case_id}/provide" style="margin-top:10px">
<div class="grid2"><select name="by">{opts}</select><input name="note" placeholder="What was delivered, and when" required></div>
<div class="row" style="margin-top:8px"><button>Record receipt and continue</button></div></form>"""
        action = f"""<div class="card"><h2>To pay this, Munim needs</h2><ul>{need_list}</ul>{receipt_form}
<form method="post" action="/cases/{case_id}/resolve" class="row" style="margin-top:10px">
<button class="ghost" name="action" value="reject">Reject (no payment)</button>
<span class="muted">There is no "approve anyway": a hold clears only when its missing evidence is supplied.</span>
</form></div>"""

    def src_link(e):
        return f" <a href='{escape(e['url'])}' target='_blank' rel='noopener'>source</a>" if e.get("url") else ""

    kinds = {"fact": "source", "claim": "email claim", "inference": "reasoning", "unavailable": "could not check"}
    ev_rows = "".join(
        f"<tr class='ev' id='{e['id']}'><td><b>{e['id']}</b><div class='kind'>{escape(kinds.get(e['kind'], e['kind']))}"
        f"</div></td>"
        f"<td>{escape(e['summary'])}{src_link(e)}</td>"
        f"<td class='muted'>{escape(e['source'])}<br>{escape((e['retrieved_at'] or '')[:16].replace('T', ' '))} UTC</td></tr>"
        for e in db.get_evidence(conn, case_id))
    events = "".join(
        f"<tr><td class='muted'>{escape(ev['ts'][11:19])}</td><td>{escape(ev['step'] or '')}</td>"
        f"<td>{escape(ev['message'] or '')}</td></tr>"
        for ev in conn.execute("SELECT * FROM events WHERE case_id = ? ORDER BY id", (case_id,)))
    sent = "".join(
        f"<tr><td>{escape(o['to_addr'])}</td><td>{escape(o['subject'])}<details><summary>message</summary>"
        f"<pre>{escape(o['body'])}</pre></details></td></tr>"
        for o in conn.execute("SELECT * FROM outbox WHERE case_id = ? ORDER BY id", (case_id,)))
    amt = (f"{fmt(to_paise(f.get('amount')))} {escape(f.get('currency') or '')} (as claimed)"
           if to_paise(f.get("amount")) else "amount unclear")
    body = f"""
<p class="muted"><a href="/">← Cases</a> · {escape(row['source_name'] or '')}</p>
<h1>{escape(f.get('vendor_name') or 'Unknown vendor')} · {escape(f.get('invoice_number') or '')} · {amt}</h1>
<div class="banner {cls}">{badge(row['status'])} &nbsp;{escape(pipeline.describe_status(conn, case_id))}</div>
{action}
<div class="card"><p><b>{escape(m.get('headline', ''))}</b></p>
{section('Verified facts (from a source, with retrieval time)', 'verified_facts')}
{section('What the email claims (not verified)', 'claims')}
{section('Reasoning (policy findings)', 'inferences')}
{section('Could not verify', 'could_not_verify')}
{section('Model-written summary (cites evidence; not machine-proven)', 'model_summary')}
<p><b>Next:</b> {escape(m.get('next_step', ''))}</p>
<p class="muted">Email read by {escape(s.get('extract_method', '?'))}; memo by {escape(s.get('memo_method', '?'))}.
The decision comes from fixed rules (<a href="/policy">policy</a>), not the model. The status line above is read from
the ledger, not from the memo.</p></div>
<div class="card"><h2>Evidence</h2><table><tr><th>Id</th><th>What it says</th><th>Source, retrieved</th></tr>{ev_rows}</table></div>
<div class="card"><h2>Timeline</h2><table>{events}</table></div>
<div class="card"><h2>Messages to the team</h2><table>{sent or '<tr><td class=muted>None</td></tr>'}</table></div>
<div class="card"><details><summary>Original email</summary><pre>{escape(row['raw'])}</pre></details></div>"""
    return page(case_id, body)


@app.post("/cases/{case_id}/provide")
def provide(case_id: str, by: str = Form(...), note: str = Form(...)):
    conn = db.connect()
    try:
        pipeline.provide_receipt(conn, case_id, by, note)
    except ValueError as exc:
        return page("Not accepted", f"<div class='banner bad'>{escape(str(exc))}</div><a href='/cases/{case_id}'>Back</a>")
    return RedirectResponse(f"/cases/{case_id}", status_code=303)


@app.post("/cases/{case_id}/resolve")
def resolve(case_id: str, action: str = Form(...)):
    conn = db.connect()
    try:
        pipeline.human_resolve(conn, case_id, action, who="ap-lead")  # anything but "reject" is refused
    except ValueError as exc:
        return page("Not accepted", f"<div class='banner bad'>{escape(str(exc))}</div><a href='/cases/{case_id}'>Back</a>")
    return RedirectResponse(f"/cases/{case_id}", status_code=303)


@app.post("/resume")
def resume():
    conn = db.connect()
    for case_id in pipeline.unfinished(conn):
        db.log_event(conn, case_id, None, "Resumed after interruption", level="warn")
        pipeline.run(conn, case_id)
    return RedirectResponse("/", status_code=303)


@app.get("/ledger")
def ledger():
    conn = db.connect()
    ours = "".join(
        f"<tr><td>{escape(p['vendor_id'])} · {escape(p['invoice_number'])}<div class='muted'>{escape(p['po_number'] or '')}"
        f" · {escape(p['receipt_id'] or '')}</div></td><td>{fmt(p['amount_paise'])} {escape(p['currency'])}</td>"
        f"<td>…{escape(p['account'][-4:])}<div class='muted'>{escape(p['ifsc'] or '')}</div></td>"
        f"<td>{badge('PAID') if p['status'] == 'paid' else '<span class=\"badge hold\">Reserved</span>'}"
        f"</td><td><a href='/cases/{p['case_id']}'>{p['case_id']}</a></td></tr>"
        for p in conn.execute("SELECT * FROM payments ORDER BY id"))
    theirs = "".join(
        f"<tr><td>{escape(p['reference'])}</td><td>{fmt(p['amount_paise'])} {escape(p['currency'])}</td>"
        f"<td>…{escape(p['account'][-4:])}<div class='muted'>{escape(p['ifsc'] or '')}</div></td>"
        f"<td class='muted'>{escape(p['idempotency_key'][:12])}…</td></tr>" for p in bank.all_payouts())
    body = f"""<h1>Ledger</h1><p class="muted">Our books on the left, the bank's on the right. They should always agree:
one payout per invoice, whatever crashed or was resent.</p><div class="grid2">
<div class="card"><h2>Our ledger</h2><table><tr><th>Invoice</th><th>Amount</th><th>A/c</th><th>Status</th><th>Case</th></tr>{ours}</table></div>
<div class="card"><h2>Sandbox bank payouts</h2><table><tr><th>Reference</th><th>Amount</th><th>A/c</th><th>Key</th></tr>{theirs}</table></div></div>"""
    return page("Ledger", body)


@app.get("/policy")
def show_policy():
    items = "".join(f"<li>{escape(x)}</li>" for x in policy.REQUIRED_TO_PAY)
    body = f"""<h1>When Munim pays</h1><div class="card"><p>All of these must hold. If one doesn't, Munim holds
the invoice, names the smallest piece of missing evidence and asks the right person on the team. Lookalike domains and
changed resends of a paid invoice are rejected outright.</p><ol>{items}</ol>
<p class="muted">Auto-pay limit ₹{config.AUTO_PAY_LIMIT_INR:,.0f}. Public data older than {config.CACHE_MAX_AGE_HOURS:g} hours is fetched again.</p></div>"""
    return page("Policy", body)
