# How Kanaku Pulla is built

Kanaku Pulla is a Python app that runs on one laptop. A vendor email goes in. It is checked against our own records
and Crustdata. Then fixed rules pay it exactly once, hold it for one missing piece of evidence, or reject it.

## 1. System overview

```mermaid
flowchart TB
  email(["Vendor email + invoice<br/>paste · upload · sample"]):::input
  team(["AP team member<br/>records a receipt · rejects"]):::input

  subgraph host["Kanaku Pulla · Python + FastAPI · one laptop"]
    direction TB
    ui["Web app and CLI · live progress for every invoice"]:::ui
    subgraph runner["Case runner · each step saved · resumes after a crash"]
      direction LR
      s1["1 · Read<br/>Claude, or the<br/>regex reader"]:::step
      s2["2–4 · Identify<br/>vendor, domain,<br/>named sender"]:::step
      s5["5 · Payment rules<br/>fixed code,<br/>not the model"]:::step
      s6["6 · Explain<br/>facts from<br/>evidence"]:::step
      s7["7 · Reserve,<br/>pay, settle"]:::step
      s8["8 · Tell<br/>the team"]:::step
      s1 --> s2 --> s5 --> s6 --> s7 --> s8
    end
    ui --> runner
  end

  email --> ui
  team --> ui

  claude["Claude Opus 5.5<br/>reads the email,<br/>labelled summary"]:::ext
  crust["Crustdata API<br/>company identify<br/>person search<br/>company enrich"]:::ext
  regs[("Trusted records<br/>vendor register<br/>purchase orders")]:::store
  db[("kanaku.db<br/>cases, evidence,<br/>ledger, outbox,<br/>Crustdata cache")]:::store
  bank[("Sandbox bank<br/>one payout<br/>per key")]:::store
  outbox(["Team outbox<br/>team only"]):::output

  s1 -.->|reads| claude
  s2 -->|lookups| crust
  s5 -->|checks| regs
  s6 -->|saved| db
  s7 -->|payout| bank
  s8 -->|message| outbox

  subgraph ext["Optional extension · separate project · in progress"]
    research["Lab procurement at /research<br/>own data · team-only drafts · never pays"]:::muted
  end
  ui -.->|bridge| research

  classDef ui fill:#0b3433,stroke:#0b3433,color:#ffffff
  classDef muted fill:#ffffff,stroke:#8a8a8a,color:#2f3b3a
  classDef input fill:#fff4d6,stroke:#b97a00,color:#1f1600
  classDef step fill:#ffffff,stroke:#0d6862,color:#0f2928
  classDef ext fill:#e3f1ef,stroke:#0d6862,color:#0f2928
  classDef store fill:#eef1f5,stroke:#4a5e5c,color:#0f2928
  classDef output fill:#dcf1e4,stroke:#136a42,color:#0f2928
  style host fill:#f7faf9,stroke:#0b3433,color:#0f2928
  style runner fill:#f0f7f6,stroke:#0d6862,color:#0f2928
  style ext fill:#fafafa,stroke:#8a8a8a,stroke-dasharray:6 4,color:#0f2928
```

Every step writes its result to `kanaku.db` before the next starts, which is how a crashed run resumes. Crustdata
answers are cached for 24 hours and retried on rate limits and timeouts.

## 2. Paying exactly once

```mermaid
sequenceDiagram
  autonumber
  participant R as Case runner
  participant L as kanaku.db (ledger)
  participant B as Sandbox bank
  participant T as Team outbox

  R->>L: BEGIN IMMEDIATE: re-check PO room and that the receipt is unused
  alt receipt already used, or PO has no room
    L-->>R: conflict (nothing reserved)
    R->>T: hold, asking for the one missing piece of evidence
  else all clear
    R->>L: reserve the invoice and allocate the receipt, then commit
    R->>B: payout(key = vendor + invoice number, account, IFSC, amount, currency)
    B-->>R: PAYOUT reference
    Note over R,B: If the process dies here, resume sends the same key.<br/>The bank returns the first payout, and no money moves twice.
    R->>L: settle: reserved → paid, together with the case state
    R->>T: Paid invoice … (one message per key)
  end
```

## 3. Where a person comes in

```mermaid
flowchart LR
  rules{"Payment rules"} -->|"all pass"| pay["Pay once"]
  rules -->|"look-alike domain<br/>or changed resend"| reject["Reject"]
  rules -->|"already in the ledger"| dup["Duplicate:<br/>point to the payment"]
  rules -->|"one thing missing"| hold["Hold"]
  hold --> ask["Ask the one person<br/>who can supply it"]
  ask -->|"team records the receipt"| rules
  ask -->|"team rejects"| reject

  classDef rule fill:#0b3433,stroke:#0b3433,color:#ffffff
  classDef ok fill:#dcf1e4,stroke:#136a42,color:#0f2928
  classDef hold fill:#fcecc9,stroke:#8a4a00,color:#1f1600
  classDef bad fill:#fce3df,stroke:#b0241a,color:#3a0d09
  classDef dup fill:#e4e9f0,stroke:#3c4a60,color:#0f2928
  class rules rule
  class pay ok
  class hold,ask hold
  class reject bad
  class dup dup
```

There is no "approve anyway". A hold clears only when the specific missing evidence arrives, and then every rule
runs again.

## What runs where

| Part | What it is | State |
|---|---|---|
| Web app and CLI | FastAPI, server-rendered pages; `python -m kanaku_pulla` | none of its own |
| Case runner | 8 steps, each saved before the next; a background thread per invoice | `kanaku.db`: `cases`, `events` |
| Model | Claude Opus 5.5, used only to read the email and add a labelled summary. Without a key a regex reader takes over | none |
| Crustdata | company identify (domain and name), person search, company enrich for new vendors | `kanaku.db`: `api_cache` (24 h) |
| Decision | fixed rules in `kanaku_pulla/policy.py` | `kanaku.db`: `evidence` |
| Trusted records | vendor register, purchase orders, team receipts | `data/*.json`, `kanaku.db`: `receipts` |
| Money | ledger reservation, then the sandbox bank, then settle | `kanaku.db`: `payments`, `receipt_allocations`; `sandbox_bank.db` |
| People | team-only outbox; receipts recorded through the web form or CLI | `kanaku.db`: `outbox` |
