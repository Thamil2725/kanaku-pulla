"""Pure adapter: does not import the AP app, create an AP case, or move money."""
from .models import Document, digest


def ap_handoff(case: dict) -> dict:
    if "extraction" not in case["state"]:
        raise ValueError("No extracted invoice is available")
    doc = Document.model_validate(case["state"]["extraction"]["document"])
    if doc.kind != "INVOICE":
        raise ValueError("Academic BOMs are not payable invoices")
    return {
        "schema_version": "1.0",
        "core_case_id": case["id"],
        "source_document_hash": case["input_hash"],
        "import_key": "core-import:" + digest(case["input_hash"]),
        "document": doc.model_dump(),
        "original_text": case["raw"],
        "evidence": list(case["state"].get("evidence", {}).values()),
        "findings": case["state"].get("findings", []),
        "core_status": case["status"],
        "sandbox_effect_already_executed": bool(case["state"].get("action_result")),
        "payment_authorization": False,
        "submitter_authenticated": False,
        "required_downstream_checks": ["trusted intake", "vendor register", "PO approval", "receipt allocation",
                                       "amount/currency/account/IFSC", "debt identity and idempotency"],
    }
