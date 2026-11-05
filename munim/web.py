"""Web UI: uvicorn munim.web:app --reload

Server-rendered on purpose: no build step, works with JavaScript off, and every
piece of email text is escaped before it reaches the page (emails are untrusted).
Every status shown here is read from the ledger and the saved case state.
"""

import json
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from . import bank, config, db, pipeline, policy
from .money import SUPPORTED_CURRENCIES, fmt, to_paise

app = FastAPI(title="Munim")
FIXTURES = config.ROOT / "fixtures"
IST = timezone(timedelta(hours=5, minutes=30))

SAMPLES = {
    "01_razorpay_invoice.txt": ("Genuine monthly invoice",
                                "Razorpay gateway fees, citing an approved PO with a receipt on file.", "Paid"),
    "02_bank_change_request.txt": ("Urgent bank-change request",
                                   "Look-alike domain, a former employee's name, a hidden “skip verification” note.",
                                   "Rejected"),
    "03_resent_under_alias.txt": ("Same invoice, another name",
                                  "Sample 1 resent as RZP-2026-42 from “Razorpay Software Pvt. Ltd.”.",
                                  "Duplicate (run sample 1 first)"),
    "04_needs_receipt.txt": ("Nobody confirmed delivery",
                             "Payroll setup with a valid PO, but no receipt from the team yet.",
                             "On hold, then paid once the team confirms"),
    "05_modified_resend.txt": ("Revised amount", "Sample 1's invoice number again, now asking ₹1,81,000.",
                               "Rejected (run sample 1 first)"),
    "06_renamed_reminder.txt": ("Renamed reminder", "RZP/2026/0042-REMINDER for the same amount.",
                                "On hold as a likely resend (run sample 1 first)"),
}

STATUS = {  # label, css class, glyph (decorative; the label carries the meaning)
    "PAID": ("Paid", "ok", "✓"), "DUPLICATE": ("Duplicate", "dup", "="), "HOLD": ("On hold", "hold", "!"),
    "REJECTED": ("Rejected", "bad", "✕"), "ERROR": ("Stopped", "bad", "!"), "RUNNING": ("In progress", "na", "…"),
}

CHECKS = [
    ("read", "The email and invoice were read clearly"),
    ("vendor", "The vendor is in our register"),
    ("domain", "The claimed sender address is on the vendor's registered domain"),
    ("sender", "Nothing about the named sender contradicts the email"),
    ("currency", "Rupees, matching the vendor, PO and receipt"),
    ("bank", "Account number and IFSC match our register"),
    ("po", "An approved purchase order has room for it"),
    ("receipt", "A team receipt confirms delivery"),
    ("once", "Not paid or reserved before"),
    ("limit", "Within the auto-pay limit"),
]
CODE_TO_CHECK = {
    "new_vendor": "vendor", "ambiguous_vendor": "vendor",
    "lookalike_domain": "domain", "domain_mismatch": "domain",
    "sender_left": "sender", "sender_unverified": "sender", "sender_not_finance": "sender",
    "unknown_po": "po", "po_wrong_vendor": "po", "no_po": "po", "po_exhausted": "po", "po_changed": "po",
    "po_capacity": "po", "no_receipt": "receipt", "receipt_taken": "receipt", "receipt_changed": "receipt",
    "duplicate": "once", "changed_resend": "once", "duplicate_suspect": "once", "bank_refused": "once",
    "over_limit": "limit", "bank_changed": "bank",
}
SEVERITY = {"block": ("Blocks payment", "bad"), "duplicate": ("Already in ledger", "dup"),
            "hold": ("Needs evidence", "hold"), "warn": ("Note", "note")}
STEP_LABELS = {
    "extract": "Read the email", "resolve": "Identify the vendor", "verify_sender": "Check the named sender",
    "profile": "Profile a new vendor", "decide": "Apply the payment rules", "explain": "Write the explanation",
    "act": "Reserve and pay", "notify": "Tell the team",
}
KIND = {"fact": ("Source", "ok"), "claim": ("Email claim", "hold"), "inference": ("Reasoning", "na"),
        "unavailable": ("Could not check", "bad")}

FONTS = ("https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700"
         "&family=IBM+Plex+Mono:wght@400;500&family=Public+Sans:ital,wght@0,400;0,500;0,600;1,400&display=swap")

