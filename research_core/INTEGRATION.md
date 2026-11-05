# Integrate with Kanaku Pulla without replacing its controls

The main package is **`kanaku_pulla`**. The extension is included in this repository under **`research_core/`**, package **`diligence_core`**. The site integration is implemented; the engine remains independently runnable. No sibling checkout is required.

## Current site integration

`kanaku_pulla/research_bridge.py` calls `diligence_core.host_ui.mount_research`. The small change in `kanaku_pulla/web.py` attaches the bridge and adds **Lab procurement** to the sidebar. The original AP policy, ledger and payment execution code are unchanged.

The embedded UI lives at `/research`, with case tabs for Review, Evidence, Location, Team draft and Activity. It also mounts `/core` API routes. The host passes an empty corporate authorization register, so this installation cannot produce Core payments. Research cases and drafts are stored in `munim/research_data/`, separate from both AP databases.

To reproduce the installed environment:

```powershell
cd G:\contextcon\munim
.\.venv\Scripts\python.exe -m pip install -r requirements-research.txt
.\.venv\Scripts\python.exe -m uvicorn kanaku_pulla.web:app --host 127.0.0.1 --port 8000
```

The main `requirements.txt` includes this editable local package. A fresh clone contains the engine, tests and packaged fictional fixtures. If the package is absent, AP still starts and `/research` explains how to install it.

Configuration uses `KANAKU_RESEARCH_MODE=demo` by default; sample data is prominently labeled fictional. `KANAKU_RESEARCH_DATA` overrides the research directory. For live checks, set `KANAKU_RESEARCH_MODE=live`, configure Crustdata through the existing app credential loader, and optionally set `GOOGLE_MAPS_API_KEY` and `KANAKU_RESEARCH_REGISTRY` for geographic comparisons. Missing geocoding remains explicitly unavailable. Live mode never loads the synthetic register.

The bridge delegates to `kanaku_pulla.llm.structured` with an academic-only schema. Research honors `KANAKU_LLM`, the existing provider order/model settings, and the same OpenRouter environment key or git-ignored key file as invoices. No separate Anthropic key is required. Core validates each extracted string against its verbatim quote. Missing supplier/institution domains stay unverified. Team recipients come from `KANAKU_TEAM_EMAILS`. Restart after configuration changes.

Unit tests mock external providers; `scripts/verify_research_reader.py` is a separate opt-in live reader check with fictional input. Missing credentials or evidence remain explicit unknowns. This is still a local, unauthenticated sandbox.

## Option 1: independent service

Run Core on port 8011. Call its `/core/cases/upload` or `/core/cases/text` endpoint and retrieve the result/evidence. Export a corporate invoice using `/core/cases/{id}/ap-handoff`.

The handoff includes `schema_version`, the original text, normalized claims, evidence, findings, stable input hash/import key and a flag showing whether a standalone sandbox effect already occurred. **It always sets `payment_authorization=false` and `submitter_authenticated=false`.**

This remains an alternative to embedding the UI. Keep core data under `core_runtime/`; do not point it at `kanaku.db` or `sandbox_bank.db`.

## Option 2: mount the router

For another host, install this source project (`pip install -e ./research_core` from the repo root), then add explicitly to the existing FastAPI application:

```python
from pathlib import Path
from diligence_core.api import create_router
from diligence_core.engine import Engine

core = Engine(
    Path("core_runtime"),
    mode="live",  # or explicitly demo for synthetic walkthroughs
    authorizations=[],  # AP remains the only payment authority
    registry=trusted_vendor_coordinate_register,
    team=config.TEAM_EMAILS,
)
app.include_router(create_router(core), prefix="/core")
```

`trusted_vendor_coordinate_register` is an operator-owned mapping like `diligence_core/fixtures/registered_locations.json`, not data copied from uploaded invoices. Choose a separate absolute data directory when deploying. `create_router` adds only API routes and audit pages; the standalone upload home page belongs to `create_app`.

## Invoice adapter contract

`diligence_core.integration.ap_handoff(case)` is a pure function. It does not import the AP package, write to AP tables or send money.

At the receiving side:

1. Accept only `kind=INVOICE`. BOMs have no authority to pay.
2. Persist the packet and a unique `import_key` before creating/scheduling the AP case. Bind imported claims to their source text/evidence and document any translation into the AP extraction schema.
3. Treat canonical company/person IDs as evidence candidates. Do not bypass the local vendor register, trusted intake, identity ambiguity, PO approval, receipt allocation, currency/account/IFSC or duplicate-debt checks.
4. Run the existing `kanaku_pulla` payment pipeline. Retain its debt key and atomic reservation model. Never turn Core's `PAID_SANDBOX` result into a second independent payout request.
5. In a merged installation, disable Core's corporate payment authorizations. One workflow owns payment execution. Core can continue owning academic team-only draft actions in its separate sink.
6. Store upstream hold/review findings with the AP case. Do not relabel them as verified facts.

A persistent import key only prevents duplicate case creation; it does not replace payment-side idempotency. For async imports, use a transactional import/outbox record and a receiver-enforced key, just as for payment actions.

## Names, IDs and migrations

Public schemas are in `diligence_core/models.py`; evidence and action metadata are JSON. Entity aliases retain their supporting evidence ID. Institution domains are separate from supplier domains.

Keep the two schemas separate first. If consolidating later, prefix tables or migrate deliberately; do not paste the proposed single `action_log` table over either application's schema. That would lose receipts, reservations, evidence and recovery information.

The standalone authorization register uses a unique invoice-specific `obligation_id`, not a reusable PO budget. Preserve the AP app's more complete PO/receipt allocation implementation. Document any adapter mapping and test renamed vendors, changed payment details, interrupted imports, and overlapping receipt allocation before enabling integrated payment execution.
