"""Web UI: uvicorn kanaku_pulla.web:app --reload

An app shell with one page per job (overview, new invoice, exceptions, cases,
ledger, rules, demo tools) and four tabs per case (decision, evidence, activity,
email). Server-rendered on purpose: no build step, works with JavaScript off,
and every piece of email text is escaped (emails are untrusted input). Every
status shown is read from the ledger and the saved case state.
"""

import json
import re
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import bank, books, config, db, pipeline, policy
from .money import SUPPORTED_CURRENCIES, fmt, to_paise

app = FastAPI(title="Kanaku Pulla")
FIXTURES = config.ROOT / "fixtures"
IST = timezone(timedelta(hours=5, minutes=30))

SAMPLES = {
    "01_razorpay_invoice.txt": ("Genuine invoice", "Approved PO, receipt on file.", "Paid", "ok"),
    "02_bank_change_request.txt": ("Bank-change request", "Look-alike domain, ex-employee's name, hidden instruction.",
                                   "Rejected", "bad"),
    "03_resent_under_alias.txt": ("Resent under another name", "Sample 01 as RZP-2026-42.", "Duplicate · after 01", "dup"),
    "04_needs_receipt.txt": ("No delivery receipt", "Valid PO, no receipt yet.", "On hold → paid after receipt", "hold"),
    "05_modified_resend.txt": ("Revised amount", "Sample 01's number, now ₹1,81,000.", "Rejected · after 01", "bad"),
    "06_renamed_reminder.txt": ("Renamed reminder", "RZP/2026/0042-REMINDER, same amount.", "On hold · after 01", "hold"),
}

STATUS = {  # label, css class, glyph (decorative; the label carries the meaning)
    "PAID": ("Paid", "ok", "✓"), "DUPLICATE": ("Duplicate", "dup", "="), "HOLD": ("On hold", "hold", "!"),
    "REJECTED": ("Rejected", "bad", "✕"), "ERROR": ("Stopped", "bad", "!"), "RUNNING": ("In progress", "na", "…"),
}
FILTERS = [("all", "All"), ("PAID", "Paid"), ("HOLD", "On hold"), ("REJECTED", "Rejected"), ("DUPLICATE", "Duplicate")]

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
SEVERITY = {"block": ("Blocks payment", "bad", "✕"), "duplicate": ("Already in ledger", "dup", "="),
            "hold": ("Needs evidence", "hold", "!"), "warn": ("Note", "note", "i")}
STEP_LABELS = {
    "extract": "Read the email", "resolve": "Identify the vendor", "verify_sender": "Check the named sender",
    "profile": "Profile a new vendor", "decide": "Apply the payment rules", "explain": "Write the explanation",
    "act": "Reserve and pay", "notify": "Tell the team",
}
KIND = {"fact": ("Source", "ok"), "claim": ("Email claim", "hold"), "inference": ("Reasoning", "na"),
        "unavailable": ("Could not check", "bad")}
FIELD_LABELS = [("sender_name", "Sender name"), ("sender_email", "Sender address"), ("vendor_name", "Vendor"),
                ("invoice_number", "Invoice number"), ("invoice_date", "Invoice date"), ("amount", "Amount"),
                ("currency", "Currency"), ("po_number", "Purchase order"), ("bank_account_number", "Account number"),
                ("bank_ifsc", "IFSC")]

FONTS = ("https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@112,600;112,700;125,800"
         "&family=DM+Mono:wght@400;500&family=Instrument+Sans:wght@400;500;600&display=swap")