CSS = """
/* Layout: one 1120px column; summary before detail; ledger tables ruled like a khata. */
:root{
  --bg:#f5f7fa; --surface:#ffffff; --surface-2:#eef2f7; --ink:#162033; --muted:#4b5670; --line:#d5dce7;
  --accent:#1d48b0; --accent-ink:#ffffff; --accent-soft:#e3eafa; --focus:#1d48b0;
  --ok:#11663e; --ok-bg:#e0f2e8; --hold:#7f4c00; --hold-bg:#fcefd2; --bad:#a8241b; --bad-bg:#fbe3e0;
  --dup:#36435e; --dup-bg:#e6ebf3; --note:#5b4a00; --note-bg:#f6f0cf;
  --font-display:"Bricolage Grotesque", "Segoe UI", system-ui, sans-serif;
  --font-body:"Public Sans", "Segoe UI", system-ui, -apple-system, sans-serif;
  --font-mono:"IBM Plex Mono", ui-monospace, "Cascadia Mono", Consolas, monospace;
  --radius:10px; color-scheme:light;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#0e131c; --surface:#151c29; --surface-2:#1b2434; --ink:#e7ebf3; --muted:#a6b0c5; --line:#2c3850;
  --accent:#93b1ff; --accent-ink:#0e131c; --accent-soft:#1c2a4a; --focus:#93b1ff;
  --ok:#86dcae; --ok-bg:#11301f; --hold:#f4c56f; --hold-bg:#33260b; --bad:#ff9d93; --bad-bg:#3b1714;
  --dup:#c6d0e3; --dup-bg:#212b3e; --note:#ecd977; --note-bg:#2f2a0c; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#0e131c; --surface:#151c29; --surface-2:#1b2434; --ink:#e7ebf3; --muted:#a6b0c5; --line:#2c3850;
  --accent:#93b1ff; --accent-ink:#0e131c; --accent-soft:#1c2a4a; --focus:#93b1ff;
  --ok:#86dcae; --ok-bg:#11301f; --hold:#f4c56f; --hold-bg:#33260b; --bad:#ff9d93; --bad-bg:#3b1714;
  --dup:#c6d0e3; --dup-bg:#212b3e; --note:#ecd977; --note-bg:#2f2a0c; color-scheme:dark}
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:400 1rem/1.55 var(--font-body)}
a{color:var(--accent);text-underline-offset:.18em}
h1,h2,h3{font-family:var(--font-display);line-height:1.2;text-wrap:balance;margin:0}
h1{font-size:clamp(1.6rem,1.2rem + 1.5vw,2.1rem);font-weight:700;letter-spacing:-.01em}
h2{font-size:1.2rem;font-weight:600}
h3{font-size:1rem;font-weight:600;font-family:var(--font-body)}
p{margin:0}
.wrap{max-width:1120px;margin-inline:auto;padding-left:max(16px,env(safe-area-inset-left,0px));
  padding-right:max(16px,env(safe-area-inset-right,0px))}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
.skip{position:absolute;left:12px;top:-60px;background:var(--accent);color:var(--accent-ink);padding:10px 14px;
  border-radius:8px;z-index:20;font-weight:600}
.skip:focus{top:12px}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px;border-radius:4px}
header.site{background:var(--surface);border-bottom:1px solid var(--line)}
.bar{display:flex;align-items:center;gap:20px;flex-wrap:wrap;padding-block:12px}
.brand{display:flex;align-items:baseline;gap:8px;text-decoration:none;color:var(--ink)}
.brand b{font:700 1.35rem var(--font-display);letter-spacing:-.01em}
.brand span{color:var(--muted);font-size:.95rem}
nav ul{display:flex;gap:4px;list-style:none;margin:0;padding:0;flex-wrap:wrap}
nav a{display:block;padding:8px 12px;border-radius:8px;color:var(--ink);text-decoration:none;font-weight:500}
nav a:hover{background:var(--surface-2)}
nav a[aria-current="page"]{background:var(--accent-soft);color:var(--accent)}
.bar .spacer{flex:1}
main{display:grid;gap:28px;padding-block:28px 48px}
main:focus{outline:none}
.lede{color:var(--muted);max-width:65ch;margin-top:6px}
.eyebrow{font:500 .75rem var(--font-mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.mono{font-family:var(--font-mono);font-variant-numeric:tabular-nums}
.muted{color:var(--muted)}
.small{font-size:.875rem}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:20px}
.stack{display:grid;gap:12px}
.section{display:grid;gap:14px}
.section-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap}
.grid-2{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,7fr);gap:24px;align-items:start}
.grid-2.even{grid-template-columns:repeat(2,minmax(0,1fr))}
@media (max-width:860px){.grid-2,.grid-2.even{grid-template-columns:minmax(0,1fr)}}
.nowrap{white-space:nowrap}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:44px;padding:10px 18px;
  border-radius:8px;border:1px solid var(--accent);background:var(--accent);color:var(--accent-ink);
  font:600 .95rem var(--font-body);cursor:pointer;text-decoration:none}
.btn:hover{filter:brightness(1.08)}
.btn-quiet{background:transparent;color:var(--ink);border-color:var(--line)}
.btn-quiet:hover{background:var(--surface-2);filter:none}
.btn-danger{background:transparent;color:var(--bad);border-color:var(--bad)}
.btn[disabled]{opacity:.6;cursor:progress}
label{font-weight:600;font-size:.95rem}
.hint{color:var(--muted);font-size:.875rem;font-weight:400}
textarea,input[type=text],select,input[type=file]{width:100%;font:inherit;color:var(--ink);background:var(--bg);
  border:1px solid var(--line);border-radius:8px;padding:10px 12px}
textarea{min-height:170px;font:.875rem/1.5 var(--font-mono);resize:vertical}
.field{display:grid;gap:6px}
.chip{display:inline-flex;align-items:center;gap:6px;padding:2px 10px;border-radius:999px;font:600 .8rem var(--font-body);
  white-space:nowrap;border:1px solid transparent}
.chip .g{font-family:var(--font-mono)}
.ok{background:var(--ok-bg);color:var(--ok)}.hold{background:var(--hold-bg);color:var(--hold)}
.bad{background:var(--bad-bg);color:var(--bad)}.dup{background:var(--dup-bg);color:var(--dup)}
.note{background:var(--note-bg);color:var(--note)}.na{background:var(--surface-2);color:var(--muted)}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
@media (max-width:720px){.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px;display:grid;gap:2px}
.stat .n{font:600 1.5rem var(--font-mono);font-variant-numeric:tabular-nums}
.stat .l{color:var(--muted);font-size:.875rem}
.recon{display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:.95rem}
.samples{list-style:none;margin:0;padding:0;display:grid;gap:8px}
.sample{width:100%;text-align:left;display:grid;gap:2px;padding:12px 14px;border-radius:8px;border:1px solid var(--line);
  background:var(--surface);color:var(--ink);font:inherit;cursor:pointer;min-height:44px}
.sample:hover{border-color:var(--accent);background:var(--accent-soft)}
.sample b{font-weight:600}.sample .d{color:var(--muted);font-size:.875rem}
.sample .x{font:500 .78rem var(--font-mono);color:var(--accent)}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface)}
table{width:100%;border-collapse:collapse;font-size:.95rem}
caption{text-align:left;padding:12px 14px;font-weight:600}
th,td{text-align:left;padding:10px 14px;vertical-align:top;border-top:1px solid var(--line)}
thead th{border-top:0;font:500 .75rem var(--font-mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted);
  background:var(--surface-2)}
td.num,th.num{text-align:right;font-family:var(--font-mono);font-variant-numeric:tabular-nums;white-space:nowrap}
tbody tr:hover{background:var(--surface-2)}
.banner{display:flex;gap:14px;align-items:flex-start;border-radius:var(--radius);padding:16px 18px;border:1px solid currentColor}
.banner .g{font:700 1.2rem var(--font-mono);line-height:1.3}
.banner p{color:var(--ink)}
.checks{list-style:none;margin:0;padding:0;display:grid}
.checks li{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 16px;padding:12px 0;border-top:1px solid var(--line);
  align-items:start}
.checks li>.chip{align-self:start;margin-top:1px}
.checks li:first-child{border-top:0}
.checks .why{grid-column:1/-1;color:var(--muted);font-size:.9rem;display:grid;gap:4px}
.evref{display:inline-block;font:500 .75rem var(--font-mono);padding:0 6px;margin-left:4px;border-radius:5px;
  background:var(--surface-2);color:var(--ink);text-decoration:none;border:1px solid var(--line)}
.evref:hover{border-color:var(--accent)}
.steps{list-style:none;margin:0;padding:0;display:grid;gap:0;counter-reset:s}
.steps li{display:grid;grid-template-columns:28px minmax(0,1fr);gap:12px;padding-bottom:14px;position:relative}
.steps li::before{counter-increment:s;content:counter(s);width:28px;height:28px;border-radius:50%;display:grid;
  place-items:center;font:600 .8rem var(--font-mono);background:var(--surface-2);color:var(--muted);border:1px solid var(--line)}
.steps li.done::before{content:"✓";background:var(--ok-bg);color:var(--ok);border-color:transparent}
.steps li.current::before{background:var(--accent);color:var(--accent-ink);border-color:transparent}
.steps li.error::before{content:"!";background:var(--bad-bg);color:var(--bad);border-color:transparent}
.steps li:not(:last-child)::after{content:"";position:absolute;left:13px;top:30px;bottom:2px;width:2px;background:var(--line)}
.steps .t{font-weight:600}.steps .m{color:var(--muted);font-size:.875rem}
.evidence{list-style:none;margin:0;padding:0;display:grid}
.evidence li{display:grid;grid-template-columns:56px minmax(0,1fr);gap:4px 14px;padding:12px 14px;border-top:1px solid var(--line)}
.evidence li:first-child{border-top:0}
.evidence li:target{background:var(--accent-soft)}
.evidence .id{font:600 .85rem var(--font-mono);padding-top:2px}
.evidence .meta{color:var(--muted);font-size:.8rem;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.memo{display:grid;gap:18px}
.memo ul{margin:6px 0 0;padding-left:20px;display:grid;gap:6px}
details>summary{cursor:pointer;font-weight:600;min-height:32px}
pre{white-space:pre-wrap;word-break:break-word;font:.85rem/1.5 var(--font-mono);margin:10px 0 0;padding:14px;
  background:var(--surface-2);border-radius:8px;max-width:100%;overflow-x:auto}
.notice{padding:12px 16px;border-radius:8px;border:1px solid currentColor}
.danger{border-color:var(--line)}
.inline-form{display:flex;gap:10px 14px;flex-wrap:wrap;align-items:start}
.inline-form select,.inline-form input{min-height:44px}
.inline-form .field{flex:1 1 240px}
footer.foot{color:var(--muted);font-size:.85rem;padding-block:0 32px;max-width:1120px}
@media (prefers-reduced-motion: reduce){*{transition:none!important;animation:none!important;scroll-behavior:auto!important}}
"""

