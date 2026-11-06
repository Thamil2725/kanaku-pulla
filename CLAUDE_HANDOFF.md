# Claude handoff: strengthen Munim and keep the team informed

## Current checkpoint — read before using the historical review below

The app is **Kanaku Pulla**, package `kanaku_pulla`. The team progress file is **`KANAKU_PULLA_EXPLAINED.md`**; update it immediately when taking over and after verified changes. The defect descriptions below are the historical baseline, not current open issues. Current verification: **49 host tests and 41 Core tests passed** on top of Claude's `9cd7199` AP version.

The reusable engine is now **inside this repository at `research_core/`**, installed by `requirements.txt`, and mounted at `/research` through `kanaku_pulla/research_bridge.py`. The old sibling prototype is superseded. Research reuses the existing `llm.structured` routing and OpenRouter key file with an academic-only schema. Source quotes are checked, with one bounded repair. Failed reads (including old Anthropic-only errors) can be retried on the same case; completed actions and payment holds cannot. Missing websites remain unverified. Real OpenRouter extraction of fictional prose completed a local draft with quote checks.

Keep `research_data/` storage separate. The host passes no Core payment authorizations. Do not import synthetic grants, turn geocodes into fraud verdicts, treat changed affiliations as supplier fraud, or treat profiles as sender authentication. The installed site defaults to labeled synthetic research verification; live Crustdata/Google adapters were mocked in this check. Existing AP controls and Claude's provider/vendor context changes are preserved.

The dashboard now has **Buying history**, reading the ten most recent completed purchases from the ledger. It does not count reservations, duplicate submissions, holds or research drafts. Existing sections keep their positions. The Crustdata-inspired color/font pass is implemented without layout changes; see `docs/theme.md`. A separate offline preview runs on port 8001 using copied data, while the user's manual-test server on port 8000 remains uninterrupted and loads the theme at its next normal restart. Read `research_core/INTEGRATION.md` and `research_core/CORE_EXPLAINED.md`, preserve databases, and record actual verification. Corporate evidence import into AP cases remains a future integration step.

## First action: update the team progress document immediately

Before changing application code, read and update **`MUNIM_EXPLAINED.md`** in this directory. This is the existing file the user referred to as `minim_explained`; use the existing filename rather than creating a second explanation document.

The user needs to share an accurate progress update with other team members. Add a prominent **Current progress and known gaps** section near the top. Preserve the useful plain-language explanation, but correct statements elsewhere that imply guarantees the implementation has not established. Do this first, not after finishing the fixes.

That first update must distinguish:

- **Implemented:** invoice extraction, vendor matching, sender-profile lookup, purchase-order/receipt policy, sandbox payment, saved workflow steps, team outbox, web audit UI.
- **Verified locally:** the five fixture outcomes, receipt-driven resumption, and actual process-crash recovery described below.
- **Open problems:** receipt reuse under overlapping cases, missing currency/IFSC controls, blanket approval bypass, and unsupported prose passing citation validation.
- **Not established:** authenticating the email sender from pasted text, current employment merely from a retrieved profile, and live API/model behavior in the independent review.
- **What is being worked on next:** the relevant fixes below, with an honest status for each.

Use labels such as `OPEN`, `IN PROGRESS`, `FIXED - NOT VERIFIED`, and `VERIFIED`. Never label a defect fixed because a patch exists without running its regression check. Explain technical problems in terms a teammate can understand: for example, "Two bills could consume the same delivery confirmation."

Update `MUNIM_EXPLAINED.md` again after each verified fix and before handing work back. Include a short change log and verification summary. Keep source/retrieval dates where relevant, but do not add deadline-based plans, hour budgets, or scheduling estimates.

## Assignment

Build on the existing Munim accounts-payable service. Reproduce and fix the problems below, preserve the working behaviors, and make the documentation match what the application actually proves. This is a concrete implementation handoff, not a request for another proposal.

Inspect the latest files before editing: another contributor may have changed them. Preserve unrelated work, including the existing untracked `MUNIM_EXPLAINED.md`. Do not reset the demo databases or discard history to make tests pass. Use isolated test databases and explicit migrations for schema changes. Keep payments in the sandbox and messages confined to our own team.