CSS = """
/* Layout: app shell. A teal ledger-binding rail on the left (top bar on phones), one job per page,
   tabs inside a case. Totals take the accountant's double underline. */
:root{
  --bg:#f2f5f4; --surface:#ffffff; --surface-2:#e9efed; --ink:#0f2928; --muted:#4a5e5c; --line:#d2ddda;
  --rail:#0b3433; --rail-2:#0f413f; --rail-ink:#e8f1ef; --rail-muted:#a7c2be; --rail-line:#1d5452;
  --accent:#0d6862; --accent-ink:#ffffff; --accent-soft:#dcefec;
  --marigold:#f2a900; --marigold-ink:#1f1600; --focus:#0d6862;
  --ok:#136a42; --ok-bg:#dcf1e4; --hold:#8a4a00; --hold-bg:#fcecc9; --bad:#b0241a; --bad-bg:#fce3df;
  --dup:#3c4a60; --dup-bg:#e4e9f0; --note:#6b5000; --note-bg:#f6eec8;
  --font-display:"Archivo", "Arial Narrow", "Segoe UI", system-ui, sans-serif;
  --font-body:"Instrument Sans", "Segoe UI", system-ui, -apple-system, sans-serif;
  --font-mono:"DM Mono", ui-monospace, "Cascadia Mono", Consolas, monospace;
  --r:12px; color-scheme:light;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#0b1413; --surface:#111e1d; --surface-2:#172826; --ink:#e3eeec; --muted:#a1b6b3; --line:#243f3c;
  --rail:#061b1a; --rail-2:#0b2826; --rail-ink:#e3eeec; --rail-muted:#94b2ad; --rail-line:#173b39;
  --accent:#62d0c4; --accent-ink:#04201e; --accent-soft:#12302d;
  --marigold:#ffc24a; --marigold-ink:#1f1600; --focus:#62d0c4;
  --ok:#82dcaa; --ok-bg:#0f2f1f; --hold:#f6c46a; --hold-bg:#33250a; --bad:#ff9e94; --bad-bg:#3a1613;
  --dup:#c4cfe0; --dup-bg:#1f2a3b; --note:#ead676; --note-bg:#2d290c; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#0b1413; --surface:#111e1d; --surface-2:#172826; --ink:#e3eeec; --muted:#a1b6b3; --line:#243f3c;
  --rail:#061b1a; --rail-2:#0b2826; --rail-ink:#e3eeec; --rail-muted:#94b2ad; --rail-line:#173b39;
  --accent:#62d0c4; --accent-ink:#04201e; --accent-soft:#12302d;
  --marigold:#ffc24a; --marigold-ink:#1f1600; --focus:#62d0c4;
  --ok:#82dcaa; --ok-bg:#0f2f1f; --hold:#f6c46a; --hold-bg:#33250a; --bad:#ff9e94; --bad-bg:#3a1613;
  --dup:#c4cfe0; --dup-bg:#1f2a3b; --note:#ead676; --note-bg:#2d290c; color-scheme:dark}
*,*::before,*::after{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:400 1rem/1.55 var(--font-body);-webkit-text-size-adjust:100%}
a{color:var(--accent);text-underline-offset:.2em}
h1,h2,h3{margin:0;line-height:1.15;text-wrap:balance}
h1{font:800 clamp(1.55rem,1.1rem + 1.6vw,2.15rem)/1.1 var(--font-display);font-stretch:125%;letter-spacing:-.01em}
h2{font:700 1.15rem/1.2 var(--font-display);font-stretch:112%}
h3{font:600 1rem/1.3 var(--font-body)}
p{margin:0}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
.skip{position:absolute;left:12px;top:-80px;z-index:30;background:var(--marigold);color:var(--marigold-ink);
  padding:10px 14px;border-radius:8px;font-weight:600}
.skip:focus{top:12px}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px;border-radius:6px}

/* shell */
.shell{display:grid;grid-template-columns:248px minmax(0,1fr);min-height:100%}
.rail{background:var(--rail);color:var(--rail-ink);display:flex;flex-direction:column;gap:22px;
  padding:22px 14px;position:sticky;top:0;height:100vh;overflow-y:auto}
.rail :focus-visible{outline-color:var(--marigold)}
.brand{display:grid;gap:2px;padding:4px 10px 14px;border-bottom:1px solid var(--rail-line);color:var(--rail-ink);
  text-decoration:none}
.brand b{font:800 1.45rem/1 var(--font-display);font-stretch:125%;letter-spacing:.01em}
.brand b span{color:var(--marigold)}
.brand small{color:var(--rail-muted);font-size:.82rem}
.rail nav ul{list-style:none;margin:0;padding:0;display:grid;gap:2px}
.rail .group{font:500 .7rem var(--font-mono);letter-spacing:.12em;text-transform:uppercase;color:var(--rail-muted);
  padding:0 10px 6px}
.rail nav a{display:flex;align-items:center;justify-content:space-between;gap:8px;min-height:42px;padding:8px 10px;
  border-radius:8px;color:var(--rail-ink);text-decoration:none;font-weight:500;border-left:3px solid transparent}
.rail nav a:hover{background:var(--rail-2)}
.rail nav a[aria-current="page"]{background:var(--rail-2);border-left-color:var(--marigold)}
.count{font:500 .75rem var(--font-mono);min-width:24px;text-align:center;padding:1px 7px;border-radius:999px;
  background:var(--marigold);color:var(--marigold-ink)}
.count.quiet{background:var(--rail-line);color:var(--rail-ink)}
.rail-foot{margin-top:auto;display:grid;gap:10px;font-size:.8rem;color:var(--rail-muted);padding:0 10px}
.rail-foot button{background:transparent;color:var(--rail-ink);border:1px solid var(--rail-line);border-radius:8px;
  padding:8px 10px;font:500 .85rem var(--font-body);cursor:pointer;min-height:40px;text-align:left}
.rail-foot button:hover{background:var(--rail-2)}
.sandbox{display:inline-flex;gap:6px;align-items:center;font:500 .72rem var(--font-mono);letter-spacing:.08em;
  text-transform:uppercase;color:var(--marigold)}
.content{min-width:0;padding:30px max(16px,env(safe-area-inset-right,0px)) 56px max(16px,env(safe-area-inset-left,0px))}
.content-inner{max-width:1080px;margin-inline:auto;display:grid;gap:28px}
main:focus{outline:none}
@media (max-width:900px){
  .shell{grid-template-columns:minmax(0,1fr)}
  .rail{position:static;height:auto;padding:14px max(16px,env(safe-area-inset-left,0px)) 12px;gap:12px}
  .brand{border-bottom:0;padding:0 0 4px}
  .rail .group{display:none}
  .rail nav ul{display:flex;flex-wrap:wrap;gap:4px}
  .rail nav a{min-height:38px;border-left:0;border-bottom:3px solid transparent;padding:6px 10px}
  .rail nav a[aria-current="page"]{border-bottom-color:var(--marigold)}
  .rail-foot{margin-top:0;grid-auto-flow:column;justify-content:start;align-items:center;padding:0}
  .content{padding-top:22px}
}

/* page header */
.page-head{display:grid;gap:8px;padding-bottom:18px;border-bottom:1px solid var(--line)}
.page-head .row{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}
.eyebrow{font:500 .75rem var(--font-mono);letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.lede{color:var(--muted);max-width:66ch}
.meta-line{font:400 .9rem var(--font-mono);color:var(--muted);display:flex;gap:6px 18px;flex-wrap:wrap}

/* building blocks */
.panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--r)}
.panel-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap;
  padding:16px 20px;border-bottom:1px solid var(--line)}
.panel-body{padding:18px 20px;display:grid;gap:14px}
.panel-body.flush{padding:0}
.stack{display:grid;gap:12px}
.cols{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:20px;align-items:start}
.cols.even{grid-template-columns:repeat(2,minmax(0,1fr))}
@media (max-width:980px){.cols,.cols.even{grid-template-columns:minmax(0,1fr)}}
.mono{font-family:var(--font-mono);font-variant-numeric:tabular-nums}
.muted{color:var(--muted)}.small{font-size:.875rem}.nowrap{white-space:nowrap}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:44px;padding:10px 18px;
  border-radius:10px;border:1px solid transparent;background:var(--marigold);color:var(--marigold-ink);
  font:600 .95rem var(--font-body);cursor:pointer;text-decoration:none}
.btn:hover{filter:brightness(1.06)}
.btn-line{background:transparent;color:var(--accent);border-color:var(--accent)}
.btn-line:hover{background:var(--accent-soft);filter:none}
.btn-danger{background:transparent;color:var(--bad);border-color:var(--bad)}
.btn-danger:hover{background:var(--bad-bg);filter:none}
.btn[disabled]{opacity:.6;cursor:progress}
label{font-weight:600;font-size:.95rem}
.hint{color:var(--muted);font-size:.875rem;font-weight:400}
textarea,input[type=text],select,input[type=file]{width:100%;font:inherit;color:var(--ink);background:var(--bg);
  border:1px solid var(--line);border-radius:10px;padding:10px 12px;min-height:44px}
textarea{min-height:220px;font:.875rem/1.55 var(--font-mono);resize:vertical}
.field{display:grid;gap:6px}
.form-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,2fr);gap:12px 16px;align-items:start}
@media (max-width:640px){.form-row{grid-template-columns:minmax(0,1fr)}}
.chip{display:inline-flex;align-items:center;gap:6px;padding:2px 10px;border-radius:999px;font:600 .8rem var(--font-body);
  white-space:nowrap}
.chip .g{font-family:var(--font-mono)}
.ok{background:var(--ok-bg);color:var(--ok)}.hold{background:var(--hold-bg);color:var(--hold)}
.bad{background:var(--bad-bg);color:var(--bad)}.dup{background:var(--dup-bg);color:var(--dup)}
.note{background:var(--note-bg);color:var(--note)}.na{background:var(--surface-2);color:var(--muted)}
.notice{padding:12px 16px;border-radius:10px;border:1px solid currentColor}

/* overview figures */
.figures{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:0;background:var(--surface);
  border:1px solid var(--line);border-radius:var(--r)}
.figure{padding:18px 20px;display:grid;gap:4px;border-left:1px solid var(--line)}
.figure:first-child{border-left:0}
.figure .n{font:500 1.6rem/1.2 var(--font-mono);font-variant-numeric:tabular-nums;justify-self:start}
.figure .n.total{border-bottom:4px double var(--ink);padding-bottom:2px}
.figure .l{color:var(--muted);font-size:.875rem}
@media (max-width:760px){.figures{grid-template-columns:repeat(2,minmax(0,1fr))}
  .figure:nth-child(3){border-left:0}.figure:nth-child(n+3){border-top:1px solid var(--line)}}
.recon{display:flex;gap:10px;align-items:center;flex-wrap:wrap}

/* lists and tables */
.list{list-style:none;margin:0;padding:0}
.list>li{padding:14px 20px;border-top:1px solid var(--line);display:grid;gap:6px}
.list>li:first-child{border-top:0}
.item-head{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.95rem}
th,td{text-align:left;padding:11px 16px;vertical-align:top;border-top:1px solid var(--line)}
thead th{border-top:0;font:500 .72rem var(--font-mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
td.num,th.num{text-align:right;font-family:var(--font-mono);font-variant-numeric:tabular-nums;white-space:nowrap}
tbody tr:hover{background:var(--surface-2)}
.filters{display:flex;gap:6px;flex-wrap:wrap}
.filters a{padding:6px 12px;border-radius:999px;border:1px solid var(--line);text-decoration:none;color:var(--ink);
  font-weight:500;font-size:.9rem;min-height:36px;display:inline-flex;align-items:center;gap:6px}
.filters a[aria-current="page"]{background:var(--ink);color:var(--bg);border-color:var(--ink)}

/* samples */
.samples{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
@media (max-width:640px){.samples{grid-template-columns:minmax(0,1fr)}}
.sample{width:100%;height:100%;text-align:left;display:grid;align-content:start;gap:6px;padding:14px 16px;
  border-radius:10px;border:1px solid var(--line);background:var(--surface);color:var(--ink);font:inherit;cursor:pointer}
.sample:hover{border-color:var(--accent);background:var(--accent-soft)}
.sample b{font-weight:600}.sample .d{color:var(--muted);font-size:.875rem}
.sample .n{font:500 .72rem var(--font-mono);letter-spacing:.08em;color:var(--muted)}
.sample .chip{white-space:normal}

/* case */
.tabs{display:flex;gap:4px;flex-wrap:wrap;border-bottom:1px solid var(--line);margin-top:4px}
.tabs a{padding:10px 14px;text-decoration:none;color:var(--muted);font-weight:600;border-bottom:3px solid transparent;
  margin-bottom:-1px;min-height:44px;display:inline-flex;align-items:center;gap:8px}
.tabs a:hover{color:var(--ink)}
.tabs a[aria-current="page"]{color:var(--ink);border-bottom-color:var(--marigold)}
.banner{display:flex;gap:14px;align-items:flex-start;border-radius:var(--r);padding:16px 18px}
.banner .g{font:700 1.25rem/1.25 var(--font-mono)}
.banner p{color:var(--ink)}
.checks{list-style:none;margin:0;padding:0}
.checks li{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 16px;padding:12px 20px;border-top:1px solid var(--line);
  align-items:start}
.checks li:first-child{border-top:0}
.checks li>.chip{align-self:start;margin-top:1px}
.checks .why{grid-column:1/-1;color:var(--muted);font-size:.9rem;display:grid;gap:4px}
.evref{display:inline-block;font:500 .75rem var(--font-mono);padding:0 6px;margin-left:5px;border-radius:5px;
  background:var(--surface-2);color:var(--ink);text-decoration:none;border:1px solid var(--line)}
.evref:hover{border-color:var(--accent)}
dl.facts{display:grid;grid-template-columns:auto minmax(0,1fr);gap:8px 16px;margin:0}
dl.facts dt{color:var(--muted);font-size:.875rem}
dl.facts dd{margin:0;font-family:var(--font-mono);font-size:.9rem;overflow-wrap:anywhere}
.steps{list-style:none;margin:0;padding:0;display:grid;counter-reset:s}
.steps li{display:grid;grid-template-columns:30px minmax(0,1fr);gap:12px;padding-bottom:16px;position:relative}
.steps li::before{counter-increment:s;content:counter(s);width:30px;height:30px;border-radius:8px;display:grid;
  place-items:center;font:500 .8rem var(--font-mono);background:var(--surface-2);color:var(--muted)}
.steps li.done::before{content:"✓";background:var(--ok-bg);color:var(--ok)}
.steps li.current::before{background:var(--marigold);color:var(--marigold-ink)}
.steps li.error::before{content:"!";background:var(--bad-bg);color:var(--bad)}
.steps li:not(:last-child)::after{content:"";position:absolute;left:14px;top:34px;bottom:4px;width:2px;background:var(--line)}
.steps .t{font-weight:600}.steps .m{color:var(--muted);font-size:.875rem}
.memo h3{margin-bottom:4px}
.memo ul{margin:6px 0 0;padding-left:20px;display:grid;gap:6px}
.evidence{list-style:none;margin:0;padding:0}
.evidence li{display:grid;grid-template-columns:52px minmax(0,1fr);gap:4px 14px;padding:14px 20px;border-top:1px solid var(--line);
  scroll-margin-top:16px}
.evidence li:first-child{border-top:0}
.evidence li:target{background:var(--accent-soft)}
.evidence .id{font:500 .9rem var(--font-mono);padding-top:1px}
.evidence .meta{color:var(--muted);font-size:.8rem;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
pre{white-space:pre-wrap;word-break:break-word;font:.86rem/1.55 var(--font-mono);margin:0;padding:16px 18px;
  background:var(--surface-2);border-radius:10px;max-width:100%;overflow-x:auto}
code{font-family:var(--font-mono);font-size:.88em;background:var(--surface-2);padding:1px 5px;border-radius:4px}
details>summary{cursor:pointer;font-weight:600;min-height:32px}
.empty{padding:28px 20px;text-align:left;color:var(--muted)}

/* live run: a progress rail, a step track that ticks off, findings filling in, then the result */
.run-top{display:grid;gap:10px;background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:16px 20px}
.run-meta{display:flex;justify-content:space-between;align-items:center;gap:12px}
.run-state{font:700 1rem var(--font-display);font-stretch:112%;display:inline-flex;align-items:center;gap:10px}
.run-state::before{content:"";width:10px;height:10px;border-radius:50%;background:var(--marigold);animation:beat 1.2s ease-out infinite}
.run-state.done::before{background:var(--ok);animation:none}
.bar-track{height:12px;border-radius:999px;background:var(--surface-2);overflow:hidden}
.bar{height:100%;border-radius:999px;background:linear-gradient(90deg,var(--accent),var(--marigold));transition:width .5s ease}
.run-grid{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:20px;align-items:start}
@media (max-width:980px){.run-grid{grid-template-columns:minmax(0,1fr)}}
.run-steps{list-style:none;margin:0;padding:14px 20px 4px}
.run-step{display:grid;grid-template-columns:38px minmax(0,1fr) auto;gap:2px 14px;padding:0 0 22px;position:relative;align-items:start}
.run-step:not(:last-child)::after{content:"";position:absolute;left:18px;top:42px;bottom:4px;width:2px;background:var(--line)}
.run-step.done:not(:last-child)::after,.run-step.skipped:not(:last-child)::after{background:var(--ok)}
.run-step .mark{width:38px;height:38px;border-radius:50%;display:grid;place-items:center;font:500 .9rem var(--font-mono);
  background:var(--surface);color:var(--muted);border:2px solid var(--line)}
.run-step.done .mark{background:var(--ok);color:var(--surface);border-color:var(--ok)}
.run-step.skipped .mark{border-style:dashed;border-color:var(--ok);color:var(--ok)}
.run-step.current .mark{background:var(--marigold);color:var(--marigold-ink);border-color:var(--marigold);animation:ring 1.4s ease-out infinite}
.run-step.error .mark{background:var(--bad);color:var(--surface);border-color:var(--bad)}
.run-step .t{font-weight:600;padding-top:7px}
.run-step.waiting .t{color:var(--muted);font-weight:500}
.run-step .m{color:var(--muted);font-size:.9rem}
.run-step.current .m{color:var(--ink);font-weight:500}
.run-step .when{font:400 .78rem var(--font-mono);color:var(--muted);padding-top:10px;white-space:nowrap}
@keyframes ring{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--marigold) 60%,transparent)}100%{box-shadow:0 0 0 14px transparent}}
@keyframes beat{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--marigold) 60%,transparent)}100%{box-shadow:0 0 0 9px transparent}}
@keyframes pop{0%{transform:scale(.55);opacity:0}70%{transform:scale(1.08)}100%{transform:scale(1);opacity:1}}
@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.run-steps.intro .run-step.done .mark,.run-steps.intro .run-step.skipped .mark{animation:pop .4s ease-out both;
  animation-delay:calc(var(--i) * 120ms)}
.run-steps.intro .run-step .body{animation:rise .4s ease-out both;animation-delay:calc(var(--i) * 120ms)}
.outcome{display:grid;grid-template-columns:auto minmax(0,1fr);gap:18px;align-items:start;padding:22px;border-radius:var(--r);
  animation:rise .45s ease-out both}
.outcome .big{width:54px;height:54px;border-radius:50%;display:grid;place-items:center;font:700 1.55rem var(--font-mono);
  background:var(--surface);border:2px solid currentColor}
.outcome h2{font-size:1.5rem;font-stretch:125%}
.outcome h2:focus{outline:none}
.outcome p{color:var(--ink)}
.outcome .eyebrow{color:inherit}
@media (prefers-reduced-motion: reduce){*{transition:none!important;animation:none!important;scroll-behavior:auto!important}}
"""