JS = """
(function(){
  var root=document.documentElement, btn=document.getElementById('theme-toggle');
  function isDark(){var t=root.getAttribute('data-theme');
    return t==='dark'||(!t&&window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches);}
  function sync(){if(!btn)return;var d=isDark();btn.setAttribute('aria-pressed',d?'true':'false');
    btn.textContent=d?'Use light theme':'Use dark theme';}
  if(btn){sync();btn.addEventListener('click',function(){var next=isDark()?'light':'dark';
    root.setAttribute('data-theme',next);try{localStorage.setItem('munim-theme',next)}catch(e){}sync();});}
  document.querySelectorAll('form[data-busy]').forEach(function(f){
    f.addEventListener('submit',function(ev){
      var b=ev.submitter;
      if(b&&b.name){var h=document.createElement('input');h.type='hidden';h.name=b.name;h.value=b.value;f.appendChild(h);}
      f.setAttribute('aria-busy','true');
      setTimeout(function(){f.querySelectorAll('button').forEach(function(x){x.disabled=true});},0);
      if(b){b.textContent=f.getAttribute('data-busy');}
      var live=document.getElementById('live');if(live){live.textContent=f.getAttribute('data-busy');}
    });
  });
})();
"""


# --- helpers ----------------------------------------------------------------------------

