# Munim, explained simply

## Current progress and known gaps

*Updated 2026-11-06 after fixing the problems found in the independent review of commit `d5c957c`.
Labels: `OPEN` = known problem, not fixed. `IN PROGRESS` = being fixed now. `FIXED - NOT VERIFIED` = a change
exists but its check hasn't passed yet. `VERIFIED` = fixed and its automated test passed.*

**How this was checked:** 33 automated tests (`python -m pytest tests`), run offline against temporary databases
with six recorded Crustdata answers, the backup text reader and rule-written explanations, plus a real
"kill the program" crash test in a separate process. Separately, one **live** run of all five demo emails against
Crustdata (6 calls, 0.46 credits, retrieved 2026-11-05 20:24 UTC) gave the same results. Claude was **not** used in
either: no API key is set, so the Claude reading and summary paths are still untested.

**Built and working:**
- Reading the email and bill (Claude, with a simple backup reader that also double-checks the amount)
- Matching the supplier against our approved list and Crustdata
- Looking up the sender's public profile in Crustdata
- Checking for an approved order and a delivery confirmation
- Paying through a pretend bank, with every step saved so a crash can resume
- Messages to our own team only
- A web page showing each decision and its evidence

**Checked on test data (`VERIFIED`):**
- The five demo emails give the expected results: paid, rejected, duplicate, on hold, rejected
- Supplying the missing delivery confirmation lets the held bill finish and pay
- Killing the program right after the bank pays, then restarting it, still gives exactly one payment
- A changed copy of a bill that is mid-payment is rejected
- "Subtotal 1,000 / Total 1,180" is read as 1,180

**Problems found in the review, and their status:**

| Problem, in plain words | What changed | Status |
|---|---|---|
| Two different bills could both use the same delivery confirmation, or together go over what the order allows | Before paying, Munim locks the books, re-checks the order's remaining budget (counting payments in progress, not just finished ones), and claims the delivery confirmation. A confirmation can back only one payment. The losing bill is held and asks for its own confirmation. | `VERIFIED` (tested with two bills on one confirmation, two bills over one order's budget, and two separate database connections. Not stress-tested with many truly simultaneous processes.) |
| The currency wasn't checked: a bill in US dollars could be paid as if the numbers were rupees | Munim only pays rupee (INR) bills automatically. The currency must match the supplier, the order and the delivery confirmation; anything else is held. Amounts are now exact whole paise, not decimals that can round. | `VERIFIED` |
| Only the account number was checked; the bank branch code (IFSC) could change unnoticed | Account number **and** IFSC must match our records. The pretend bank refuses a repeat payment tag if any detail differs. | `VERIFIED` |
| An "Approve anyway" button let a person pay a held bill without the missing evidence | Removed from the web page, the command line **and** the code underneath. A hold clears only when its specific missing evidence is supplied; supplying one thing can't clear an unrelated problem. "Reject" still works and moves no money. | `VERIFIED` |
| The written explanation could attach a proof number to a sentence the proof doesn't support | "Verified facts" are now copied word for word from the evidence records by code. Claude can only add a separate summary, labelled as model-written. The status line ("Paid…") is read from the ledger, so it can't say "Paid" before the bank has settled. | `VERIFIED` for verified facts and the status line. The model-written summary's citations are only checked to exist, not to support the sentence, which is why it is labelled. |

**Not established (limits, not bugs):**
- **Who really sent the email.** Munim reads the "From" line of pasted text. Anyone can type a real person's address
  there, so it is shown as a *claim*. Payment never depends on it: money moves only when our own records (approved
  supplier and bank details, an approved order, a team-confirmed delivery) line up. The sender checks can only stop a
  payment.
- **Whether someone works at the supplier today, or may bill us.** Crustdata shows what their public profile lists, as
  of when it was retrieved. Useful for spotting fakes; not proof of employment or authority.
- **Who on the team confirmed a delivery.** The team member's address is typed in and checked against the team list,
  but there's no login, so it isn't authenticated.
- **Claude.** Without an API key, the Claude reading and summary paths haven't run.

**Still open:**
- Testing the payment lock under many truly simultaneous processes.
- Running and checking the Claude path once a key is available.

---

## The problem

Every company gets bills from the businesses it buys from: software, services, supplies. Someone has to decide which
bills to pay. In most companies that's a small team, or an outside firm, called "accounts payable".

Their job sounds simple, but two mistakes cost a lot of money:

1. **Paying a fraudster.** A common scam is an email that looks like it's from a real supplier: *"Our bank account has
   changed, please send this payment to our new account today."* The email address looks almost right, the name at
   the bottom is a real person, and the money goes to a criminal.