## Review baseline and evidence

Independent review date: **2026-11-06**. Reviewed Git commit: **`d5c957c`**. These observations describe that snapshot, not a guarantee about subsequent changes.

The review ran against temporary databases using **six existing cached Crustdata responses**, the **regex extraction fallback**, and **template memos**. It made no live Crustdata or Anthropic calls. It did not modify application source or the original demo databases. Reproduce those conditions for baseline checks, then distinguish any additional live validation you perform.

The original machine-local report is at `D:\Temp\munim-audit-mqa60r2c\report.json`. Its important results are included here so this handoff does not depend on that temporary file surviving.

### Working behaviors to preserve

| Check | Observed result |
| --- | --- |
| `01_razorpay_invoice.txt` | `PAID` |
| `02_bank_change_request.txt` | `REJECTED` |
| `03_resent_under_alias.txt` | `DUPLICATE` |
| `04_needs_receipt.txt` | `HOLD` |
| Supplying the missing receipt for fixture 04 | Same case reached `PAID` |
| `05_modified_resend.txt` | `REJECTED` |
| All five fixtures plus the supplied receipt | Two sandbox payouts total |
| `Subtotal: INR 1,000` followed by `Total: INR 1,180` | Extracted `1180.00` |
| Actual process termination at `after_bank` | Exit 137; workflow at `act`; ledger `reserved`; one bank payout |
| Changed-amount resend before recovery | `REJECTED` |
| Fresh process resumes the interrupted case | `PAID`; still one bank payout |
| `/`, `/ledger`, `/policy` | HTTP 200 in FastAPI TestClient |
| Python modules | All parsed successfully |

The old amount-dependent payment key, subtotal bug, and indefinite cache retention have been addressed in this snapshot. Do not describe them as still-open defects. The key is now vendor plus normalized invoice number; cache expiry is configured. The problems below remain separate concerns.

## 1. Reserve the receipt and purchase-order capacity atomically

**Status: OPEN - reproduced.**

Relevant code: `munim/policy.py` (`open_receipts`, `po_paid_total`, `_three_way_match`), `munim/pipeline.py` (`step_act`), and `munim/db.py` (payments schema).

**Reproduction:** Start two isolated cases from fixture 01. Change only the invoice number in the second from `RZP/2026/0042` to `RZP/2026/0043`. Advance both cases through decision/explanation to `act` before allowing either to act. Both decisions select receipt `GRN-2026-311`. Then run both action steps. Both cases became `PAID`, with **two payouts consuming the same receipt**.

The existing payment key correctly protects one invoice identity. It does not prevent two different invoice identities from spending the same delivery evidence. The decision-to-action gap also requires protecting the PO's remaining authorized amount.

**Implement:**

- For the current one-receipt-per-invoice model, give a receipt allocation a database-enforced unique identity. Explicitly decide whether receipt IDs are globally unique or scoped to a PO, and enforce that same identity everywhere.
- In one write transaction, revalidate the PO approval/currency/remaining capacity, reserve the receipt, and reserve the invoice debt. Include both reserved and paid allocations in capacity accounting.
- Use a SQLite locking/transaction strategy that prevents two connections from validating the same capacity simultaneously, such as an explicit `BEGIN IMMEDIATE` around validation and reservation.
- Audit transaction boundaries carefully: helpers that use their own `with conn:` blocks can commit an outer transaction unexpectedly. Keep evidence logging from splitting the reservation transaction.
- Commit the reservation before calling the bank; do not hold a database transaction open during a network operation. Retain the stable bank idempotency key and reconcile the reserved payment after an uncertain bank outcome.
- On allocation conflict, stop before the bank call, persist an explicit reason, and leave the losing case recoverable. Do not silently discard the conflict through broad `INSERT OR IGNORE` handling.
- Never release a reservation merely because a timeout occurred: the bank may already have committed the payout.