def page(title, body, active="", note="", error=""):
    nav = "".join(
        f'<li><a href="{href}"{" aria-current=\"page\"" if key == active else ""}>{label}</a></li>'
        for key, href, label in (("cases", "/", "Invoices"), ("ledger", "/ledger", "Ledger"),
                                 ("policy", "/policy", "When Munim pays")))
    flash = ""
    if error:
        flash += f'<div class="notice bad" role="alert"><b>Not accepted.</b> {escape(error)}</div>'
    if note:
        flash += f'<div class="notice ok" role="status">{escape(note)}</div>'
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · Munim</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%231d48b0'/%3E%3Cpath d='M9 10h14M9 16h14M9 22h9' stroke='white' stroke-width='2.5' stroke-linecap='round'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS}">
<script>try{{var t=localStorage.getItem('munim-theme');if(t)document.documentElement.setAttribute('data-theme',t)}}catch(e){{}}</script>
<style>{CSS}</style></head>
<body><a class="skip" href="#main">Skip to content</a>
<header class="site"><div class="wrap bar">
<a class="brand" href="/"><b>Munim</b><span lang="hi">मुनीम</span><span>· accounts payable desk</span></a>
<nav aria-label="Main"><ul>{nav}</ul></nav><div class="spacer"></div>
<button type="button" class="btn btn-quiet" id="theme-toggle" aria-pressed="false">Use dark theme</button>
</div></header>
<main id="main" class="wrap" tabindex="-1">{flash}{body}</main>
<footer class="wrap foot"><p>Sandbox: payouts go to a pretend bank and messages stay in the team outbox.
Fixed rules decide every payment; Claude only reads emails and adds a labelled summary.</p></footer>
<div id="live" class="sr-only" aria-live="polite"></div>
<script>{JS}</script></body></html>""")


def chip(label, cls, glyph=""):
    g = f'<span class="g" aria-hidden="true">{glyph}</span>' if glyph else ""
    return f'<span class="chip {cls}">{g}{escape(label)}</span>'


def status_chip(status):
    label, cls, glyph = STATUS.get(status, (status.title(), "na", ""))
    return chip(label, cls, glyph)


def evrefs(ids):
    return "".join(f'<a class="evref" href="#{escape(i)}" aria-label="Evidence {escape(i)}">{escape(i)}</a>' for i in ids)


def when(iso):
    """'2026-11-05T18:57:21+00:00' -> '6 Nov, 00:27 IST'"""
    try:
        t = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return escape(iso or "")
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    t = t.astimezone(IST)
    return f"{t.day} {t.strftime('%b')}, {t.strftime('%H:%M')} IST"


def claimed_amount(fields):
    p = to_paise((fields or {}).get("amount"))
    return f"{fmt(p)}" if p else "amount unclear"


def reconcile(conn):
    """Compare our ledger with the bank's payouts. Returns (problems, pending, ledger_count, bank_count)."""
    ours = {p["payment_key"]: p for p in conn.execute("SELECT * FROM payments")}
    theirs = {b["idempotency_key"]: b for b in bank.all_payouts()}
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


def recon_line(conn):
    problems, pending, n_ours, n_bank = reconcile(conn)
    if problems:
        return (f'<div class="recon">{chip("Books and bank disagree", "bad", "✕")}'
                f'<span>{escape(problems[0])}</span></div>')
    extra = f" · {len(pending)} awaiting bank confirmation" if pending else ""
    return (f'<div class="recon">{chip("Books and bank agree", "ok", "✓")}'
            f'<span class="muted">{n_ours} ledger entr{"y" if n_ours == 1 else "ies"}, {n_bank} bank '
            f'payout{"" if n_bank == 1 else "s"}{extra}</span></div>')


# --- the payment checklist (derived from saved findings; never claims a check that did not run) ----------

def check_key(code):
    if code in CODE_TO_CHECK:
        return CODE_TO_CHECK[code]
    if code.endswith("bank_account_number") or code.endswith("bank_ifsc"):
        return "bank"
    if code.endswith("currency"):
        return "currency"
    return "read"


def checklist(state):
    findings = state.get("findings")
    if findings is None:
        return [(key, label, ("Pending", "na", "…"), []) for key, label in CHECKS]
    by_key = {}
    for f in findings:
        by_key.setdefault(check_key(f["code"]), []).append(f)
    fields, res, match = state.get("fields") or {}, state.get("resolution") or {}, state.get("match") or {}
    ran = {
        "read": True,
        "vendor": bool(res.get("vendor")),
        "domain": res.get("domain_status") == "registered",
        "sender": "sender" in state,
        "currency": (fields.get("currency") or "") in SUPPORTED_CURRENCIES,
        "bank": bool(res.get("claimed")) and bool(fields.get("bank_account_number")),
        "po": bool(match.get("po_number")),
        "receipt": bool(match.get("receipt_id")),
        "once": bool(match.get("payment_key")),
        "limit": to_paise(fields.get("amount")) is not None,
    }
    rows = []
    for key, label in CHECKS:
        fs = by_key.get(key, [])
        worst = next((s for s in ("block", "duplicate", "hold", "warn") if any(f["severity"] == s for f in fs)), None)
        if worst:
            text, cls = SEVERITY[worst]
            result = (text, cls, {"block": "✕", "duplicate": "=", "hold": "!", "warn": "i"}[worst])
        elif ran[key]:
            result = ("OK", "ok", "✓")
        else:
            result = ("Not checked", "na", "–")
        rows.append((key, label, result, fs))
    return rows