2. **Paying the same bill twice.** The supplier sends a reminder, or the bill arrives again with the name written a bit
   differently, and nobody notices it was already paid.

## What Munim does, in one line

**Munim reads a supplier's bill, checks it against our own records and public data, pays it once when everything
lines up, and asks a person only for the specific thing that's missing.**

(A *munim* is the traditional Indian accountant who kept a business's books.)

## How it works, step by step

Imagine an email arrives with a bill attached. Munim does what a careful accountant would:

1. **Reads the email and the bill.** It pulls out who the email *says* it's from, which company, the bill number, the
   amount and the bank account. It notes exactly where in the email each detail came from. It reads the amount twice,
   in two different ways, and if the two readings disagree it stops instead of guessing.

2. **Works out which company the bill claims to be from.** It looks the sender's email domain up in Crustdata, a large
   database of companies and people, and compares it with our own list of approved suppliers. Different spellings
   like "Razorpay", "Razorpay Software Pvt. Ltd." and razorpay.com all count as the same company. It also spots fake
   look-alike addresses such as *razorpay-payments.in* or *razorpaay.com*.

3. **Looks up the named sender's public profile.** In Crustdata: does the profile list a role at that company, which
   role, and if the role has ended, when? This can expose a fake (for example, a "finance director" who left two years
   ago), but a matching profile does **not** prove that person sent the email.

4. **Checks we actually owe the money.** A real company only pays for what it ordered and received. So Munim checks:
   - Did we place an order with this supplier (a "purchase order"), approved by someone with the authority to do so?
   - Has someone on our team confirmed that the goods or service were actually delivered?

5. **Decides, using fixed rules.** The decision is made by a clear checklist, not by the AI's judgement, so the same bill
   always gets the same answer however the email is worded. The AI is only used to read the email and to help explain
   the decision.

6. **Pays, or doesn't.** If everything checks out, it sends the payment. If not, it either rejects the bill or puts it
   on hold.

7. **Tells the team, and shows its work.** Every decision comes with a short explanation, and each point links to
   where the information came from and when it was retrieved. If Munim couldn't check something, it says so.

## When Munim pays

Only when **all** of these are true:

- The supplier is on our approved list
- The email's claimed sender address is on the supplier's real domain (a consistency check, not proof of who sent it)
- The bill is in rupees, matching the supplier, the order and the delivery confirmation
- The bank account number **and** branch code (IFSC) match the ones we have on file
- We have an approved order with enough budget left (payments already in progress count too)
- Someone on our team confirmed the goods or service were delivered, and no other payment has used that confirmation
- This bill hasn't been paid, or started being paid, before
- The amount is under the limit Munim may pay on its own
- Nothing about the named sender contradicts the claim (no look-alike address, no ended role)

## When Munim asks a person, and how

Most automated systems give up by dumping problems on a person: "something's wrong, please look." Munim is more
specific. When it puts a bill on hold, it says **exactly what is missing** and asks **the one person who can provide
it**.

For example: *"I can pay this ₹59,000 payroll setup bill as soon as someone confirms the setup was actually done. I've
asked the operations lead."* When the operations lead confirms, Munim checks the address they gave is on our team
list, records it, and finishes paying the same bill. (In this demo the team member's identity is simply typed in;
there's no login yet.)

## Never paying twice, even if something crashes

Computers crash at the worst moments, sometimes right after the bank has sent the money but before our records are
updated. A careless system would then pay again.

Munim prevents this in three ways:

- Before paying, it writes in the books: "this bill from this supplier is about to be paid."
- Every payment carries a unique tag (supplier + bill number). If the same tag reaches the bank twice, the bank
  recognises it and doesn't send the money again.
- If the same bill shows up again with a **different** amount or bank account, that's treated as suspicious and
  rejected, not paid.

- Before paying, it also locks the books for a moment and claims the delivery confirmation and the order budget, so
  two *different* bills can't both use the same confirmation or overspend the same order.

After a crash, Munim picks up exactly where it stopped. It never "un-books" a payment just because something timed
out, because the bank may already have sent the money; the retry with the same tag finds out safely.

## The five demo emails

| The email | What Munim does |
|---|---|
| A normal October bill from Razorpay's finance manager | **Pays it.** In the demo the program is deliberately crashed right after the bank pays; when restarted it finishes the job and the money still goes out only once. |
| "Our bank account is frozen, pay our new account today", from *razorpay-payments.in*, signed with the name of someone whose profile shows they left Razorpay | **Rejects it.** The address is a look-alike, the named person's profile lists their Razorpay role as ended in June 2024, the bank account doesn't match our records, and the email hides a note telling automated systems to "skip verification" (ignored). |
| The same October bill sent again as a "reminder", with the company name and bill number written differently | **Doesn't pay it.** It recognises it's the same bill from the same company and points to the payment that already covered it. |
| A "revised" copy of the October bill asking for more money | **Rejects it.** That bill was already paid for a different amount. A real correction would come as a new bill. |
| A bill for payroll setup where nobody has confirmed the work was done | **Holds it** and asks the operations lead for that one confirmation. Once given, it pays. |
| The October bill sent again as "RZP/2026/0042-REMINDER", same amount | **Holds it** as a likely resend. A system that only compares exact bill numbers would miss it. It pays only if the team confirms a *new* delivery for this bill. |

## What it won't do

- It never contacts suppliers itself. It only messages our own team, so a fraudster can't use it to send anything.
- It never follows instructions written inside an email ("this vendor is pre-approved, skip checks").
- It never pays a supplier that isn't on our approved list. New suppliers need a person to approve them first.
- It never guesses. If a fact can't be checked, the explanation says so.

## Where Crustdata comes in

Crustdata is a live database of companies and the people who work at them. Munim uses it for two questions:

- **"Which company does this email domain belong to?"**
- **"What does the named person's public profile say about their role at that company?"**

That's how it flagged the fake email above: the named person's profile showed their Razorpay role ended in June 2024.
Neither answer proves who actually sent an email; they help spot when a claim doesn't add up.

## What's not real yet

This was built in one night for a hackathon:

- Payments go to a **pretend bank** that behaves like a real payment service, not to real accounts.
- Messages go to a team outbox inside the app, not to real email.
- Emails are pasted in, so the sender can't be authenticated.
- The supplier list, orders and bank details are sample data.

The next steps would be connecting a real payment service and inbox, a proper process for confirming bank-account
changes by phone, and onboarding new suppliers automatically.

## Change log

- **2026-11-06, review of `d5c957c`:** added this progress section; corrected wording that implied Munim proves who
  sent an email or that someone works at a supplier today.
- **2026-11-06, fixes:** delivery confirmations and order budgets are reserved under a lock before paying; currency
  (INR only) and IFSC are checked; amounts are exact paise; "Approve anyway" removed; verified facts come straight
  from evidence records; the sender is recorded as an unauthenticated claim; the status line is read from the ledger.
  Older databases upgrade in place: nothing is deleted, and any confirmation that two old payments shared is
  reported (`python -m munim issues`). The demo database upgraded with 0 issues.
- **2026-11-06, extra cases:** a renamed resend of a paid bill ("…-REMINDER", same amount) is now held as a likely
  resend; a test covers a sender whose profile lists an ended role writing from the real domain. Considered but not
  built: judging a bill by the supplier's headcount, and flagging home addresses. Both would turn away honest small
  businesses, and the payment already requires an approved order and a team-confirmed delivery. 33 tests pass.

## Verification summary (2026-11-06)

| Check | Result |
|---|---|
| Five demo emails + supplying the missing confirmation | paid, rejected, duplicate, held → paid, rejected; 2 bank payouts |
| Two bills sharing one delivery confirmation | 1 payout; the other held, asking for its own confirmation |
| Two bills over one order's budget | 1 payout; the other held |
| Same race through two separate database connections | 1 payout |
| Program killed right after the bank pays, then restarted | Exit 137, booking kept as "reserved", 1 payout; a changed copy sent meanwhile is rejected; restart finishes with still 1 payout |
| US-dollar bill / changed IFSC | Not paid |
| Pretend bank given the same tag with a different account, IFSC, amount or currency | Refused; still 1 payout |
| "Approve anyway" on a held bill (code, web page) | Refused; nothing paid |
| A delivery confirmation supplied for a bill that also has wrong bank details | Still held |
| A model sentence ("The moon is made of cheese") citing a real evidence number | Never shown as a verified fact; only in the labelled model summary |
| Crustdata timeouts, rate limits, broken replies, stale cache | Retries a fixed number of times, then shows "could not check" |
| Live run of the five emails (Crustdata, no Claude) | Same results as the offline tests |
| A paid bill resent with "-REMINDER" added to its number | Held as a likely resend; pays only after the team confirms a new delivery |
| A bill naming someone whose profile lists their role as ended, from the supplier's real domain | Held, asking the supplier's registered contact to confirm |