JS = """
(function(){
  var root=document.documentElement, btn=document.getElementById('theme-toggle');
  function isDark(){var t=root.getAttribute('data-theme');
    return t==='dark'||(!t&&window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches);}
  function sync(){if(!btn)return;var d=isDark();btn.setAttribute('aria-pressed',d?'true':'false');
    btn.textContent=d?'Switch to light theme':'Switch to dark theme';}
  if(btn){sync();btn.addEventListener('click',function(){var next=isDark()?'light':'dark';
    root.setAttribute('data-theme',next);try{localStorage.setItem('kanaku-theme',next)}catch(e){}sync();});}
  document.querySelectorAll('form[data-busy]').forEach(function(f){
    f.addEventListener('submit',function(ev){
      var b=ev.submitter;
      if(b&&b.name){var h=document.createElement('input');h.type='hidden';h.name=b.name;h.value=b.value;f.appendChild(h);}
      f.setAttribute('aria-busy','true');
      setTimeout(function(){f.querySelectorAll('button').forEach(function(x){x.disabled=true});},0);
      if(b){var lab=b.querySelector('b')||b;lab.textContent=f.getAttribute('data-busy');}
      var live=document.getElementById('live');if(live){live.textContent=f.getAttribute('data-busy');}
    });
  });
  var box=document.getElementById('run');
  if(box&&box.getAttribute('data-done')!=='1'){
    var url=box.getAttribute('data-poll'),last='',said='',bar=document.getElementById('bar'),
        track=document.getElementById('bar-track'),el=document.getElementById('elapsed'),
        st=document.getElementById('run-state'),live=document.getElementById('live'),
        t0=Date.now(),base=el?(parseFloat(el.getAttribute('data-s'))||0):0;
    function dur(x){return x<1?Math.round(x*1000)+' ms':x.toFixed(1)+' s';}
    var tick=setInterval(function(){if(el)el.textContent=dur(base+(Date.now()-t0)/1000);},100);
    (function poll(){
      fetch(url,{cache:'no-store'}).then(function(r){return r.json();}).then(function(d){
        if(d.html!==last){box.innerHTML=d.html;last=d.html;}
        if(bar)bar.style.width=d.percent+'%';
        if(track)track.setAttribute('aria-valuenow',d.percent);
        if(live&&d.announce!==said){live.textContent=d.announce;said=d.announce;}
        if(d.done){clearInterval(tick);if(el)el.textContent=dur(Number(d.elapsed));
          if(st){st.textContent='Finished';st.classList.add('done');}
          var h=document.getElementById('outcome-h');if(h)h.focus();return;}
        setTimeout(poll,450);
      }).catch(function(){setTimeout(poll,1500);});
    })();
  }
})();
"""

ICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' "
        "height='32' rx='7' fill='%230b3433'/%3E%3Cpath d='M9 10h14M9 16h14' stroke='%23f2a900' stroke-width='2.5' "
        "stroke-linecap='round'/%3E%3Cpath d='M9 22h9' stroke='white' stroke-width='2.5' stroke-linecap='round'/%3E%3C/svg%3E")


# --- shell --------------------------------------------------------------------------------