def steps_html(conn, case_id, row):
    events = conn.execute("SELECT * FROM events WHERE case_id = ? ORDER BY id", (case_id,)).fetchall()
    last = {}
    for e in events:
        if e["step"]:
            last[e["step"]] = e
    current = row["step"]
    idx = pipeline.STEPS.index(current) if current in pipeline.STEPS else len(pipeline.STEPS)
    items = []
    for i, step in enumerate(pipeline.STEPS[:-1]):
        if i < idx or current == "done":
            cls, state_txt = "done", "done"
        elif i == idx:
            cls, state_txt = ("error", "stopped here") if row["status"] == "ERROR" else ("current", "next")
        else:
            cls, state_txt = "", "not reached"
        e = last.get(step)
        if e:
            msg = f'{escape(e["message"])} · {when(e["ts"])}'
        elif step == "profile" and cls == "done":
            msg = "Skipped: the vendor is already onboarded"
        else:
            msg = state_txt.capitalize()
        items.append(f'<li class="{cls}"><div><div class="t">{escape(STEP_LABELS[step])}'
                     f'<span class="sr-only"> ({state_txt})</span></div><div class="m">{msg}</div></div></li>')
    return f'<ol class="steps">{"".join(items)}</ol>'


# --- pages ------------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(note: str = "", error: str = ""):
    with closing(db.connect()) as conn:
        rows = conn.execute("SELECT * FROM cases ORDER BY created_at DESC").fetchall()
        counts = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        paid_total = conn.execute("SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE status='paid'").fetchone()[0]
        pending = pipeline.unfinished(conn)
        trs = []
        for r in rows:
            s = json.loads(r["state"])
            f = s.get("fields") or {}
            vendor = f.get("vendor_name") or "Unknown vendor"
            trs.append(
                f'<tr><td class="nowrap"><a href="/cases/{r["id"]}" class="mono">{r["id"]}</a>'
                f'<div class="muted small">{when(r["created_at"])}</div></td>'
                f'<td>{status_chip(r["status"])}</td>'
                f'<td>{escape(vendor)}<div class="muted small mono">{escape(f.get("invoice_number") or "")}</div></td>'
                f'<td class="num">{claimed_amount(f)}</td>'
                f'<td>{escape(pipeline.describe_status(conn, r["id"]))}</td></tr>')
        recon = recon_line(conn)
    samples = "".join(
        f'<li><button class="sample" type="submit" name="fixture" value="{escape(name)}">'
        f'<b>{escape(title)}</b><span class="d">{escape(desc)}</span><span class="x">Expect: {escape(expect)}</span>'
        f'</button></li>'
        for name, (title, desc, expect) in SAMPLES.items() if (FIXTURES / name).exists())
    resume = ""
    if pending:
        resume = (f'<form method="post" action="/resume" data-busy="Resuming…" class="card recon">'
                  f'{chip(f"{len(pending)} interrupted", "hold", "!")}'
                  f'<span>A run stopped part-way. Resuming continues from the saved step and never pays twice.</span>'
                  f'<button class="btn" type="submit">Resume interrupted cases</button></form>')
    table = (f'<div class="table-wrap"><table><caption class="sr-only">Invoices processed</caption>'
             f'<thead><tr><th scope="col">Case</th><th scope="col">Status</th><th scope="col">Vendor and invoice</th>'
             f'<th scope="col" class="num">Amount claimed</th><th scope="col">Where the money stands</th></tr></thead>'
             f'<tbody>{"".join(trs)}</tbody></table></div>') if trs else (
        '<div class="card"><p><b>No invoices yet.</b> Pick a sample on the left, or paste an email. '
        'Start with “Genuine monthly invoice”; several samples build on it.</p></div>')
    body = f"""
<section class="section" aria-labelledby="h-desk">
  <div><p class="eyebrow">Payables desk</p><h1 id="h-desk">Invoices</h1>
  <p class="lede">Paste a vendor's email, or pick a sample. Munim checks it against your vendor register, purchase
  orders and receipts, and against Crustdata, then pays it once, holds it for one specific thing, or rejects it.</p></div>
  <div class="stats" role="list">
    <div class="stat" role="listitem"><span class="n">{fmt(paid_total) if paid_total else "₹0"}</span><span class="l">Paid ({counts.get("PAID", 0)})</span></div>
    <div class="stat" role="listitem"><span class="n">{counts.get("HOLD", 0)}</span><span class="l">On hold</span></div>
    <div class="stat" role="listitem"><span class="n">{counts.get("REJECTED", 0)}</span><span class="l">Rejected</span></div>
    <div class="stat" role="listitem"><span class="n">{counts.get("DUPLICATE", 0)}</span><span class="l">Duplicates stopped</span></div>
  </div>
  {recon}
</section>
{resume}
<div class="grid-2 even">
  <section class="card stack" aria-labelledby="h-new">
    <h2 id="h-new">Check a new invoice</h2>
    <form method="post" action="/cases" enctype="multipart/form-data" class="stack"
      data-busy="Checking: reading the email, looking up Crustdata, applying the rules…">
      <div class="field"><label for="raw">Vendor email and invoice text</label>
        <textarea id="raw" name="raw" placeholder="From: Name &lt;billing@vendor.com&gt;&#10;Invoice No: …&#10;Amount: INR …&#10;A/c No: … IFSC: …"></textarea></div>
      <div class="field"><label for="file">Or upload a .txt or .eml file <span class="hint">(optional)</span></label>
        <input type="file" id="file" name="file" accept=".txt,.eml,text/plain,message/rfc822"></div>
      <div><button class="btn" type="submit">Check and process</button></div>
    </form>
  </section>
  <section class="card stack" aria-labelledby="h-samples">
    <div><h2 id="h-samples">Samples</h2><p class="hint">Real Crustdata lookups on sandbox invoices. Start with the
    first; several build on it.</p></div>
    <form method="post" action="/cases" data-busy="Checking: reading the email, looking up Crustdata, applying the rules…">
      <ul class="samples" aria-labelledby="h-samples">{samples}</ul></form>
  </section>
</div>
<section class="section" aria-labelledby="h-cases">
    <div class="section-head"><h2 id="h-cases">Processed</h2><a href="/ledger">Open the ledger</a></div>
    {table}
    <details class="card danger"><summary>Start a fresh demo</summary>
      <form method="post" action="/demo/reset" class="stack" style="margin-top:12px">
        <p class="small">Deletes every case, the ledger, receipts recorded here, team messages and the sandbox
        bank's payouts. Recorded Crustdata answers are kept, so a rerun costs no credits.</p>
        <div><input type="checkbox" id="confirm" name="confirm" value="yes" required>
        <label for="confirm">I want to delete this demo's cases and payouts</label></div>
        <div><button class="btn btn-danger" type="submit">Delete and start fresh</button></div>
      </form></details>
</section>"""
    return page("Invoices", body, active="cases", note=note, error=error)


