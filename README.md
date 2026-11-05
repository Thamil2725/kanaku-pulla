# Kanaku Pulla

**An accounts-payable agent that pays real vendor invoices exactly once and stops the fake ones.**
Built overnight at ContextCon 04, IIT Kanpur. Theme: *Buying and paying*, with *Trust and compliance*.
A *kanakku pillai* is the village accountant who kept the books in Tamil Nadu.

## The service

Every company has a payables desk, in-house or outsourced, that decides which invoices get paid. It is the last check
before money leaves the company. The work is reading invoices, checking that the vendor is who they say they are,
matching each invoice to what was ordered and delivered, and paying it once. Two failures cost real money: paying a
fraudster (fake "our bank account changed" emails are one of the most common business frauds), and paying the same
invoice twice.

Kanaku Pulla does that job from the vendor's email to the payout. A person is asked only when an answer would change the
result, and Kanaku Pulla says exactly what it needs from them.

## What it does today

```
vendor email ─► read ─► who is the vendor? ─► is the sender real? ─► is the money owed? ─► decide ─► pay once ─► tell the team
                 │        Crustdata identify      Crustdata person      PO + receipt          fixed     idempotent     team inbox
                 │        + our vendor register   search                (three-way match)     rules     sandbox bank   only
                 └─ two independent readings of the amount must agree
```

1. **Read** the email and invoice into fields, each with the exact text it came from (a model, with a strict regex
   reader as a fallback and as a second reading of the amount).
2. **Resolve the vendor.** The sending domain is looked up in Crustdata (`/company/identify`) and matched to our
   vendor register by Crustdata company id, so "Razorpay", "Razorpay Software Pvt. Ltd." and `razorpay.com` are one
   record. Lookalike domains (`razorpay-payments.in`, `razorpaay.com`, `razorpay.co`) are caught.
3. **Check the sender** with Crustdata `/person/search`: do they work at the vendor today, in what role, and if they
   left, when and where they went.
