"""Opt-in live reader smoke test: fictional input, synthetic verification, local team draft."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kanaku_pulla import config
from kanaku_pulla.research_bridge import research_reader
from diligence_core.engine import Engine

raw = """Research materials request for demo-paper:reader-smoke-002.
We need General measurement kit, catalogue DEMO-42 and Analysis code and calibration dataset.
The supplier is Example Instruments, website instruments.example.org, at 12 Research Park, Kanpur, India.
The corresponding author is Mira Example, institution website old-university.example.org.
This is a fictional test document, not an invoice or a purchase authorization.
"""

if __name__ == "__main__":
    configured = config.llm_providers()
    print("Configured readers:", ", ".join(configured) or "none", flush=True)
    if not configured:
        raise SystemExit("Configure a reader first; no credentials are printed by this script.")
    engine = Engine(ROOT / "research_data" / "reader-smoke", mode="demo", authorizations=[],
                    team=config.TEAM_EMAILS, structured_reader=research_reader)
    cid = engine.store.ingest(raw, "fictional-reader-smoke.txt")
    if engine.store.can_retry_extraction(engine.store.get(cid)):
        engine.prepare_extraction_retry(cid)
    case = engine.run(cid)
    print(json.dumps({"id": cid, "status": case["status"], "method": case["state"].get("extraction", {}).get("method"),
                      "error": case["error"], "effects": len(engine.sink.list())}, indent=2))
    if case["status"] != "DRAFTED_FOR_TEAM":
        raise SystemExit(1)