def shell(title, body, active="", note="", error=""):
    with closing(db.connect()) as conn:
        holds = conn.execute("SELECT COUNT(*) FROM cases WHERE status = 'HOLD'").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
        stuck = len(pipeline.unfinished(conn))

    def item(key, href, label, count=None, quiet=False):
        cur = ' aria-current="page"' if key == active else ""
        badge = ""
        if count:
            badge = (f'<span class="count{" quiet" if quiet else ""}" aria-hidden="true">{count}</span>'
                     f'<span class="sr-only"> ({count})</span>')
        return f'<li><a href="{href}"{cur}><span>{label}</span>{badge}</a></li>'

    nav = (f'<p class="group" id="nav-desk">Desk</p><ul aria-labelledby="nav-desk">'
           + item("overview", "/", "Overview")
           + item("new", "/new", "New invoice")
           + item("exceptions", "/exceptions", "Exceptions", holds)
           + item("cases", "/cases", "All cases", total, quiet=True)
           + '</ul><p class="group" id="nav-books" style="margin-top:14px">Books</p><ul aria-labelledby="nav-books">'
           + item("ledger", "/ledger", "Ledger")
           + item("rules", "/rules", "Payment rules")
           + item("demo", "/demo", "Demo tools", stuck)
           + "</ul>")
    flash = ""
    if error:
        flash += f'<div class="notice bad" role="alert"><b>Not accepted.</b> {escape(error)}</div>'
    if note:
        flash += f'<div class="notice ok" role="status">{escape(note)}</div>'
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · Kanaku Pulla</title>
<link rel="icon" href="{ICON}">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS}">
<script>try{{var t=localStorage.getItem('kanaku-theme');if(t)document.documentElement.setAttribute('data-theme',t)}}catch(e){{}}</script>
<style>{CSS}</style></head>
<body><a class="skip" href="#main">Skip to content</a>
<div class="shell">
<header class="rail">
  <a class="brand" href="/"><b>kanaku<span>-</span>pulla</b><small>payables desk</small></a>
  <nav aria-label="Main">{nav}</nav>
  <div class="rail-foot"><span class="sandbox">● Sandbox</span>
    <button type="button" id="theme-toggle" aria-pressed="false">Switch to dark theme</button></div>
</header>
<div class="content"><main id="main" class="content-inner" tabindex="-1">{flash}{_regions(body)}</main></div>
</div>
<div id="live" class="sr-only" aria-live="polite"></div>
<script>{JS}</script></body></html>""")


def _regions(html):
    """Tables can scroll sideways on small screens; make each wrapper a labelled, focusable region."""
    return re.sub(r'<div class="table-wrap">(\s*)<table><caption class="sr-only">([^<]*)</caption>',
                  r'<div class="table-wrap" tabindex="0" role="region" aria-label="\2">\1<table><caption class="sr-only">\2</caption>',
                  html)


def head(eyebrow, title, lede="", action="", meta=""):
    return (f'<div class="page-head"><p class="eyebrow">{eyebrow}</p><div class="row"><h1>{title}</h1>{action}</div>'
            + (f'<p class="lede">{lede}</p>' if lede else "") + (meta or "") + "</div>")


def chip(label, cls, glyph=""):
    g = f'<span class="g" aria-hidden="true">{glyph}</span>' if glyph else ""
    return f'<span class="chip {cls}">{g}{escape(label)}</span>'


def status_chip(status):
    label, cls, glyph = STATUS.get(status, (status.title(), "na", ""))
    return chip(label, cls, glyph)


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
    return fmt(p) if p else "amount unclear"


def reconcile(conn):
    """Our ledger against the bank (shared with the payment step, which pauses while they disagree)."""
    return books.reconcile(conn)


def recon_line(conn):
    problems, pending, n_ours, n_bank = reconcile(conn)
    if problems:
        return f'<div class="recon">{chip("Books and bank disagree", "bad", "✕")}<span>{escape(problems[0])}</span></div>'
    extra = f" · {len(pending)} awaiting bank confirmation" if pending else ""
    return (f'<div class="recon">{chip("Books and bank agree", "ok", "✓")}'
            f'<span class="muted">{n_ours} ledger entr{"y" if n_ours == 1 else "ies"}, {n_bank} bank '
            f'payout{"" if n_bank == 1 else "s"}{extra}</span></div>')


def cases_table(conn, rows, caption):
    if not rows:
        return '<p class="empty">Nothing here yet.</p>'
    trs = []
    for r in rows:
        s = json.loads(r["state"])
        f = s.get("fields") or {}
        trs.append(
            f'<tr><td class="nowrap"><a href="/cases/{r["id"]}" class="mono">{r["id"]}</a>'
            f'<div class="muted small">{when(r["created_at"])}</div></td>'
            f'<td>{status_chip(r["status"])}</td>'
            f'<td>{escape(f.get("vendor_name") or "Unknown vendor")}'
            f'<div class="muted small mono">{escape(f.get("invoice_number") or "")}</div></td>'
            f'<td class="num">{claimed_amount(f)}</td>'
            f'<td>{escape(pipeline.describe_status(conn, r["id"]))}</td></tr>')
    return (f'<div class="table-wrap"><table><caption class="sr-only">{escape(caption)}</caption>'
            f'<thead><tr><th scope="col">Case</th><th scope="col">Status</th><th scope="col">Vendor and invoice</th>'
            f'<th scope="col" class="num">Amount claimed</th><th scope="col">Where the money stands</th></tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table></div>')


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
            result = SEVERITY[worst]
        elif ran[key]:
            result = ("OK", "ok", "✓")
        else:
            result = ("Not checked", "na", "–")
        rows.append((key, label, result, fs))
    return rows


def steps_html(conn, case_id, row):
    last = {}
    for e in conn.execute("SELECT * FROM events WHERE case_id = ? ORDER BY id", (case_id,)):
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
            msg = "Skipped"
        else:
            msg = state_txt.capitalize()
        items.append(f'<li class="{cls}"><div><div class="t">{escape(STEP_LABELS[step])}'
                     f'<span class="sr-only"> ({state_txt})</span></div><div class="m">{msg}</div></div></li>')
    return f'<ol class="steps">{"".join(items)}</ol>'


# --- overview, new invoice, exceptions, cases ------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def overview(note: str = "", error: str = ""):
    with closing(db.connect()) as conn:
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM cases GROUP BY status").fetchall())
        paid_total = conn.execute("SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE status='paid'").fetchone()[0]
        recon = recon_line(conn)
        recent = conn.execute("SELECT * FROM cases ORDER BY created_at DESC LIMIT 5").fetchall()
        recent_table = cases_table(conn, recent, "Most recent invoices")
        holds = conn.execute("SELECT * FROM cases WHERE status = 'HOLD' ORDER BY created_at DESC").fetchall()
        stuck = pipeline.unfinished(conn)
    attention = []
    for r in holds:
        s = json.loads(r["state"])
        f = s.get("fields") or {}
        need = next((x["needs"]["evidence"] for x in s.get("findings", []) if x.get("needs")), "a decision")
        attention.append(f'<li><div class="item-head"><a href="/cases/{r["id"]}"><b>{escape(f.get("vendor_name") or "Unknown")}'
                         f' · {escape(f.get("invoice_number") or "")}</b></a>{status_chip("HOLD")}</div>'
                         f'<p class="small muted">Needs {escape(need)}</p></li>')
    if stuck:
        attention.insert(0, f'<li><div class="item-head"><a href="/demo"><b>{len(stuck)} interrupted run'
                            f'{"s" if len(stuck) != 1 else ""}</b></a>{chip("Resume", "hold", "!")}</div>'
                            f'<p class="small muted">Resume from Demo tools.</p></li>')
    attention_html = (f'<ul class="list">{"".join(attention)}</ul>' if attention
                      else '<p class="empty">Nothing waiting.</p>')
    body = f"""
{head("Payables desk", "Overview",
      "Pays genuine invoices once. Holds or rejects the rest.",
      '<a class="btn" href="/new">Check a new invoice</a>')}
<section aria-labelledby="h-fig" class="stack">
  <h2 id="h-fig" class="sr-only">Totals</h2>
  <div class="figures">
    <div class="figure"><span class="n total">{fmt(paid_total) if paid_total else "₹0"}</span><span class="l">Paid · {counts.get("PAID", 0)} invoice{"" if counts.get("PAID", 0) == 1 else "s"}</span></div>
    <div class="figure"><span class="n">{counts.get("HOLD", 0)}</span><span class="l">On hold</span></div>
    <div class="figure"><span class="n">{counts.get("REJECTED", 0)}</span><span class="l">Rejected</span></div>
    <div class="figure"><span class="n">{counts.get("DUPLICATE", 0)}</span><span class="l">Duplicates stopped</span></div>
  </div>
  {recon}