@app.post("/cases")
async def new_case(raw: str = Form(""), fixture: str = Form(""), file: UploadFile | None = None):
    name = "pasted email"
    if fixture:
        path = FIXTURES / Path(fixture).name
        if not path.exists():
            return RedirectResponse("/?error=" + quote("That sample no longer exists."), status_code=303)
        raw, name = path.read_text(encoding="utf-8"), path.name
    elif file is not None and file.filename:
        raw, name = (await file.read()).decode("utf-8", errors="replace"), Path(file.filename).name
    if not raw.strip():
        return RedirectResponse("/?error=" + quote("Paste an email or choose a file first."), status_code=303)
    with closing(db.connect()) as conn:
        case_id = pipeline.create_case(conn, raw, source_name=name)
        pipeline.run(conn, case_id)
    return RedirectResponse(f"/cases/{case_id}", status_code=303)


@app.get("/cases/{case_id}", response_class=HTMLResponse)
def show_case(case_id: str, note: str = "", error: str = ""):
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not row:
            return page("Not found", '<h1>No such case</h1><p><a href="/">Back to invoices</a></p>', active="cases")
        s = json.loads(row["state"])
        m, f = s.get("memo") or {}, s.get("fields") or {}
        label, cls, glyph = STATUS.get(row["status"], (row["status"], "na", ""))
        status_line = pipeline.describe_status(conn, case_id)
        if row["status"] == "HOLD":
            status_line = "Nothing paid yet. Munim is waiting for the specific evidence listed below."
        elif row["status"] in ("REJECTED", "DUPLICATE"):
            sev = "block" if row["status"] == "REJECTED" else "duplicate"
            reasons = [x["text"] for x in s.get("findings", []) if x["severity"] == sev]
            if reasons:
                more = f" (+{len(reasons) - 1} more below)" if len(reasons) > 1 else ""
                status_line = f"Nothing paid. {reasons[0]}{more}"
        evidence = db.get_evidence(conn, case_id)
        steps = steps_html(conn, case_id, row)
        sent = conn.execute("SELECT * FROM outbox WHERE case_id = ? ORDER BY id", (case_id,)).fetchall()
        events = conn.execute("SELECT * FROM events WHERE case_id = ? ORDER BY id", (case_id,)).fetchall()

    action = ""
    if row["status"] == "HOLD":
        needs = [x["needs"] for x in s.get("findings", []) if x.get("needs")]
        need_items = "".join(f'<li>{escape(n["evidence"])} <span class="muted">· asked: {escape(n["ask"])}</span></li>'
                             for n in needs)
        receipt_form = ""
        if any(n.get("provide_as") == "receipt" for n in needs):
            asked = {n["ask"] for n in needs if n.get("provide_as") == "receipt"}
            opts = "".join(
                f'<option value="{escape(t)}"{" selected" if t in asked or t.split("@")[0] in asked else ""}>'
                f'{escape(t)}</option>' for t in config.TEAM_EMAILS)
            receipt_form = f"""
<form method="post" action="/cases/{case_id}/provide" class="stack" data-busy="Recording the receipt and re-checking…">
  <h3>Record the delivery receipt</h3>
  <div class="inline-form">
    <div class="field"><label for="by">Confirmed by</label><select id="by" name="by">{opts}</select></div>
    <div class="field"><label for="note">What was delivered, and when</label>
      <input type="text" id="note" name="note" required placeholder="e.g. Payroll onboarding completed on 5 Nov"></div>
  </div>
  <p class="hint">Team identities are typed in for this sandbox and checked against the team list; there is no login.</p>
  <div><button class="btn" type="submit">Record receipt and re-check</button></div>
</form>"""
        action = f"""
<section class="card stack" aria-labelledby="h-needs">
  <h2 id="h-needs">To pay this, Munim needs</h2><ul>{need_items}</ul>{receipt_form}
  <form method="post" action="/cases/{case_id}/resolve" class="recon">
    <input type="hidden" name="action" value="reject">
    <button class="btn btn-danger" type="submit">Reject invoice</button>
    <span class="hint">There is no “approve anyway”: a hold clears only when its missing evidence is supplied.</span>
  </form>
</section>"""

    checks = "".join(
        f'<li><span>{escape(lbl)}</span>{chip(res[0], res[1], res[2])}'
        + (f'<div class="why">{"".join(f"<span>{escape(x["text"])}{evrefs(x["evidence"])}</span>" for x in fs)}</div>'
           if fs else "")
        + "</li>"
        for _, lbl, res, fs in checklist(s))

    def memo_section(title, key, hint=""):
        items = m.get(key) or []
        if not items:
            return ""
        lis = "".join(f'<li>{escape(c["text"])}{evrefs(c["evidence"])}</li>' for c in items)
        return f'<div><h3>{title}</h3>{f"<p class=\"hint\">{hint}</p>" if hint else ""}<ul>{lis}</ul></div>'

    def src_link(e):
        return (f' <a href="{escape(e["url"])}" target="_blank" rel="noopener">source<span class="sr-only"> '
                f'(opens in a new tab)</span></a>') if e.get("url") else ""

    ev_items = "".join(
        f'<li id="{e["id"]}"><span class="id">{e["id"]}</span><div class="stack" style="gap:4px">'
        f'<span>{escape(e["summary"])}{src_link(e)}</span><span class="meta">{chip(*KIND.get(e["kind"], (e["kind"], "na")))}'
        f'<span class="mono">{escape(e["source"])}</span><span>recorded {when(e["retrieved_at"])}</span></span></div></li>'
        for e in evidence)
    sent_rows = "".join(
        f'<tr><td>{escape(o["to_addr"])}</td><td>{escape(o["subject"])}<details><summary class="small">Message</summary>'
        f'<pre>{escape(o["body"])}</pre></details></td><td class="muted small">{when(o["created_at"])}</td></tr>'
        for o in sent)
    timeline = "".join(f'<tr><td class="mono small">{when(e["ts"])}</td><td>{escape(STEP_LABELS.get(e["step"] or "", e["step"] or ""))}'
                       f'</td><td>{escape(e["message"] or "")}</td></tr>' for e in events)

    body = f"""
<div class="stack">
  <p class="small"><a href="/">← All invoices</a> · <span class="mono">{case_id}</span> · {escape(row['source_name'] or '')}</p>
  <h1>{escape(f.get('vendor_name') or 'Unknown vendor')}</h1>
  <p class="mono muted">Invoice {escape(f.get('invoice_number') or 'number unclear')} · {claimed_amount(f)}
    {escape(f.get('currency') or '')} as claimed · from {escape(f.get('sender_email') or 'unknown sender')}</p>
</div>
<div class="banner {cls}" role="status"><span class="g" aria-hidden="true">{glyph}</span>
  <div class="stack" style="gap:2px"><b>{escape(label)}</b><p>{escape(status_line)}</p></div></div>
{action}
<div class="grid-2">
  <section class="card stack" aria-labelledby="h-checks">
    <div class="section-head"><h2 id="h-checks">Payment rules</h2><a href="/policy" class="small">How these work</a></div>
    <p class="hint">Fixed rules decide. “Not checked” means an earlier problem stopped the check from running.</p>
    <ul class="checks">{checks}</ul>
  </section>
  <section class="card stack" aria-labelledby="h-steps">
    <h2 id="h-steps">What Munim did</h2>{steps}
  </section>
</div>
<section class="card memo" aria-labelledby="h-memo">
  <div><h2 id="h-memo">Explanation</h2><p class="lede">{escape(m.get('headline', ''))}</p></div>
  {memo_section('Verified facts', 'verified_facts', 'Copied word for word from a source, with when it was recorded.')}
  {memo_section('What the email claims', 'claims', 'Text from the email itself. Not verified.')}
  {memo_section('Problems found', 'inferences')}
  {memo_section('Could not verify', 'could_not_verify')}
  {memo_section('Summary written by Claude', 'model_summary', 'Model-written. It cites evidence but is not machine-checked.')}
  <p><b>Next:</b> {escape(m.get('next_step', ''))}</p>
  <p class="hint">Email read by {escape(s.get('extract_method', '?'))}; explanation by {escape(s.get('memo_method', '?'))}.</p>
</section>
<section class="section" aria-labelledby="h-ev">
  <h2 id="h-ev">Evidence</h2>
  <div class="table-wrap"><ol class="evidence" aria-labelledby="h-ev">{ev_items}</ol></div>
</section>
<section class="section" aria-labelledby="h-msg">
  <h2 id="h-msg">Messages to the team</h2>
  <div class="table-wrap"><table><caption class="sr-only">Messages sent to the team</caption>
  <thead><tr><th scope="col">To</th><th scope="col">Subject</th><th scope="col">Sent</th></tr></thead>
  <tbody>{sent_rows or '<tr><td colspan="3" class="muted">None yet</td></tr>'}</tbody></table></div>
</section>
<details class="card"><summary>Full timeline</summary>
  <div class="table-wrap" style="margin-top:10px"><table><caption class="sr-only">Every recorded step</caption>
  <thead><tr><th scope="col">When</th><th scope="col">Step</th><th scope="col">What happened</th></tr></thead>
  <tbody>{timeline}</tbody></table></div></details>
<details class="card"><summary>Original email</summary><pre>{escape(row['raw'])}</pre></details>"""
    return page(f"{case_id} {label}", body, active="cases", note=note, error=error)


