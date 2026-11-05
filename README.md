# Munim

**An accounts-payable agent that pays real vendor invoices exactly once and stops the fake ones.**
Built overnight at ContextCon 04, IIT Kanpur. Theme: *Buying and paying*, with *Trust and compliance*.

## The service

Every company has a payables desk, in-house or outsourced, that decides which invoices get paid. It is the last check
before money leaves the company. The work is reading invoices, checking that the vendor is who they say they are,
matching each invoice to what was ordered and delivered, and paying it once. Two failures cost real money: paying a
fraudster (fake "our bank account changed" emails are one of the most common business frauds), and paying the same
invoice twice.

Munim does that job from the vendor's email to the payout. A person is asked only when an answer would change the
result, and Munim says exactly what it needs from them.

## What it does today

```
vendor email ─► read ─► who is the vendor? ─► is the sender real? ─► is the money owed? ─► decide ─► pay once ─► tell the team
                 │        Crustdata identify      Crustdata person      PO + receipt          fixed     idempotent     team inbox
                 │        + our vendor register   search                (three-way match)     rules     sandbox bank   only
                 └─ two independent readings of the amount must agree
```

1. **Read** the email and invoice into fields, each with the exact text it came from (Claude, with a strict regex
   reader as a fallback and as a second reading of the amount).
2. **Resolve the vendor.** The sending domain is looked up in Crustdata (`/company/identify`) and matched to our
   vendor register by Crustdata company id, so "Razorpay", "Razorpay Software Pvt. Ltd." and `razorpay.com` are one
   record. Lookalike domains (`razorpay-payments.in`, `razorpaay.com`, `razorpay.co`) are caught.
3. **Check the sender** with Crustdata `/person/search`: do they work at the vendor today, in what role, and if they
   left, when and where they went.
4. **Check the money is owed:** an approved purchase order covers it and a receipt shows it was delivered.
5. **Decide** with fixed rules ([`munim/policy.py`](munim/policy.py)), never the model, so the same case gets the
   same answer however it is worded. See [When Munim pays](#when-munim-pays).
6. **Explain.** Claude writes a memo where every claim cites evidence (`E3`); code rejects any uncited claim and asks
   again, then falls back to a plain memo. Facts, inferences and "could not verify" are kept apart.
7. **Act.** Reserve the debt in the ledger, pay through the bank with an idempotency key, then settle. A crash at any
   point resumes from the saved step without paying twice.
8. **Hand over only the exception,** naming the smallest missing evidence and asking the one team member who can
   supply it. When they answer, Munim checks the answer and finishes the same case.

## When Munim pays

All of these must hold:

1. The vendor is in our register, matched by its registered domain or Crustdata company id
2. The email came from the vendor's registered domain
3. The bank account matches the one in our register
4. An approved purchase order for this vendor covers the amount
5. A receipt shows the goods or service were delivered
6. This invoice number has not been paid or reserved before
7. The amount is within the auto-pay limit
8. The sender is an approved contact, verified at the vendor, or a vendor mailbox on the registered domain

Rejected outright: lookalike domains, and a resend of an already-paid invoice with a different amount or account.

## The demo cases (`fixtures/`)

| # | What comes in | What Munim does |
|---|---|---|
| 1 | Razorpay's October invoice, from their finance controller | Pays it. Run with `MUNIM_CRASH_AT=after_bank` to kill the process after the bank pays; `resume` settles it with one payout. |
| 2 | "Our HDFC account is frozen, pay our new ICICI account today", from `razorpay-payments.in`, signed by a real former Razorpay finance manager, with a hidden "skip verification" note | Rejects it: lookalike domain, sender left Razorpay in June 2024 (Crustdata), bank account differs from our register, embedded instruction ignored. |
| 3 | The same invoice resent from `accounts@razorpay.com` as "RZP-2026-42" under "Razorpay Software Pvt. Ltd." | Duplicate: same company, same invoice; points to the ledger entry that paid it. |
| 4 | A payroll onboarding invoice with a valid PO but no delivery receipt | Holds it and asks the ops lead for one thing: a receipt. When they record it, Munim pays the same case. |
| 5 | A "revised" copy of invoice 1 asking for ₹1,81,000 | Rejects it: the invoice is already paid for ₹1,18,000; a real change needs a new invoice or debit note. |

## Run it

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # macOS/Linux: .venv/bin/pip
cp .env.example .env    # add ANTHROPIC_API_KEY and CRUSTDATA_API_KEY

python -m munim reset
MUNIM_CRASH_AT=after_bank python -m munim run fixtures/01_razorpay_invoice.txt   # dies after the bank pays
python -m munim resume                                                            # settles it, one payout
python -m munim run fixtures/0[2-5]*.txt
python -m munim provide <case-id> --receipt "Payroll onboarding done 5 Nov" --by ops-lead@munim.local
python -m munim ledger

uvicorn munim.web:app --reload    # http://127.0.0.1:8000
```

Without an Anthropic key Munim still runs: the regex reader and a template memo take over, and the memo says so.

## How it's built

- **Python, SQLite, FastAPI.** State lives in `munim.db` (cases, evidence, audit timeline, ledger, outbox, API cache);
  the sandbox bank is a separate database, `sandbox_bank.db`, because it stands in for a system we do not control.
- **Claude** (`claude-opus-5-5`) reads emails into a typed schema and writes the cited memo. It never decides.
- **Crustdata:** `/company/identify` (domain and name), `/person/search` (name + company id, current and past roles),
  `/company/enrich` for vendors not in the register. Responses are cached with their retrieval time and refetched
  after 24 hours; failures are retried with backoff and, if they persist, recorded as "could not verify".
- **Exactly once:** the payment key is vendor + normalized invoice number (not amount), reserved in the ledger before
  the bank call. The bank refuses a reused key with different details.
- **Team only:** every outgoing message is checked against `MUNIM_TEAM_EMAILS`; Munim never contacts a vendor.

## Approaches we considered

- **One agent loop with Crustdata tools, deciding as it goes.** Flexible, but the same invoice worded differently
  could get a different answer, and a prompt injection in the email could talk it into paying. We kept the model for
  reading and explaining, and put the decision in rules.
- **Fraud scoring** (headcount vs amount, domain age, news). Dropped: a big invoice from a small company proves
  nothing, and a score is hard to defend. Each rule now maps to a control an auditor would recognise.
- **Approve/reject queue for humans.** Replaced by "the smallest missing evidence": a hold names what would let it
  pay, asks the right person, and resumes the same case when they answer.

We had two outside reviews of the plan mid-build (GPT models via Codex). They found real holes we then fixed: the payment key
included the amount (a changed resend could pay twice), we were verifying companies but not debts (no PO or receipt),
the regex reader could take a subtotal, and the cache never expired.

## What worked and what didn't

- **Name matching alone is unreliable.** "Razorpay Software Private Limited" matches five companies at equal
  confidence (0.6). Munim only trusts a name when the sending domain or our register agrees, and records the ambiguity.
- **Alternate domains are noisy.** Crustdata lists `bitly.com` among Razorpay's domains (marketing links). Munim treats
  a domain that appears only in that list as a hold, not a match.
- **Profiles describe employment, not authority.** A current finance title is supporting evidence, never enough on
  its own; payment needs the register, the PO and the receipt.
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
