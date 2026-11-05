# Core extension: progress for the team

The reusable engine is included in this repository under **`research_core/`** and available at **`/research` (Lab procurement)** through a small bridge. Research cases and team drafts have separate storage; the AP payment policy remains unchanged. A clone now contains the complete implementation.

## What it does

- A corporate invoice can end in one **sandbox payment**, but only with a matching trusted approval/receipt record and no unresolved checks.
- An academic materials list ends in one **team-only request draft**. It includes the materials/code request, unresolved supplier/address evidence and any profile-listed change in the author's affiliation. It does not email the author or approve a purchase.
- Both use the same evidence, identity matching, geographic comparison and crash-recovery components.

## Important distinctions

A supplier and a paper's author are different people/organizations. An author's change of job does not make a reagent supplier fraudulent. A residential or PO-box address is not proof of a shell company. Coordinates only help compare a claimed location with an independently trusted reference.

A public profile match does not authenticate the person who uploaded a document. Approval to spend comes from trusted internal records, never from profile matching.

## Implemented

- FastAPI upload/paste interface, JSON API, audit page and command-line runner.
- Text-PDF parsing with page markers; labelled text extraction and optional structured model extraction with quote checks.
- Company/person canonical IDs and evidenced aliases; ambiguous matches stay unverified.
- Crustdata and Google geocoding adapters, rate pacing, retries and dated response cache.
- Validated latitude/longitude, WGS84 geodesic distance and explicit approximate/partial-match handling.
- Separate workflow database and idempotent sandbox action receiver.
- Unique authorization allocation, immutable payment details and team-recipient restrictions.
- Pure invoice handoff adapter and documented router/service integration.
- Host-styled Lab procurement page and Review, Evidence, Location, Team draft and Activity tabs; uploads, sample cases and interrupted-review resumption.
- Shared Gemini/OpenRouter/Claude routing and the existing OpenRouter key file; no research-specific Anthropic key. The research schema excludes payment fields.
- A no-key text template, explicit missing-domain results, and bounded source-quote repair. Failed reads can be retried on the same case without reopening completed actions or payment holds.
- Dashboard Buying history from completed AP ledger records, with vendor, purchase order, invoice, amount, date and case links.

## Verified

All **41 extension tests and 49 host tests passed** after integration with Claude's current AP version. Offline regressions cover invoice payment, academic drafts, actual process death after either effect commits, restart without duplication, overlapping authorization use, altered payment details, ambiguity, missing authorization, retries, PDF parsing and the research pages. Added tests cover shared OpenRouter credentials, retrying old Anthropic holds, rejecting unsupported quotes, missing websites and Buying history that cannot count duplicate or reserved payments.

The test suite uses synthetic providers and mocked HTTP responses. The demo data is fictional and visibly labeled; it is not a live finding about any company, address or researcher.

The current server at `http://127.0.0.1:8000/research` was also checked over HTTP: the address-discrepancy sample completed a team draft, replay reused its case with one effect, all five tabs loaded, and the AP ledger page stayed unchanged. Browser automation had no available session, so the research layout has not received a visual browser check.

## Still limited

- Real OpenRouter extraction was exercised with fictional prose and completed one team draft after source-quote validation. Live research Crustdata/Google requests remain unverified by this build; those adapters use mocked contract tests.
- OCR, real email sending, real payment rails, full mailbox authentication and commercial/residential land-use classification are not implemented.
- A PDF parser is not a hostile-file isolation service; expose this as a local sandbox until ingestion isolation and authentication are added.
- The core payment demo uses exact invoice-specific approval records; general PO budgeting remains the existing AP app's responsibility.
- Policy holds still require review/corrected submissions. The new retry is limited to failed document reads with no actions; the AP evidence-resolution UI is not duplicated here.
- A candidate new affiliation is a lead for team review, not automatic permission to contact a new address.

## How to merge

Read `INTEGRATION.md`. The current host UI is already connected; the standalone service and pure invoice adapter remain available for future integration. Corporate evidence is not automatically imported into existing AP cases. Implement that import deliberately if needed, preserving payment authority in Kanaku Pulla and never treating Core's findings as an instruction to bypass AP checks.