4. **Check the money is owed:** an approved purchase order covers it and a receipt shows it was delivered.
5. **Decide** with fixed rules ([`kanaku_pulla/policy.py`](kanaku_pulla/policy.py)), never the model, so the same case gets the
   same answer however it is worded. See [When Kanaku Pulla pays](#when-kanaku-pulla-pays).
6. **Explain.** Code renders the memo from the evidence records: verified facts are the exact evidence summaries
   (source + retrieval time), email claims are labelled as claims, reasoning is the policy's own findings, failed
   lookups are "could not verify". The model may add a short summary, shown separately and labelled model-written.
   The status line ("Paid …") is read from the ledger, never from the memo.
7. **Act.** In one `BEGIN IMMEDIATE` transaction, re-check the PO's unspent authorisation and the receipt, then reserve
   the invoice and allocate the receipt. Commit, then pay through the bank with a stable idempotency key (outside any
   transaction), then settle. A crash at any point resumes from the saved step; a reservation is never released on a
   timeout, because the bank may already have paid.
8. **Hand over only the exception,** naming the smallest missing evidence and asking the one team member who can
   supply it. When they answer, Kanaku Pulla checks the answer and finishes the same case.

## When Kanaku Pulla pays

All of these must hold ([`kanaku_pulla/policy.py`](kanaku_pulla/policy.py), also shown at `/policy`):

1. The vendor is in our register, matched by its registered domain or Crustdata company id
2. The email's claimed sender address is on the vendor's registered domain (a consistency check, not authentication)
3. The invoice is in a supported currency (INR only) that matches the vendor, purchase order and receipt
4. The bank account number **and** IFSC match our register
5. An approved purchase order has enough unspent authorisation (reserved and paid amounts both count)
6. A receipt from our team shows delivery, and no other payment has been allocated that receipt
7. This invoice number has not been paid or reserved before
8. The amount is within the auto-pay limit
9. Nothing about the named sender contradicts the claim (no look-alike domain, no ended role)

Rejected outright: look-alike domains, and a resend of an already-paid invoice with any different payment detail.

**What authorises a payment is our own records** (vendor register, purchase order, team receipt). The email's
`From:` line is pasted text, so Kanaku Pulla records it as an unauthenticated *claim*; public-profile checks can stop a
payment but never authorise one. There is no "approve anyway": a hold clears only when its specific missing
evidence is supplied, and the full policy is re-run. People can always reject.

## The demo cases (`fixtures/`)

| # | What comes in | What Kanaku Pulla does |
|---|---|---|
| 1 | Razorpay's October invoice, claiming to be from their finance controller, citing an approved PO with a team receipt | Pays it. Run with `KANAKU_CRASH_AT=after_bank` to kill the process after the bank pays; `resume` settles it with one payout. |
| 2 | "Our HDFC account is frozen, pay our new ICICI account today", from `razorpay-payments.in`, signed with the name of a real former Razorpay finance manager, with a hidden "skip verification" note | Rejects it: look-alike domain; the named person's public profile lists their Razorpay role as ended in June 2024 (Crustdata); account and IFSC differ from our register; embedded instruction ignored. |
| 3 | The same invoice resent from `accounts@razorpay.com` as "RZP-2026-42" under "Razorpay Software Pvt. Ltd." | Duplicate: same company, same invoice; points to the ledger entry that paid it. |
| 4 | A payroll onboarding invoice with a valid PO but no delivery receipt | Holds it and asks the ops lead for one thing: a receipt. When they record it, Kanaku Pulla pays the same case. |
| 5 | A "revised" copy of invoice 1 asking for ₹1,81,000 | Rejects it: the invoice is already paid for ₹1,18,000; a real change needs a new invoice or debit note. |
| 6 | A "reminder" renamed `RZP/2026/0042-REMINDER`, same amount, from a vendor mailbox | Holds it as a likely resend of a paid invoice: exact-number matching misses it, and its receipt is already used. It pays only if the team confirms a new delivery for this invoice. |

## Run it

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # macOS/Linux: .venv/bin/pip
cp .env.example .env    # add CRUSTDATA_API_KEY and a model key (GEMINI_API_KEY, OPENROUTER_API_KEY or ANTHROPIC_API_KEY)

python -m kanaku_pulla reset
KANAKU_CRASH_AT=after_bank python -m kanaku_pulla run fixtures/01_razorpay_invoice.txt   # dies after the bank pays
python -m kanaku_pulla resume                                                            # settles it, one payout
python -m kanaku_pulla run fixtures/0[2-5]*.txt
python -m kanaku_pulla provide <case-id> --receipt "Payroll onboarding done 5 Nov" --by ops-lead@kanaku.local
python -m kanaku_pulla recheck <case-id>                                       # after a record is corrected
python -m kanaku_pulla ledger

uvicorn kanaku_pulla.web:app --reload    # http://127.0.0.1:8000

python -m pytest tests                   # 42 offline tests: temp databases, recorded Crustdata answers, real crash test
python -m kanaku_pulla issues            # anything found while upgrading an older database
```

**The web app** has one page per job: Overview, New invoice, Exceptions (held invoices and the one thing each needs), All cases, Ledger (our books beside the bank's), Payment rules and Demo tools (resume, crash demo, fresh start). Submitting an invoice opens a live progress page: each step ticks off as it runs, the findings fill in, and the result appears at the end. Each case has four tabs: Decision (the payment-rule checklist), Evidence, Activity and Email (every field read, and whether it was found in the text).

Without any model key Kanaku Pulla still runs: the regex reader takes over and the memo is rule-written, and the page
says which was used. The tests force that mode (`KANAKU_OFFLINE=1`, `KANAKU_NO_LLM=1`), so they never call live
services. `tests/crustdata_cache.json` holds the six recorded Crustdata answers, with person profiles trimmed to the
fields Kanaku Pulla uses.

## How it's built

- **Python, SQLite, FastAPI.** State lives in `kanaku.db` (cases, evidence, audit timeline, ledger, outbox, API cache);
  the sandbox bank is a separate database, `sandbox_bank.db`, because it stands in for a system we do not control.
- **A model** reads emails into a typed schema and writes a labelled summary. It never decides. Whichever keys are
  set are tried in order: Google Gemini (`gemini-3.5-flash-lite`, free key from AI Studio), OpenRouter (NVIDIA
  Nemotron 3 Ultra, free), then Claude. A failing key falls through to the next; if all fail, the regex reader is used.
  The amount is always read a second time by the regex reader, and a disagreement holds the invoice.
- **Crustdata:** `/company/identify` (domain and name), `/person/search` (name + company id, current and past roles),
  `/company/enrich` for vendors not in the register. Responses are cached with their retrieval time and refetched
  after 24 hours; failures are retried with backoff and, if they persist, recorded as "could not verify".
- **Exactly once:** the payment key is vendor + normalized invoice number (not amount), reserved in the ledger before
  the bank call. The bank refuses a reused key if account, IFSC, amount or currency differ.
- **Books against the bank:** before reserving any payment, our ledger is compared with the bank's payouts; while
  they disagree (someone edited the books), payments pause and the invoice is held. PO room counts the larger of
  the ledger amount and what the bank paid, so lowering a ledger entry frees nothing.
- **Receipts and PO budgets:** `receipt_allocations` gives each receipt one payment (receipt ids are globally unique);
  PO capacity counts reserved and paid amounts, re-checked inside the reservation transaction.
- **Money:** integer paise parsed with `Decimal`; malformed, zero, negative or non-finite amounts are rejected.
- **Upgrades:** `db.migrate` upgrades older databases in place (adds paise columns, backfills receipt allocations) and
  records conflicts in `migration_issues` instead of deleting anything.
- **Team only:** every outgoing message is checked against `KANAKU_TEAM_EMAILS`; Kanaku Pulla never contacts a vendor. The
  team member who records a receipt is a typed-in sandbox identity, checked against that list but not authenticated.

## Approaches we considered

- **One agent loop with Crustdata tools, deciding as it goes.** Flexible, but the same invoice worded differently
  could get a different answer, and a prompt injection in the email could talk it into paying. We kept the model for
  reading and explaining, and put the decision in rules.
- **Fraud scoring** (headcount vs amount, domain age, news). Dropped: a big invoice from a small company proves
  nothing, and a score is hard to defend. Each rule now maps to a control an auditor would recognise.
- **Approve/reject queue for humans.** Replaced by "the smallest missing evidence": a hold names what would let it
  pay, asks the right person, and resumes the same case when they answer.

We had outside reviews during the build (GPT models via Codex). The first round found that the payment key included
the amount (a changed resend could pay twice), that we were verifying companies but not debts (no PO or receipt),
that the regex reader could take a subtotal, and that the cache never expired. A second review of commit `d5c957c`
reproduced five more problems, each now covered by a test: two invoices could share one receipt or overspend a PO;
currency and IFSC weren't checked; "Approve anyway" bypassed every control; model prose could pass as a cited fact;
and the pasted sender was treated as more than a claim. See `KANAKU_PULLA_EXPLAINED.md` for the plain-language status.

## What worked and what didn't

- **Name matching alone is unreliable.** "Razorpay Software Private Limited" matches five companies at equal
  confidence (0.6). Kanaku Pulla only trusts a name when the sending domain or our register agrees, and records the ambiguity.
- **Alternate domains are noisy.** Crustdata lists `bitly.com` among Razorpay's domains (marketing links). Kanaku Pulla treats
  a domain that appears only in that list as a hold, not a match.
- **Profiles describe what a profile lists, not employment or authority.** Evidence says "the public profile of X
  lists…", with the role dates the profile gives and the time we retrieved it. It can stop a payment, never authorise one.
- **Not yet tested:** the model paths against a live key (tested with stubbed replies), and the payment lock under
  many truly simultaneous processes (tested with two interleaved connections).
- **Campus TLS interception** broke Python's certificate checks; we use the OS trust store (`truststore`) instead of
  turning verification off.

## Left out, and what's next

- Real payout rails (Razorpay X / bank APIs) and a real inbox: the sandbox bank and outbox mirror their contracts.
- Bank-change requests always hold; next is a verified call-back flow to the registered contact.
- New-vendor onboarding (KYB with registries, GST and MCA data) is held for a person today.
- Watchers on vendor contacts, so an invoice from someone who has just left is flagged before it arrives.

## A note on the fixtures

Names in the fixtures come from public professional profiles so the live checks have something real to find. In case 2
the email impersonates a former employee; that person is the one being impersonated, not accused of anything. Bank
details, purchase orders and internal emails are made up.