@app.post("/cases/{case_id}/provide")
def provide(case_id: str, by: str = Form(...), note: str = Form(...)):
    with closing(db.connect()) as conn:
        try:
            pipeline.provide_receipt(conn, case_id, by, note)
        except ValueError as exc:
            return RedirectResponse(f"/cases/{case_id}?error=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/cases/{case_id}?note=" + quote("Receipt recorded. Munim re-checked every rule."),
                            status_code=303)


@app.post("/cases/{case_id}/resolve")
def resolve(case_id: str, action: str = Form(...)):
    with closing(db.connect()) as conn:
        try:
            pipeline.human_resolve(conn, case_id, action, who="ap-lead")  # anything but "reject" is refused
        except ValueError as exc:
            return RedirectResponse(f"/cases/{case_id}?error=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/cases/{case_id}?note=" + quote("Invoice rejected. No money moved."), status_code=303)


@app.post("/resume")
def resume():
    with closing(db.connect()) as conn:
        pending = pipeline.unfinished(conn)
        for case_id in pending:
            db.log_event(conn, case_id, None, "Resumed after interruption", level="warn")
            pipeline.run(conn, case_id)
    return RedirectResponse("/?note=" + quote(f"Resumed {len(pending)} case(s)."), status_code=303)


@app.post("/demo/reset")
def reset_demo(confirm: str = Form("")):
    if confirm != "yes":
        return RedirectResponse("/?error=" + quote("Tick the box to confirm the reset."), status_code=303)
    with closing(db.connect()) as conn:
        db.clear_cases(conn)
    bank.clear()
    return RedirectResponse("/?note=" + quote("Fresh demo: cases, ledger and payouts cleared."), status_code=303)


