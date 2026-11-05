"""Research must reuse the invoice reader without gaining invoice payment authority."""
import json

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from kanaku_pulla import config, llm
from kanaku_pulla.research_bridge import attach_research
from diligence_core.extraction import labelled, leaves
from diligence_core.resources import FIXTURES


def unstructured():
    raw = (FIXTURES / "academic_bom.txt").read_text(encoding="utf-8")
    result = labelled(raw)
    raw = raw.replace("Document Type: ACADEMIC_BOM\n", "Research materials request\n")
    result.quotes = {path: raw for path, _ in leaves(result.document.model_dump())}
    return raw, result


def host(tmp_path, monkeypatch, mode="demo"):
    monkeypatch.setenv("KANAKU_RESEARCH_DATA", str(tmp_path / "research"))
    monkeypatch.setenv("KANAKU_RESEARCH_MODE", mode)
    monkeypatch.delenv("KANAKU_RESEARCH_REGISTRY", raising=False)
    app = FastAPI()
    attach_research(app, lambda title, body, **kwargs: HTMLResponse(body + str(kwargs.get("error", ""))), config)
    return TestClient(app), app.state.research_engine


def test_research_reuses_openrouter_key_file_and_preserves_provenance(tmp_path, monkeypatch):
    raw, result = unstructured()
    key_file = tmp_path / "open-router-key.txt"
    key_file.write_text("OPENROUTER_API_KEY=test-research-token")
    monkeypatch.setattr(config, "OPENROUTER_KEY_FILE", key_file)
    monkeypatch.setattr(config, "NO_LLM", False)
    monkeypatch.setattr(config, "LLM_CHOICE", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CORE_LLM_MODEL", raising=False)
    calls = []
    def complete(url, **kwargs):
        calls.append(url)
        assert kwargs["headers"]["Authorization"] == "Bearer test-research-token"
        assert kwargs["json"]["model"] == config.OPENROUTER_MODEL
        assert kwargs["json"]["response_format"]["json_schema"]["name"] == "AcademicExtraction"
        assert "Invoice" not in kwargs["json"]["response_format"]["json_schema"]["schema"].get("$defs", {})
        return httpx.Response(200, json={"choices": [{"message": {"content": result.model_dump_json()}}]})
    monkeypatch.setattr(llm.httpx, "post", complete)
    client, engine = host(tmp_path, monkeypatch)
    r = client.post("/research/cases", data={"raw": raw})
    assert r.status_code == 200 and "Draft ready" in r.text
    case = engine.store.get(r.url.path.split("/")[-1])
    assert case["state"]["extraction"]["method"] == "openrouter_structured_with_quote_checks"
    assert len(engine.sink.list()) == 1 and not engine.authorizations
    assert calls == [llm.OPENROUTER_URL]
    assert "test-research-token" not in json.dumps(engine.store.audit(case["id"]))


def test_old_anthropic_hold_can_be_retried_but_completed_draft_cannot(tmp_path, monkeypatch):
    raw, result = unstructured()
    client, engine = host(tmp_path, monkeypatch)
    cid = engine.store.ingest(raw, "old-upload.pdf")
    engine.store.save(cid, "done", {"mode": "demo"}, "HOLD", "Unstructured text needs ANTHROPIC_API_KEY and CORE_LLM_MODEL")
    engine.store.event(cid, "Original reader unavailable")
    page = client.get(f"/research/cases/{cid}")
    assert "ANTHROPIC_API_KEY" not in page.text and "Retry document reading" in page.text
    engine.structured_reader = lambda *args: (result, "openrouter")
    retry = client.post(f"/research/cases/{cid}/retry-extraction")
    assert retry.status_code == 200 and "Draft ready" in retry.text
    assert any(e["message"] == "Original reader unavailable" for e in engine.store.audit(cid)["events"])
    assert client.post(f"/research/cases/{cid}/retry-extraction").status_code == 409
    again = client.post("/research/cases", data={"raw": raw})
    assert again.url.path.endswith(cid) and len(engine.sink.list()) == 1


def test_research_reupload_retries_reader_failure_and_never_reopens_payment_hold(tmp_path, monkeypatch):
    raw, result = unstructured()
    client, engine = host(tmp_path, monkeypatch)
    engine.structured_reader = lambda *args: (_ for _ in ()).throw(RuntimeError("model unavailable"))
    held = client.post("/core/cases/text", json={"text": raw}).json()
    assert held["status"] == "HOLD"
    engine.structured_reader = lambda *args: (result, "gemini")
    retried = client.post("/core/cases/text", json={"text": raw}).json()
    assert retried["id"] == held["id"] and retried["status"] == "DRAFTED_FOR_TEAM"
    invoice = client.post("/core/cases/text", json={"text": (FIXTURES / "corporate_invoice.txt").read_text()}).json()
    assert invoice["status"] == "HOLD"
    assert client.post(f'/research/cases/{invoice["id"]}/retry-extraction').status_code == 409


def test_missing_domain_stays_unverified_and_fabricated_quote_never_runs(tmp_path, monkeypatch):
    raw, result = unstructured()
    result.document.suppliers[0].domain = None
    client, engine = host(tmp_path, monkeypatch)
    engine.structured_reader = lambda *args: (result, "openrouter")
    response = client.post("/research/cases", data={"raw": raw})
    assert "Draft ready" in response.text and "Website not supplied" in response.text
    case = engine.store.get(response.url.path.split("/")[-1])
    assert case["state"]["suppliers"][0]["status"] == "unverified"
    result.document.reference = "An invented research title"
    bad = client.post("/core/cases/text", json={"text": raw + "\nSecond request"}).json()
    assert bad["status"] == "HOLD" and "quote" in bad["error"]
    assert len(engine.sink.list()) == 1


def test_live_research_reuses_crustdata_credentials_and_template_needs_no_model(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "crustdata_key", lambda: "test-crustdata-token")
    client, engine = host(tmp_path, monkeypatch, mode="live")
    assert engine.providers.crustdata_key() == "test-crustdata-token" and engine.registry == {}
    template = client.get("/research/template")
    assert template.status_code == 200 and "attachment" in template.headers["content-disposition"]
    assert labelled(template.text).document.kind == "ACADEMIC_BOM"