</section>
<div class="cols">
  <section class="panel" aria-labelledby="h-recent">
    <div class="panel-head"><h2 id="h-recent">Recent invoices</h2><a href="/cases" class="small">All cases</a></div>
    <div class="panel-body flush">{recent_table}</div>
  </section>
  <section class="panel" aria-labelledby="h-attn">
    <div class="panel-head"><h2 id="h-attn">Needs your attention</h2><a href="/exceptions" class="small">Exceptions</a></div>
    <div class="panel-body flush">{attention_html}</div>
  </section>
</div>"""
    return shell("Overview", body, active="overview", note=note, error=error)


@app.get("/new", response_class=HTMLResponse)
def new_invoice(error: str = ""):
    busy = "Starting…"
    samples = "".join(
        f'<li><button class="sample" type="submit" name="fixture" value="{escape(name)}">'
        f'<span class="n">SAMPLE {name[:2]}</span><b>{escape(title)}</b><span class="d">{escape(desc)}</span>'
        f'<span>{chip("Expect: " + expect, cls)}</span></button></li>'
        for name, (title, desc, expect, cls) in SAMPLES.items() if (FIXTURES / name).exists())
    body = f"""
{head("Payables desk", "New invoice",
      "Paste an email, upload one, or run a sample.")}
<div class="cols even">
  <section class="panel" aria-labelledby="h-paste">
    <div class="panel-head"><h2 id="h-paste">Paste or upload</h2></div>
    <form method="post" action="/cases" enctype="multipart/form-data" class="panel-body" data-busy="{busy}">
      <div class="field"><label for="raw">Vendor email and invoice text</label>
        <textarea id="raw" name="raw" placeholder="From: Name &lt;billing@vendor.com&gt;&#10;Invoice No: …&#10;PO No: …&#10;Amount: INR …&#10;A/c No: …&#10;IFSC: …"></textarea></div>
      <div class="field"><label for="file">Or upload a .txt or .eml file <span class="hint">(optional)</span></label>
        <input type="file" id="file" name="file" accept=".txt,.eml,text/plain,message/rfc822"></div>
      <div><button class="btn" type="submit">Check and process</button></div>
    </form>
  </section>
  <section class="panel" aria-labelledby="h-samples">
    <div class="panel-head"><h2 id="h-samples">Sample emails</h2>
      <span class="hint">Run 01 first.</span></div>
    <form method="post" action="/cases" class="panel-body" data-busy="{busy}">
      <ul class="samples" aria-labelledby="h-samples">{samples}</ul></form>
  </section>
</div>"""
    return shell("New invoice", body, active="new", error=error)


@app.get("/exceptions", response_class=HTMLResponse)
def exceptions():
    with closing(db.connect()) as conn:
        holds = conn.execute("SELECT * FROM cases WHERE status = 'HOLD' ORDER BY created_at DESC").fetchall()
    items = []
    for r in holds:
        s = json.loads(r["state"])
        f = s.get("fields") or {}
        needs = [x["needs"] for x in s.get("findings", []) if x.get("needs")]
        need_html = "".join(f'<li>{escape(n["evidence"])} <span class="muted">· ask {escape(n["ask"])}</span></li>'
                            for n in needs)
        items.append(f"""<li><div class="item-head"><div><a href="/cases/{r['id']}"><b>{escape(f.get('vendor_name') or 'Unknown vendor')}</b></a>
<span class="mono small muted"> · {escape(f.get('invoice_number') or '')} · {claimed_amount(f)}</span></div>
<a class="btn btn-line" href="/cases/{r['id']}">Open case <span class="sr-only">{r['id']}</span></a></div>
<ul class="small" style="margin:0;padding-left:18px">{need_html}</ul></li>""")
    content = (f'<ul class="list">{"".join(items)}</ul>' if items
               else '<p class="empty">Nothing waiting.</p>')
    body = f"""
{head("Payables desk", "Exceptions",
      "Held invoices and the one thing each needs.")}
<section class="panel" aria-labelledby="h-holds"><div class="panel-head"><h2 id="h-holds">Waiting on the team</h2>
<span class="hint">{len(holds)} held</span></div><div class="panel-body flush">{content}</div></section>"""
    return shell("Exceptions", body, active="exceptions")


@app.get("/cases", response_class=HTMLResponse)
def all_cases(status: str = "all"):
    status = status if status in dict(FILTERS) else "all"
    with closing(db.connect()) as conn:
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM cases GROUP BY status").fetchall())
        if status == "all":
            rows = conn.execute("SELECT * FROM cases ORDER BY created_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT * FROM cases WHERE status = ? ORDER BY created_at DESC", (status,)).fetchall()
        table = cases_table(conn, rows, "Invoice list, newest first")
    total = sum(counts.values())
    tabs = "".join(
        f'<a href="/cases?status={k}"{" aria-current=\"page\"" if k == status else ""}>{label}'
        f'<span class="mono small">{total if k == "all" else counts.get(k, 0)}</span></a>'
        for k, label in FILTERS)
    body = f"""
{head("Payables desk", "All cases", "",
      '<a class="btn" href="/new">Check a new invoice</a>')}