@app.get("/ledger", response_class=HTMLResponse)
def ledger():
    with closing(db.connect()) as conn:
        recon = recon_line(conn)
        problems, _, _, _ = reconcile(conn)
        ours = "".join(
            f'<tr><td><a class="mono" href="/cases/{p["case_id"]}">{escape(p["invoice_number"])}</a>'
            f'<div class="muted small">{escape(p["vendor_id"])} · {escape(p["po_number"] or "")} · {escape(p["receipt_id"] or "")}</div></td>'
            f'<td class="num">{fmt(p["amount_paise"])}</td>'
            f'<td class="mono small">…{escape(p["account"][-4:])}<br>{escape(p["ifsc"] or "")}</td>'
            f'<td>{status_chip("PAID") if p["status"] == "paid" else chip("Reserved", "hold", "…")}'
            f'<div class="muted small mono">{escape(p["bank_ref"] or "")}</div></td></tr>'
            for p in conn.execute("SELECT * FROM payments ORDER BY id"))
    theirs = "".join(
        f'<tr><td class="mono small">{escape(b["reference"])}</td><td class="num">{fmt(b["amount_paise"])}</td>'
        f'<td class="mono small">…{escape(b["account"][-4:])}<br>{escape(b["ifsc"] or "")}</td>'
        f'<td class="mono small muted">{escape(b["idempotency_key"][:12])}…</td></tr>' for b in bank.all_payouts())
    issues = "".join(f"<li>{escape(x)}</li>" for x in problems)
    body = f"""
<div class="stack"><p class="eyebrow">Reconciliation</p><h1>Ledger</h1>
<p class="lede">Our books beside the bank's. One payout per invoice, whatever crashed or was resent. Each payout carries
the same tag as its ledger entry, so a retry finds the first payout instead of sending money again.</p>{recon}
{f'<ul class="notice bad">{issues}</ul>' if issues else ''}</div>
<div class="grid-2" style="grid-template-columns:repeat(2,minmax(0,1fr))">
<section class="section" aria-labelledby="h-ours" style="min-width:0"><h2 id="h-ours">Our ledger</h2>
<div class="table-wrap"><table><caption class="sr-only">Our ledger entries</caption>
<thead><tr><th scope="col">Invoice</th><th scope="col" class="num">Amount</th><th scope="col">Account / IFSC</th><th scope="col">Status</th></tr></thead>
<tbody>{ours or '<tr><td colspan="4" class="muted">Nothing paid or reserved yet</td></tr>'}</tbody></table></div></section>
<section class="section" aria-labelledby="h-bank" style="min-width:0"><h2 id="h-bank">Sandbox bank payouts</h2>
<div class="table-wrap"><table><caption class="sr-only">Payouts the sandbox bank made</caption>
<thead><tr><th scope="col">Reference</th><th scope="col" class="num">Amount</th><th scope="col">Account / IFSC</th><th scope="col">Tag</th></tr></thead>
<tbody>{theirs or '<tr><td colspan="4" class="muted">No payouts yet</td></tr>'}</tbody></table></div></section>
</div>
<style>@media (max-width:860px){{main .grid-2{{grid-template-columns:minmax(0,1fr)!important}}}}</style>"""
    return page("Ledger", body, active="ledger")


@app.get("/policy", response_class=HTMLResponse)
def show_policy():
    rules = "".join(f"<li>{escape(x)}</li>" for x in policy.REQUIRED_TO_PAY)
    body = f"""
<div class="stack"><p class="eyebrow">Policy</p><h1>When Munim pays</h1>
<p class="lede">Money moves only when every rule below holds. If one doesn't, Munim holds the invoice, names the
smallest piece of missing evidence and asks the one person on the team who can supply it. Look-alike domains and
changed resends of a paid invoice are rejected outright.</p></div>
<section class="card stack" aria-labelledby="h-rules"><h2 id="h-rules">Every one of these must hold</h2><ol class="stack">{rules}</ol>
<p class="hint">Auto-pay limit {fmt(int(config.AUTO_PAY_LIMIT_INR * 100))}. Public data older than
{config.CACHE_MAX_AGE_HOURS:g} hours is fetched again.</p></section>
<div class="grid-2" style="grid-template-columns:repeat(2,minmax(0,1fr))">
<section class="card stack" aria-labelledby="h-auth"><h2 id="h-auth">What authorises a payment</h2>
<p>Our own records: the vendor register (who the vendor is and where its money goes), an approved purchase order,
and a delivery receipt from the team. The email's “From” line is pasted text, so it is a claim. Public-profile checks
can stop a payment, never authorise one.</p></section>
<section class="card stack" aria-labelledby="h-limits"><h2 id="h-limits">What Munim does not establish</h2>
<ul class="stack"><li>Who really sent an email (there is no trusted mail channel in this sandbox).</li>
<li>That someone works at a vendor today: a profile shows what it lists, as of when it was retrieved.</li>
<li>Who on the team confirmed a delivery: identities are typed in, not signed in.</li></ul></section>
</div>
<style>@media (max-width:860px){{main .grid-2{{grid-template-columns:minmax(0,1fr)!important}}}}</style>"""
    return page("When Munim pays", body, active="policy")