**Acceptance:** The reproduced overlap yields one payout and one receipt allocation. Also test two distinct receipts against insufficient shared PO capacity; combined reserved/paid amounts must not exceed authorization. Crash recovery must retain the reservation and still produce one payout. Test with separate connections/processes as well as deterministic interleaving.

## 2. Validate currency and the complete payment destination

**Status: OPEN - reproduced.**

Relevant code: `munim/policy.py`, `munim/pipeline.py:step_act`, `munim/bank.py:payout`, the vendor register, and the PO/receipt records.

**Reproduction A:** In fixture 01 replace `Amount: INR 1,18,000.00` with these two lines:

```text
Currency: USD
Amount: 1,18,000.00
```

The case became `PAID` and the bank recorded **118000 USD**, even though the order and policy amounts are treated as INR. The control currently compares numeric amounts without establishing matching currencies.

**Reproduction B:** In a fresh isolated database, replace `HDFC0000123` in fixture 01 with `ICIC0000123`, leaving the account number unchanged. The case became `PAID` with the changed IFSC. The sandbox bank's replay comparison also omits IFSC.

**Implement:**

- Make supported currency explicit. An INR-only policy is a valid first scope; hold unsupported, missing, conflicting, or unverifiable currency rather than defaulting it silently.
- Store explicit currencies in trusted PO and receipt records and compare invoice, authorization, receipt, reservation, and bank request currencies consistently. Do not invent currency for unknown legacy records; migrate documented fixture data explicitly.
- Validate the account number **and IFSC** against the trusted register. Missing or changed routing information must not reach the bank through the automatic path.
- Compare the complete normalized payment payload on retries: account, IFSC, amount, and currency. Changed details under the same invoice identity are a conflict, not a new payment key.
- Represent monetary values consistently with decimal arithmetic or integer minor units, and reject non-positive/non-finite or malformed amounts. Avoid float rounding as the monetary validation contract.

**Acceptance:** Both reproductions stop without a payout. A legitimate matching INR invoice still pays. Same-key same-payload replay returns the original payout; altered IFSC/currency/account/amount cannot be accepted as that payout or create another one.

## 3. Remove the blanket approval bypass

**Status: OPEN - reproduced.**

Relevant code: `munim/web.py` ("Approve anyway" and resolve route), `munim/pipeline.py` (`human_resolve`, `step_act`, `provide_receipt`), and the CLI approval command.

**Reproduction:** Run fixture 04 to `HOLD`, then call `human_resolve(..., action='approve')` without providing a receipt. It became `PAID`, with **zero recorded receipts**. `step_act` converts a held decision directly to payment based on the approval action, without rerunning the missing controls.

**Implement:**

- Remove the generic "Approve anyway" payment bypass from the backend as well as the UI/CLI. Hiding the button alone is not a fix.
- Resolve holds by accepting the specific missing evidence, validating its source and applicable team authority, and rerunning policy.
- Preserve the existing receipt-provision workflow, but treat the entered team identity honestly as a sandbox identity unless an authenticated session establishes it.
- Do not allow one approval to waive unrelated constraints such as a missing receipt, bank mismatch, currency mismatch, exhausted PO, or duplicate reservation.
- If a specific authorized exception is implemented, define its exact scope in policy, record who authorized it and the supporting evidence, and still enforce all remaining controls and payment idempotency.

**Acceptance:** A generic approval cannot pay fixture 04 without valid delivery evidence. Valid receipt provision still finishes the same case. A receipt cannot clear an unrelated bank/currency hold. Reject remains available without moving money.

## 4. Make provenance support the claim, not merely reference an existing ID

**Status: OPEN - reproduced.**

Relevant code: `munim/memo.py` (`validate`, `_claude_memo`, `template_memo`), evidence creation in `munim/checks.py` and `munim/pipeline.py`, and audit rendering.

**Reproduction:** Construct a memo with `verified_facts=[{"text": "The moon is made of cheese.", "evidence": ["E1"]}]` and call `validate(memo, {"E1"})`. It returns **no validation errors**. Current validation establishes that an ID exists, not that its content supports the claim.