<nav class="filters" aria-label="Filter by status">{tabs}</nav>
<section class="panel" aria-labelledby="h-list"><h2 id="h-list" class="sr-only">Invoices</h2>
<div class="panel-body flush">{table}</div></section>"""
    return shell("All cases", body, active="cases")


@app.post("/cases")
async def new_case(raw: str = Form(""), fixture: str = Form(""), file: UploadFile | None = None):
    name = "pasted email"
    if fixture:
        path = FIXTURES / Path(fixture).name
        if not path.exists():
            return RedirectResponse("/new?error=" + quote("That sample no longer exists."), status_code=303)
        raw, name = path.read_text(encoding="utf-8"), path.name
    elif file is not None and file.filename:
        raw, name = (await file.read()).decode("utf-8", errors="replace"), Path(file.filename).name
    if not raw.strip():
        return RedirectResponse("/new?error=" + quote("Paste an email or choose a file first."), status_code=303)
    with closing(db.connect()) as conn:
        case_id = pipeline.create_case(conn, raw, source_name=name)
    start_run(case_id)
    return RedirectResponse(f"/cases/{case_id}/progress", status_code=303)


# --- live progress ----------------------------------------------------------------------------

def start_run(case_id):
    """Run the case on its own thread (and its own connection) so the page can show each step as it happens."""
    def work():
        with closing(db.connect()) as conn:
            pipeline.run(conn, case_id)
    threading.Thread(target=work, name=f"case-{case_id}", daemon=True).start()


RUN_STEPS = pipeline.STEPS[:-1]
DOMAIN_WORDS = {"registered": "registered domain", "lookalike": "look-alike domain", "unrelated": "unrelated domain",
                "listed_alternate": "alternate domain only", "unknown": "unknown domain"}
SENDER_WORDS = {"current": "profile lists a current role", "left": "profile lists the role as ended",
                "not_found": "no public profile found", "role_mailbox": "shared mailbox", "error": "lookup failed",
                "unverifiable": "not checked"}


def _dur(x):
    return f"{round(x * 1000)} ms" if x < 1 else f"{x:.1f} s"


def _seconds(a, b):
    try:
        ta, tb = datetime.fromisoformat(a), datetime.fromisoformat(b)
        return max(0.0, (tb - ta).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def run_view(conn, case_id, intro=False):
    """Everything the progress page shows, rendered on the server (all text escaped)."""
    row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    s = json.loads(row["state"])
    f, res, sender, match = s.get("fields") or {}, s.get("resolution") or {}, s.get("sender") or {}, s.get("match") or {}
    events = conn.execute("SELECT * FROM events WHERE case_id = ? ORDER BY id", (case_id,)).fetchall()
    last = {}
    for e in events:
        if e["step"]:
            last[e["step"]] = e
    done = row["step"] == "done" or row["status"] == "ERROR"
    idx = RUN_STEPS.index(row["step"]) if row["step"] in RUN_STEPS else len(RUN_STEPS)
    finished = len(RUN_STEPS) if row["step"] == "done" else idx
    percent = round(100 * finished / len(RUN_STEPS))
    end = events[-1]["ts"] if done and events else db.now()
    elapsed = _seconds(row["created_at"], end)

    items = []
    for i, step in enumerate(RUN_STEPS):
        e = last.get(step)
        if row["step"] == "done" or i < idx:
            state, glyph, said = "done", "✓", "done"
            detail = escape(e["message"]) if e else ("Skipped" if step == "profile" else "Done")
            if step == "profile" and (not e or e["message"].startswith("Skipped")):
                state, glyph, said = "skipped", "–", "skipped"
        elif i == idx and row["status"] == "ERROR":
            state, glyph, said, detail = "error", "!", "stopped", escape(e["message"]) if e else "Stopped here"
        elif i == idx:
            state, glyph, said, detail = "current", str(i + 1), "in progress", "Working…"
        else:
            state, glyph, said, detail = "waiting", str(i + 1), "waiting", "Waiting"
        when_txt = f'<span class="when">{when(e["ts"])}</span>' if e and state in ("done", "error") else ""
        items.append(
            f'<li class="run-step {state}" style="--i:{i}"><span class="mark" aria-hidden="true">{glyph}</span>'
            f'<div class="body"><div class="t">{escape(STEP_LABELS[step])}<span class="sr-only">: {said}</span></div>'
            f'<div class="m">{detail}</div></div>{when_txt}</li>')

    def fact(label, value, known):
        v = escape(value) if known else '<span class="muted">…</span>'
        return f"<dt>{label}</dt><dd>{v}</dd>"

    vendor = (res.get("vendor") or res.get("claimed") or {}).get("legal_name") or "Not in our register"
    facts = "".join((
        fact("Invoice", f.get("invoice_number") or "unclear", "fields" in s),
        fact("Amount", f"{claimed_amount(f)} {f.get('currency') or ''}".strip(), "fields" in s),
        fact("From", f.get("sender_email") or "unknown", "fields" in s),
        fact("Vendor", vendor, "resolution" in s),
        fact("Domain", DOMAIN_WORDS.get(res.get("domain_status"), res.get("domain_status") or ""), "resolution" in s),
        fact("Sender", SENDER_WORDS.get(sender.get("status"), sender.get("status") or ""), "sender" in s),
        fact("Purchase order", match.get("po_number") or "none matched", "findings" in s),
        fact("Receipt", match.get("receipt_id") or "none available", "findings" in s),
        fact("Decision", (s.get("decision") or "").title(), "decision" in s),
    ))

    outcome, announce = "", ""
    if done:
        label, cls, glyph = STATUS.get(row["status"], (row["status"], "na", ""))
        if row["status"] == "HOLD":
            need = next((x["needs"]["evidence"] for x in s.get("findings", []) if x.get("needs")), "a decision")
            line = f"Nothing paid. Needs {need}."
        elif row["status"] in ("REJECTED", "DUPLICATE"):
            sev = "block" if row["status"] == "REJECTED" else "duplicate"
            reason = next((x["text"] for x in s.get("findings", []) if x["severity"] == sev), "")
            line = f"Nothing paid. {reason}" if reason else pipeline.describe_status(conn, case_id)
        else:
            line = pipeline.describe_status(conn, case_id)
        outcome = (f'<section class="outcome {cls}" aria-labelledby="outcome-h"><span class="big" aria-hidden="true">{glyph}</span>'
                   f'<div class="stack" style="gap:8px"><p class="eyebrow">Result</p>'
                   f'<h2 id="outcome-h" tabindex="-1">{escape(label)}</h2><p>{escape(line)}</p>'
                   f'<div class="recon"><a class="btn" href="/cases/{case_id}">Open the decision</a>'
                   f'<a class="btn btn-line" href="/new">Check another invoice</a></div></div></section>')
        announce = f"Finished. {label}. {line}"
    else:
        announce = f"Step {idx + 1} of {len(RUN_STEPS)}: {STEP_LABELS[RUN_STEPS[idx]]}"

    html = (f'<div class="run-grid">'
            f'<section class="panel" aria-labelledby="h-run"><div class="panel-head"><h2 id="h-run">Steps</h2>'
            f'<span class="hint mono">{finished} of {len(RUN_STEPS)}</span></div>'
            f'<ol class="run-steps{" intro" if intro else ""}">{"".join(items)}</ol></section>'
            f'<div class="stack" style="gap:20px">{outcome}'
            f'<section class="panel" aria-labelledby="h-found"><div class="panel-head"><h2 id="h-found">What it found</h2></div>'
            f'<div class="panel-body"><dl class="facts">{facts}</dl></div></section></div></div>')
    return {"done": done, "status": row["status"], "percent": percent, "elapsed": round(elapsed, 3),
            "announce": announce, "html": html}


@app.get("/cases/{case_id}/progress", response_class=HTMLResponse)
def progress_page(case_id: str):
    conn, row = _load_case(case_id)
    with closing(conn):
        if not row:
            return _missing(case_id)
        v = run_view(conn, case_id, intro=True)
        f = json.loads(row["state"]).get("fields") or {}
    title = escape(f.get("vendor_name") or "Checking invoice") if v["done"] or f else "Checking invoice"
    state_txt = "Finished" if v["done"] else "Working"
    body = f"""
{head(f'<a href="/new">New invoice</a> / <span class="mono">{case_id}</span>', title)}
<div class="run-top">
  <div class="run-meta"><span class="run-state{' done' if v['done'] else ''}" id="run-state">{state_txt}</span>
    <span class="mono muted" id="elapsed" data-s="{v['elapsed']}">{_dur(v['elapsed'])}</span></div>
  <div class="bar-track" id="bar-track" role="progressbar" aria-label="Progress" aria-valuemin="0" aria-valuemax="100"
    aria-valuenow="{v['percent']}"><div class="bar" id="bar" style="width:{v['percent']}%"></div></div>
</div>
<div id="run" data-poll="/cases/{case_id}/progress.json" data-done="{1 if v['done'] else 0}">{v['html']}</div>
<noscript><p><a href="/cases/{case_id}/progress">Refresh to see the latest step</a></p></noscript>"""
    return shell(f"{case_id} progress", body, active="new")


@app.get("/cases/{case_id}/progress.json")
def progress_json(case_id: str):
    conn, row = _load_case(case_id)
    with closing(conn):
        if not row:
            return JSONResponse({"error": "no such case"}, status_code=404)
        return JSONResponse(run_view(conn, case_id))


# --- a case, in four tabs -------------------------------------------------------------------

def case_frame(conn, case_id, tab, inner, note="", error=""):
    row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    s = json.loads(row["state"])
    f = s.get("fields") or {}
    label, cls, glyph = STATUS.get(row["status"], (row["status"], "na", ""))
    status_line = pipeline.describe_status(conn, case_id)
    if row["status"] == "HOLD":
        status_line = "Nothing paid. Waiting for the evidence below."
    elif row["status"] in ("REJECTED", "DUPLICATE"):
        sev = "block" if row["status"] == "REJECTED" else "duplicate"
        reasons = [x["text"] for x in s.get("findings", []) if x["severity"] == sev]
        if reasons:
            more = f" (+{len(reasons) - 1} more)" if len(reasons) > 1 else ""
            status_line = f"Nothing paid. {reasons[0]}{more}"
    n_ev = conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id = ?", (case_id,)).fetchone()[0]
    tabs = "".join(
        f'<a href="/cases/{case_id}{suffix}"{" aria-current=\"page\"" if key == tab else ""}>{name}'
        + (f' <span class="mono small muted">{n_ev}</span>' if key == "evidence" else "") + "</a>"
        for key, suffix, name in (("decision", "", "Decision"), ("evidence", "/evidence", "Evidence"),
                                  ("activity", "/activity", "Activity"), ("email", "/email", "Email")))
    meta = (f'<p class="meta-line"><span>Invoice {escape(f.get("invoice_number") or "unclear")}</span>'
            f'<span>{claimed_amount(f)} {escape(f.get("currency") or "")} as claimed</span>'
            f'<span>From {escape(f.get("sender_email") or "unknown")}</span></p>')
    body = f"""
{head(f'<a href="/cases">All cases</a> / <span class="mono">{case_id}</span>', escape(f.get("vendor_name") or "Unknown vendor"),
      meta=meta)}
<div class="banner {cls}" role="status"><span class="g" aria-hidden="true">{glyph}</span>
  <div class="stack" style="gap:2px"><b>{escape(label)}</b><p>{escape(status_line)}</p></div></div>
