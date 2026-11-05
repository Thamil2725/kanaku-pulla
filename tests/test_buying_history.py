import json

from fastapi.testclient import TestClient

from kanaku_pulla import config, pipeline


def test_dashboard_buying_history_uses_paid_ledger_not_claims_or_duplicates(env):
    from kanaku_pulla.web import app, buying_history
    client = TestClient(app)
    assert "No completed purchases yet" in client.get("/").text
    raw = (config.ROOT / "fixtures" / "01_razorpay_invoice.txt").read_text(encoding="utf-8")
    cid = pipeline.create_case(env.conn, raw, source_name="test-buying-history")
    pipeline.run(env.conn, cid)
    alias = (config.ROOT / "fixtures" / "03_resent_under_alias.txt").read_text(encoding="utf-8")
    pipeline.run(env.conn, pipeline.create_case(env.conn, alias, source_name="test-resend"))
    row = env.conn.execute("SELECT * FROM cases WHERE id=?", (cid,)).fetchone()
    state = json.loads(row["state"])
    state["fields"]["amount"] = "9999999.00"
    state["fields"]["vendor_name"] = "<script>untrusted()</script>"
    with env.conn:
        env.conn.execute("UPDATE cases SET state=? WHERE id=?", (json.dumps(state), cid))
    history = buying_history(env.conn)
    assert history.count("<tr>") == 2
    assert "Razorpay Software Private Limited" in history and f'/cases/{cid}' in history
    assert "1,18,000.00" in history and "9999999" not in history and "untrusted()" not in history
    assert "PO-2026-118" in history
    page = client.get("/").text
    assert 'id="h-buying"' in page and "Full ledger" in page
    with env.conn:
        env.conn.execute("UPDATE payments SET status='reserved'")
    assert "No completed purchases yet" in buying_history(env.conn)
