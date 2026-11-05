from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from diligence_core.host_ui import mount_research
from .conftest import FIXTURES


def host(engine, tmp_path):
    app = FastAPI()
    def shell(title, body, active="", error=""):
        return HTMLResponse(f"<html><title>{title}</title><aside>Existing AP navigation</aside><main>{error}{body}</main></html>")
    mounted = mount_research(app, shell, data_dir=tmp_path / "host", team=engine.team, mode="demo", registry=engine.registry)
    return TestClient(app), mounted


def test_research_uses_host_shell_and_completes_team_draft(engine, tmp_path):
    client, mounted = host(engine, tmp_path)
    page = client.get("/research")
    assert page.status_code == 200
    assert "Existing AP navigation" in page.text and "Sample data" in page.text
    response = client.post("/research/cases", data={"sample": "academic_address_discrepancy.txt"})
    assert response.status_code == 200
    assert "Draft ready" in response.text
    cid = client.get("/core/cases").json()[0]["id"]
    for tab in ["decision", "evidence", "location", "draft", "activity"]:
        page = client.get(f"/research/cases/{cid}?tab={tab}")
        assert page.status_code == 200
        assert "Existing AP navigation" in page.text
    assert "reference_coordinates" in client.get(f"/research/cases/{cid}?tab=location").text
    assert "No external message was sent" in client.get(f"/research/cases/{cid}?tab=draft").text
    assert [e["kind"] for e in mounted.sink.list()] == ["team_draft"]


def test_host_extension_cannot_pay_and_rejects_path_traversal(engine, tmp_path):
    client, mounted = host(engine, tmp_path)
    response = client.post("/core/cases/text", json={"text": (FIXTURES / "corporate_invoice.txt").read_text()})
    assert response.json()["status"] == "HOLD"
    assert not mounted.sink.list()
    assert client.post("/research/cases", data={"sample": "../authorizations.json"}).status_code == 200
    assert len(client.get("/core/cases").json()) == 1


def test_research_html_escapes_document_and_references(engine, tmp_path):
    client, _ = host(engine, tmp_path)
    text = (FIXTURES / "academic_bom.txt").read_text().replace("demo-paper:measurement-methods-001", "<script>alert(1)</script>")
    response = client.post("/research/cases", data={"raw": text})
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