**Implement:**

- Render verified-fact text from validated evidence records or deterministic templates backed by their structured fields. Do not let free-form generated prose become a verified fact just by attaching a valid ID.
- If the model selects facts, have it select evidence IDs/typed claims and render their fact text in code. Keep model-written interpretations clearly separate and tied to specific evidence; do not promise they are mechanically proven merely because they cite an ID.
- Preserve `fact`, `inference`, and unavailable/unknown distinctions. A failed lookup must not be promoted into proof that a person or company does not exist.
- Include source location or API request/response provenance, retrieval time, and any source observation/update date actually available. Retrieval time does not establish when the underlying fact was true.
- Derive decision headlines and payment status from persisted state. A decision to pay is not a confirmed payout; avoid "Paid" before bank settlement and stale hold memos after a later action.
- Audit upstream evidence wording too: a source reporting a role should be described as "the profile lists...", not as unconditional proof of current employment.

**Acceptance:** An unrelated claim with an existing evidence ID cannot appear as a verified fact. Missing/unknown IDs remain rejected. API failure appears as unavailable. A pre-payment crash cannot leave the UI asserting a completed payment without a settled record.

## 5. Separate company/profile matching from sender authentication

**Status: OPEN LIMITATION - established by code inspection; not an email-transport attack test.**

Relevant code: `munim/extract.py` (pasted `From:` extraction), `munim/checks.py` (domain resolution and sender-profile checks), `munim/policy.py` (registered-domain fallback), `README.md`, and `MUNIM_EXPLAINED.md`.

An attacker can type an approved address into a pasted invoice. Looking up that domain identifies a claimed company; finding a matching person supports a profile claim. Neither proves that the message was sent by that person, nor that they currently have payment authority. The reviewed path does not validate a trusted mail-transport identity.

**Implement/document:**

- Represent claimed sender, company match, public-profile match, and trusted submission/channel verification as different evidence states.
- Do not infer an authenticated sender from the raw `From:` string or copy-pasted mail-authentication headers. Any transport evidence must come from a trusted ingestion boundary.
- For a sandbox trusted-inbox simulation, mark that assumption explicitly and keep arbitrary pasted inputs from silently acquiring it. Do not represent the simulation as production authentication.
- Make policy explicit about which trusted evidence authorizes payment. Mark unresolved requirements as holds when they are required by policy; do not hide them behind a reassuring green profile badge.
- Correct the explanation document's claims that Crustdata verifies "who sent the email" or establishes employment "today". Preserve the useful distinction between invoice authenticity, company identity, payment authority, and whether a debt is owed.

**Acceptance:** A pasted known address cannot become authenticated merely by resolving its company or person profile. The audit page and team explanation show the difference between source claims, sandbox assumptions, and verified actions.

## Verification and delivery instructions

1. Make the immediate `MUNIM_EXPLAINED.md` update described at the top of this file.
2. Reproduce current defects on isolated data. If a contributor already fixed one, verify that fix and record the evidence instead of overwriting it.
3. Implement the fixes with focused regression tests for the financial invariants and policy boundaries above. Preserve the actual process-crash test; a mocked bank response alone does not prove recovery.
4. Run the five fixture outcomes, hold/provide/resume, alias resend, changed resend before recovery, overlapping allocations, complete payment-payload conflicts, and unsupported citation checks.
5. Verify failure handling with mocked 429s, timeouts, malformed responses, exhausted retries, and stale cache data. Keep retry counts bounded and label unavailable facts honestly. Separate mock/cached checks from live endpoint verification.
6. Check schema upgrades against existing records without destructive resets. Detect pre-existing duplicate receipt allocations before adding a unique constraint; report and handle conflicts explicitly rather than deleting records.
7. Update `MUNIM_EXPLAINED.md` and the README to match final behavior. Leave clear entries for anything still unresolved. Report changed files, test results, and the boundaries of the guarantees.

The expected outcome is the existing AP service with defensible payment controls and an accurate team-facing progress document. Do not replace the project with an admissions application or expand into unrelated features as part of these fixes.
