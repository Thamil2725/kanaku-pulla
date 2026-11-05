import copy
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from diligence_core.api import create_app
from diligence_core.engine import Engine
from diligence_core.integration import ap_handoff
from diligence_core.store import Conflict
from .conftest import FIXTURES, ROOT, submit


def test_corporate_pays_and_alias_resend_reuses_effect(engine):
    first = submit(engine)
    assert first["status"] == "PAID_SANDBOX", first
    second = submit(engine, transform=lambda s: s.replace("Example Instruments Pvt Ltd", "Example Instruments"))
    assert second["status"] == "PAID_SANDBOX"
    assert len(engine.sink.list()) == 1
    assert submit(engine)["id"] == first["id"]
    with engine.store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM entities WHERE kind='company'").fetchone()[0] == 1


def test_no_authorization_cannot_pay(engine):
    engine.authorizations = []
    assert submit(engine)["status"] == "HOLD"
    assert not engine.sink.list()


@pytest.mark.parametrize("old,new", [("Currency: INR", "Currency: USD"),
    ("TEST0000123", "TEST0000999"), ("123456789012", "999999999999"), ("50000.00", "50001.00")])
def test_altered_payment_details_hold(engine, old, new):
    assert submit(engine, transform=lambda s: s.replace(old, new))["status"] == "HOLD"
    assert not engine.sink.list()


def test_changed_amount_same_identity_conflicts_even_with_second_grant(engine):
    assert submit(engine)["status"] == "PAID_SANDBOX"
    revised = copy.deepcopy(engine.authorizations[0]); revised["amount_minor"] = 6000000
    engine.authorizations.append(revised)
    case = submit(engine, transform=lambda s: s.replace("50000.00", "60000.00"))
    assert case["status"] == "HOLD"
    assert "changed" in case["error"]
    assert len(engine.sink.list()) == 1


def test_same_receipt_cannot_authorize_two_distinct_invoices(engine):
    second = copy.deepcopy(engine.authorizations[0]); second["invoice_number"] = "DEMO-INV-002"
    engine.authorizations.append(second)
    raw = (FIXTURES / "corporate_invoice.txt").read_text()
    ids = [engine.store.ingest(raw, "a"), engine.store.ingest(raw.replace("DEMO-INV-001", "DEMO-INV-002"), "b")]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(engine.run, ids))
    assert sorted(r["status"] for r in results) == ["HOLD", "PAID_SANDBOX"]
    assert len(engine.sink.list()) == 1


def test_identical_concurrent_action_is_exactly_once(engine):
    payload = {"to": "ap-team@team.local", "body": "review"}
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: engine.sink.execute("shared", "team_draft", payload), range(8)))
    assert len({r["reference"] for r in results}) == 1
    assert len(engine.sink.list()) == 1


def test_sink_rejects_external_recipient(engine):
    with pytest.raises(Conflict):
        engine.sink.execute("external", "team_draft", {"to": "author@external.org"})
    assert not engine.sink.list()


def test_academic_author_is_distinct_from_supplier(engine):
    case = submit(engine, "academic_bom.txt")
    assert case["status"] == "DRAFTED_FOR_TEAM"
    state = case["state"]
    assert state["suppliers"][0]["company_id"] == "1001"
    assert state["contact_institution"]["company_id"] == "2001"
    assert state["contact"]["status"] == "listed_past"
    assert state["contact"]["authenticated"] is False
    assert any(f["code"] == "affiliation_changed" for f in state["findings"])
    assert len(engine.sink.list()) == 1
    assert engine.sink.list()[0]["kind"] == "team_draft"
    assert submit(engine, "academic_bom.txt")["id"] == case["id"]
    assert len(engine.sink.list()) == 1


def test_address_discrepancy_is_not_shell_company_verdict(engine):
    case = submit(engine, "academic_address_discrepancy.txt")
    assert case["status"] == "DRAFTED_FOR_TEAM"
    assert any(f["code"] == "address_discrepancy" for f in case["state"]["findings"])
    geo = case["state"]["locations"][0]
    assert geo["comparison"]["distance_m"] > 1000000
    assert geo["geocode"]["property_use"] == "unknown"
    assert not any("shell" in f["code"] for f in case["state"]["findings"])


def test_unknown_domain_and_unknown_person_do_not_imply_fraud(engine):
    engine.providers.data["companies"].clear()
    engine.providers.data["people"].clear()
    case = submit(engine)
    assert case["status"] == "HOLD"
    assert case["state"]["contact"]["status"] == "unverified"
    assert not engine.sink.list()


def test_duplicate_person_profiles_remain_ambiguous(engine):
    p = copy.deepcopy(engine.providers.data["people"]["Alex Example"][0]); p["crustdata_person_id"] = 999
    engine.providers.data["people"]["Alex Example"].append(p)
    case = submit(engine)
    assert case["status"] == "HOLD"
    assert case["state"]["contact"]["status"] == "unverified"


def test_api_and_adapter_no_existing_app_mutations(engine):
    client = TestClient(create_app(engine))
    result = client.post("/core/cases/text", json={"text": (FIXTURES / "corporate_invoice.txt").read_text()})
    assert result.status_code == 200
    cid = result.json()["id"]
    for url in ["/", "/docs", "/core/cases", "/core/sandbox-effects", f"/core/cases/{cid}/audit"]:
        assert client.get(url).status_code == 200
    packet = client.get(f"/core/cases/{cid}/ap-handoff").json()
    assert packet["payment_authorization"] is False
    assert packet["sandbox_effect_already_executed"] is True
    assert "required_downstream_checks" in packet
    assert client.get("/core/cases/absent").status_code == 404
    with pytest.raises(ValueError):
        ap_handoff(submit(engine, "academic_bom.txt"))


@pytest.mark.parametrize("filename", ["corporate_invoice.txt", "academic_bom.txt"])
def test_actual_process_crash_after_sink_commit_then_resume(tmp_path, filename):
    env = os.environ.copy()
    env.update(CORE_CRASH_AT="after_effect", CORE_MODE="demo", PYTHONIOENCODING="utf-8")
    args = [sys.executable, "-m", "diligence_core.cli", "--data-dir", str(tmp_path), "--demo-trust"]
    result = subprocess.run(args + ["run", str(FIXTURES / filename)], cwd=ROOT, env=env, capture_output=True, timeout=30)
    assert result.returncode == 137, result.stderr.decode()
    engine = Engine(tmp_path, mode="demo")
    assert len(engine.sink.list()) == 1
    with engine.store.connect() as c:
        assert c.execute("SELECT status FROM actions").fetchone()[0] == "pending"
    env.pop("CORE_CRASH_AT")
    result = subprocess.run(args + ["resume"], cwd=ROOT, env=env, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode()
    result = subprocess.run(args + ["resume"], cwd=ROOT, env=env, capture_output=True, timeout=30)
    assert result.returncode == 0
    assert len(engine.sink.list()) == 1
    with engine.store.connect() as c:
        assert c.execute("SELECT status FROM actions").fetchone()[0] == "complete"