<nav class="tabs" aria-label="Case sections">{tabs}</nav>
{inner}"""
    return shell(f"{case_id} {label}", body, active="cases", note=note, error=error)


def evrefs(case_id, ids):
    return "".join(f'<a class="evref" href="/cases/{case_id}/evidence#{escape(i)}" aria-label="Evidence {escape(i)}">'
                   f'{escape(i)}</a>' for i in ids)


def _load_case(case_id):
    conn = db.connect()
    row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    return conn, row


def _missing(case_id):
    return shell("Not found", head("Cases", "No such case", f"There is no case {escape(case_id)}.",
                                   '<a class="btn btn-line" href="/cases">All cases</a>'), active="cases")


@app.get("/cases/{case_id}", response_class=HTMLResponse)
def case_decision(case_id: str, note: str = "", error: str = ""):
    conn, row = _load_case(case_id)
    with closing(conn):
        if not row:
            return _missing(case_id)
        s = json.loads(row["state"])
        m, f, match = s.get("memo") or {}, s.get("fields") or {}, s.get("match") or {}
        pay = conn.execute("SELECT * FROM payments WHERE case_id = ? ORDER BY id DESC LIMIT 1", (case_id,)).fetchone()

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
<form method="post" action="/cases/{case_id}/provide" class="stack" data-busy="Re-checking…">
  <h3>Record the delivery receipt</h3>
  <div class="form-row">
    <div class="field"><label for="by">Confirmed by</label><select id="by" name="by">{opts}</select></div>
    <div class="field"><label for="note">What was delivered, and when</label>
      <input type="text" id="note" name="note" required placeholder="e.g. Payroll onboarding completed on 5 Nov"></div>
  </div>
  <p class="hint">Sandbox: no login, checked against the team list.</p>
  <div><button class="btn" type="submit">Record receipt and re-check</button></div>
</form>"""
            action = f"""
<section class="panel" aria-labelledby="h-needs">
  <div class="panel-head"><h2 id="h-needs">To pay, it needs</h2></div>
  <div class="panel-body"><ul style="margin:0;padding-left:20px">{need_items}</ul>{receipt_form}
  <div class="recon">
    <form method="post" action="/cases/{case_id}/recheck" data-busy="Re-checking…">
      <button class="btn btn-line" type="submit">Re-check now</button></form>
    <form method="post" action="/cases/{case_id}/resolve">
      <input type="hidden" name="action" value="reject">
      <button class="btn btn-danger" type="submit">Reject invoice</button></form>
    <span class="hint">Re-check after a record is corrected. No approve-anyway.</span>
  </div></div>
</section>"""

    checks = "".join(
        f'<li><span>{escape(lbl)}</span>{chip(*res)}'
        + (f'<div class="why">{"".join(f"<span>{escape(x["text"])}{evrefs(case_id, x["evidence"])}</span>" for x in fs)}</div>'
           if fs else "")
        + "</li>"
        for _, lbl, res, fs in checklist(s))
    acct = (f.get("bank_account_number") or "")
    facts = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in (
        ("Decision", escape(s.get("decision", "pending").title())),
        ("Payment", escape(f"{pay['status']} · {pay['bank_ref'] or 'no bank reference yet'}") if pay else "none"),
        ("Pay to", escape(f"a/c …{acct[-4:]} · {f.get('bank_ifsc') or 'IFSC missing'}") if acct else "unclear"),
        ("Purchase order", escape(match.get("po_number") or "not matched")),
        ("Receipt", escape(match.get("receipt_id") or "none allocated")),
        ("Read by", escape(s.get("extract_method", "?"))),
    ))
    inner = f"""
{action}
<div class="cols">
  <section class="panel" aria-labelledby="h-checks">
    <div class="panel-head"><h2 id="h-checks">Payment rules</h2><a href="/rules" class="small">How these work</a></div>
    <ul class="checks">{checks}</ul>
  </section>
  <section class="panel" aria-labelledby="h-glance">
    <div class="panel-head"><h2 id="h-glance">At a glance</h2></div>
    <div class="panel-body"><p>{escape(m.get('headline', ''))}</p><dl class="facts">{facts}</dl>
      <p class="small"><b>Next:</b> {escape(m.get('next_step', ''))}</p>
      <p class="hint">“Not checked”: an earlier problem stopped it.</p></div>
  </section>
</div>"""
    with closing(db.connect()) as conn:
        return case_frame(conn, case_id, "decision", inner, note=note, error=error)


@app.get("/cases/{case_id}/evidence", response_class=HTMLResponse)
def case_evidence(case_id: str):
    conn, row = _load_case(case_id)
    with closing(conn):
        if not row:
            return _missing(case_id)
        s = json.loads(row["state"])
        m = s.get("memo") or {}
        evidence = db.get_evidence(conn, case_id)

        def section(title, key, hint=""):
            items = m.get(key) or []
            if not items:
                return ""
            lis = "".join(f'<li>{escape(c["text"])}{evrefs(case_id, c["evidence"])}</li>' for c in items)
            return f'<div><h3>{title}</h3>{f"<p class=\"hint\">{hint}</p>" if hint else ""}<ul>{lis}</ul></div>'

        def src_link(e):
            return (f' <a href="{escape(e["url"])}" target="_blank" rel="noopener">source<span class="sr-only"> '
                    f'(opens in a new tab)</span></a>') if e.get("url") else ""

        ev_items = "".join(
            f'<li id="{e["id"]}"><span class="id">{e["id"]}</span><div class="stack" style="gap:4px">'
            f'<span>{escape(e["summary"])}{src_link(e)}</span><span class="meta">{chip(*KIND.get(e["kind"], (e["kind"], "na")))}'
            f'<span class="mono">{escape(e["source"])}</span><span>recorded {when(e["retrieved_at"])}</span></span></div></li>'
            for e in evidence)
        inner = f"""
<section class="panel memo" aria-labelledby="h-memo">
  <div class="panel-head"><h2 id="h-memo">Explanation</h2><span class="hint">by {escape(s.get('memo_method', '?'))}</span></div>
  <div class="panel-body">
  {section('Verified facts', 'verified_facts', 'From a source, word for word.')}
  {section('What the email claims', 'claims', 'Not verified.')}
  {section('Problems found', 'inferences')}
  {section('Could not verify', 'could_not_verify')}
  {section('Model-written summary', 'model_summary', 'Cites evidence; not machine-checked.')}
  </div>
</section>
<section class="panel" aria-labelledby="h-ev">
  <div class="panel-head"><h2 id="h-ev">Evidence records</h2><span class="hint">{len(evidence)} records</span></div>
  <ol class="evidence">{ev_items}</ol>
</section>"""
        return case_frame(conn, case_id, "evidence", inner)


@app.get("/cases/{case_id}/activity", response_class=HTMLResponse)
def case_activity(case_id: str):
    conn, row = _load_case(case_id)
    with closing(conn):
        if not row:
            return _missing(case_id)
        steps = steps_html(conn, case_id, row)
        sent = conn.execute("SELECT * FROM outbox WHERE case_id = ? ORDER BY id", (case_id,)).fetchall()
        events = conn.execute("SELECT * FROM events WHERE case_id = ? ORDER BY id", (case_id,)).fetchall()
        sent_items = "".join(
            f'<li><div class="item-head"><b>{escape(o["subject"])}</b><span class="small muted">{when(o["created_at"])}</span></div>'
            f'<p class="small muted">To {escape(o["to_addr"])}</p><details><summary class="small">Message</summary>'
            f'<pre>{escape(o["body"])}</pre></details></li>' for o in sent)
        timeline = "".join(
            f'<tr><td class="mono small nowrap">{when(e["ts"])}</td><td>{escape(STEP_LABELS.get(e["step"] or "", e["step"] or ""))}'
            f'</td><td>{escape(e["message"] or "")}</td></tr>' for e in events)
        inner = f"""
<div class="cols even">
  <section class="panel" aria-labelledby="h-steps"><div class="panel-head"><h2 id="h-steps">What Kanaku Pulla did</h2></div>
    <div class="panel-body">{steps}</div></section>
  <section class="panel" aria-labelledby="h-msg"><div class="panel-head"><h2 id="h-msg">Messages to the team</h2>
    <span class="hint">Team only</span></div>
    <div class="panel-body flush">{f'<ul class="list">{sent_items}</ul>' if sent_items else '<p class="empty">None yet.</p>'}</div></section>
</div>
<section class="panel" aria-labelledby="h-tl"><div class="panel-head"><h2 id="h-tl">Full timeline</h2></div>
  <div class="panel-body flush"><div class="table-wrap"><table><caption class="sr-only">Every recorded step</caption>
  <thead><tr><th scope="col">When</th><th scope="col">Step</th><th scope="col">What happened</th></tr></thead>
  <tbody>{timeline}</tbody></table></div></div></section>"""
        return case_frame(conn, case_id, "activity", inner)


@app.get("/cases/{case_id}/email", response_class=HTMLResponse)
def case_email(case_id: str):
    conn, row = _load_case(case_id)
    with closing(conn):
        if not row:
            return _missing(case_id)
        s = json.loads(row["state"])
        f, quotes, grounded = s.get("fields") or {}, s.get("quotes") or {}, s.get("grounded") or {}
        rows = "".join(
            f'<tr><th scope="row">{label}</th><td class="mono">{escape(str(f.get(k) or "—"))}</td>'
            f'<td class="mono small muted">{escape(quotes.get(k) or "")}</td>'
            f'<td>{chip("Found in text", "ok", "✓") if grounded.get(k) else chip("Not found" if f.get(k) else "Missing", "na", "–")}</td></tr>'
            for k, label in FIELD_LABELS)
        injected = "".join(f"<li>{escape(t)}</li>" for t in f.get("instructions_to_automated_systems") or [])
        inner = f"""
<section class="panel" aria-labelledby="h-fields">
  <div class="panel-head"><h2 id="h-fields">What Kanaku Pulla read</h2><span class="hint">by {escape(s.get('extract_method', '?'))}</span></div>
  <div class="panel-body flush"><div class="table-wrap"><table><caption class="sr-only">Fields read from the email</caption>
  <thead><tr><th scope="col">Field</th><th scope="col">Value</th><th scope="col">Taken from</th><th scope="col">Check</th></tr></thead>
  <tbody>{rows}</tbody></table></div></div>
</section>
{f'<section class="panel" aria-labelledby="h-inj"><div class="panel-head"><h2 id="h-inj">Ignored instructions</h2></div><div class="panel-body"><p class="small">Email text is data. Rules decide.</p><ul class="mono small">{injected}</ul></div></section>' if injected else ''}
<section class="panel" aria-labelledby="h-raw"><div class="panel-head"><h2 id="h-raw">Original email</h2>
  <span class="hint">{escape(row['source_name'] or '')}</span></div>
  <div class="panel-body"><pre>{escape(row['raw'])}</pre></div></section>"""
        return case_frame(conn, case_id, "email", inner)


@app.post("/cases/{case_id}/provide")
def provide(case_id: str, by: str = Form(...), note: str = Form(...)):
    with closing(db.connect()) as conn:
        try:
            pipeline.provide_receipt(conn, case_id, by, note)
        except ValueError as exc:
            return RedirectResponse(f"/cases/{case_id}?error=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/cases/{case_id}?note=" + quote("Receipt recorded and re-checked."),
                            status_code=303)


@app.post("/cases/{case_id}/resolve")
def resolve(case_id: str, action: str = Form(...)):
    with closing(db.connect()) as conn:
        try:
            pipeline.human_resolve(conn, case_id, action, who="ap-lead")  # anything but "reject" is refused
        except ValueError as exc:
            return RedirectResponse(f"/cases/{case_id}?error=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/cases/{case_id}?note=" + quote("Rejected. Nothing paid."), status_code=303)


@app.post("/cases/{case_id}/recheck")
def recheck(case_id: str):
    with closing(db.connect()) as conn:
        try:
            status = pipeline.recheck(conn, case_id, who="ap-lead")
        except ValueError as exc:
            return RedirectResponse(f"/cases/{case_id}?error=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/cases/{case_id}?note=" + quote(f"Re-checked: {STATUS.get(status, (status,))[0]}."),
                            status_code=303)


# --- books ---------------------------------------------------------------------------------

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
        total = conn.execute("SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE status='paid'").fetchone()[0]
    payouts = bank.all_payouts()
    theirs = "".join(
        f'<tr><td class="mono small">{escape(b["reference"])}</td><td class="num">{fmt(b["amount_paise"])}</td>'
        f'<td class="mono small">…{escape(b["account"][-4:])}<br>{escape(b["ifsc"] or "")}</td>'
        f'<td class="mono small muted">{escape(b["idempotency_key"][:12])}…</td></tr>' for b in payouts)
    bank_total = sum(b["amount_paise"] or 0 for b in payouts)
    issues = "".join(f"<li>{escape(x)}</li>" for x in problems)

    def table(caption, headrow, rows, empty, foot):
        return (f'<div class="table-wrap"><table><caption class="sr-only">{caption}</caption><thead><tr>{headrow}</tr></thead>'
                f'<tbody>{rows or f"<tr><td colspan=4 class=muted>{empty}</td></tr>"}</tbody>'
                f'<tfoot><tr><th scope="row">Total paid</th><td class="num"><span style="border-bottom:4px double currentColor">'
                f'{fmt(foot)}</span></td><td colspan="2"></td></tr></tfoot></table></div>')

    body = f"""
{head("Books", "Ledger",
      "Our books beside the bank's. One payout per invoice.")}
<section class="stack" aria-labelledby="h-recon"><h2 id="h-recon" class="sr-only">Reconciliation</h2>{recon}
{f'<ul class="notice bad">{issues}</ul>' if issues else ''}</section>
<div class="stack" style="gap:20px">
<section class="panel" aria-labelledby="h-ours"><div class="panel-head"><h2 id="h-ours">Our ledger</h2></div>
<div class="panel-body flush">{table("Our ledger entries", '<th scope="col">Invoice</th><th scope="col" class="num">Amount</th><th scope="col">Account / IFSC</th><th scope="col">Status</th>', ours, "Nothing paid or reserved yet", total)}</div></section>
<section class="panel" aria-labelledby="h-bank"><div class="panel-head"><h2 id="h-bank">Sandbox bank payouts</h2></div>
<div class="panel-body flush">{table("Payouts the sandbox bank made", '<th scope="col">Reference</th><th scope="col" class="num">Amount</th><th scope="col">Account / IFSC</th><th scope="col">Tag</th>', theirs, "No payouts yet", bank_total)}</div></section>
</div>"""
    return shell("Ledger", body, active="ledger")


@app.get("/rules", response_class=HTMLResponse)
def rules():
    items = "".join(f"<li>{escape(x)}</li>" for x in policy.REQUIRED_TO_PAY)
    body = f"""
{head("Books", "Payment rules",
      "All must hold to pay. Otherwise it holds for one missing thing, or rejects.")}
<section class="panel" aria-labelledby="h-rules"><div class="panel-head"><h2 id="h-rules">Every one of these must hold</h2></div>
<div class="panel-body"><ol class="stack" style="margin:0;padding-left:22px">{items}</ol>
<p class="hint">Auto-pay limit {fmt(int(config.AUTO_PAY_LIMIT_INR * 100))} · public data refreshed after {config.CACHE_MAX_AGE_HOURS:g} h.</p></div></section>
<div class="cols even">
<section class="panel" aria-labelledby="h-auth"><div class="panel-head"><h2 id="h-auth">What authorises a payment</h2></div>
<div class="panel-body"><p>Our vendor register, an approved PO and a team receipt. The email's sender is only a claim;
profile checks can stop a payment, never approve one.</p></div></section>
<section class="panel" aria-labelledby="h-limits"><div class="panel-head"><h2 id="h-limits">What Kanaku Pulla does not establish</h2></div>
<div class="panel-body"><ul class="stack" style="margin:0;padding-left:20px"><li>Who really sent an email.</li><li>That someone works at a vendor today.</li>
<li>Who on the team confirmed delivery (no login).</li></ul></div></section>
</div>"""
    return shell("Payment rules", body, active="rules")


@app.get("/policy")
def old_policy_link():
    return RedirectResponse("/rules", status_code=307)


# --- demo tools ------------------------------------------------------------------------------

@app.get("/demo", response_class=HTMLResponse)
def demo(note: str = "", error: str = ""):
    with closing(db.connect()) as conn:
        stuck = pipeline.unfinished(conn)
        issues = conn.execute("SELECT COUNT(*) FROM migration_issues").fetchone()[0]
    resume = (f'<form method="post" action="/resume" data-busy="Resuming…" class="stack">'
              f'<p>{len(stuck)} run{"s" if len(stuck) != 1 else ""} stopped part-way: '
              f'{", ".join(escape(c) for c in stuck)}.</p><div><button class="btn" type="submit">Resume interrupted cases</button></div></form>'
              if stuck else '<p class="muted">No interrupted runs.</p>')
    body = f"""
{head("Books", "Demo tools")}
<div class="cols even">
<section class="panel" aria-labelledby="h-crash"><div class="panel-head"><h2 id="h-crash">Crash in the middle of a payment</h2></div>
<div class="panel-body"><p>Start fresh, then run in a second PowerShell window:</p>
<pre>$env:KANAKU_CRASH_AT="after_bank"
.venv\\Scripts\\python -m kanaku_pulla run fixtures\\01_razorpay_invoice.txt
Remove-Item Env:KANAKU_CRASH_AT</pre>
<p>Then resume here. The ledger shows one payout.</p></div></section>
<section class="panel" aria-labelledby="h-resume"><div class="panel-head"><h2 id="h-resume">Resume interrupted runs</h2></div>
<div class="panel-body">{resume}<p class="hint">Retries reuse the same tag, so nothing pays twice.</p></div></section>
</div>
<section class="panel" aria-labelledby="h-reset"><div class="panel-head"><h2 id="h-reset">Start a fresh demo</h2></div>
<form method="post" action="/demo/reset" class="panel-body">
  <p>Deletes cases, ledger, receipts, messages and payouts. Keeps cached Crustdata answers.</p>
  <div><input type="checkbox" id="confirm" name="confirm" value="yes" required>
  <label for="confirm">Delete this demo's data</label></div>
  <div><button class="btn btn-danger" type="submit">Delete and start fresh</button></div>
</form></section>"""
    return shell("Demo tools", body, active="demo", note=note, error=error)


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
        return RedirectResponse("/demo?error=" + quote("Tick the box to confirm the reset."), status_code=303)
    with closing(db.connect()) as conn:
        db.clear_cases(conn)
    bank.clear()
    return RedirectResponse("/demo?note=" + quote("Demo data cleared."), status_code=303)
