"""Optional host-shell integration. All research state remains in Core's own DB."""
import json
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .api import create_router
from .engine import Engine
from .extraction import NeedsReview, read_document
from .resources import FIXTURES
from .store import Conflict


LABELS = {"DRAFTED_FOR_TEAM": ("Draft ready", "ok"), "HOLD": ("Needs review", "hold"),
          "ERROR": ("Stopped", "bad"), "RUNNING": ("Processing", "na"), "PAID_SANDBOX": ("Sandbox effect", "note")}


def mount_research(app, shell, *, data_dir, team, mode="demo", registry=None, structured_reader=None, crustdata_key=None):
    engine = Engine(data_dir, team=team, mode=mode, registry=registry, authorizations=[], structured_reader=structured_reader, crustdata_key=crustdata_key)
    app.state.research_engine = engine
    app.include_router(create_router(engine), prefix="/core")
    router = APIRouter()
    fixtures = FIXTURES
    samples = {"academic_bom.txt": ("Author changed institution", "Separate the author from the supplier and prepare a team request."),
               "academic_address_discrepancy.txt": ("Supplier address differs", "Compare coordinates, show the distance, and ask for supporting evidence.")}

    def chip(status):
        label, style = LABELS.get(status, (status, "na"))
        return f'<span class="chip {style}">{escape(label)}</span>'

    def render(title, body, error=""):
        return shell(title, body, active="research", error=error)

    def banner():
        return ('<div class="notice hold"><b>Sample data.</b> The people, companies and coordinates in this research sandbox are fictional. Drafts stay in the team inbox.</div>'
                if mode == "demo" else '<div class="notice note"><b>Source checks.</b> Profile and location matches do not authenticate a sender or authorize a purchase. Requests remain team-only drafts.</div>')

    @router.get("/research")
    def research_home(error: str = ""):
        with engine.store.connect() as c:
            cases = list(c.execute("SELECT id,status,state,created_at FROM cases ORDER BY created_at DESC"))
        rows = []
        for case in cases:
            state = json.loads(case["state"])
            doc = state.get("extraction", {}).get("document", {})
            if doc.get("kind", "ACADEMIC_BOM") != "ACADEMIC_BOM":
                continue
            rows.append(f'<tr><td><a href="/research/cases/{escape(case["id"])}">{escape(doc.get("reference") or case["id"])}</a></td><td>{chip(case["status"])}</td><td>{escape((doc.get("contact") or {}).get("name", "Not yet extracted"))}</td><td>{len(state.get("findings", []))}</td></tr>')
        sample_buttons = "".join(f'<li><button class="sample" name="sample" value="{name}"><b>{title}</b><span class="d">{detail}</span></button></li>' for name, (title, detail) in samples.items()) if mode == "demo" else '<p>Upload a document to start a sourced review.</p>'
        body = f'''<div class="page-head"><p class="eyebrow">Research desk</p><h1>Lab procurement</h1><p class="lede">Check suppliers, trace the author’s listed affiliation, and prepare a materials or code request for your team.</p></div>
{banner()}
<div class="cols even"><section class="panel"><div class="panel-head"><h2>Review a materials list</h2></div>
<form class="panel-body" method="post" action="/research/cases" enctype="multipart/form-data" data-busy="Starting review…">
<div class="field"><label for="research-text">Paper reference, suppliers and materials</label><textarea id="research-text" name="raw" placeholder="Paste the paper title, supplier names and websites, and the materials you need. Or upload a text PDF."></textarea></div>
<div class="field"><label for="research-file">Text PDF or UTF-8 document</label><input id="research-file" type="file" name="file" accept=".pdf,.txt,.md"></div>
<p class="hint">Uses the same document reader as your invoices. Include supplier websites; missing facts stay unverified. <a href="/research/template">Download the no-key text template</a>. Scanned PDFs need a readable transcription.</p><div><button class="btn">Review document</button></div></form></section>
<section class="panel"><div class="panel-head"><h2>Explore the checks</h2></div><form class="panel-body" method="post" action="/research/cases" data-busy="Starting review…"><ul class="samples">{sample_buttons}</ul></form></section></div>
<section class="panel"><div class="panel-head"><h2>Research cases</h2><form method="post" action="/research/resume"><button class="btn btn-line">Resume interrupted reviews</button></form></div>
<div class="table-wrap"><table><caption class="sr-only">Research documents and their review status</caption><thead><tr><th>Reference</th><th>Outcome</th><th>Contact claim</th><th>Review notes</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan="4">No research documents yet. Start with a sample or upload.</td></tr>'}</tbody></table></div></section>'''
        return render("Lab procurement", body, error)

    @router.get("/research/template")
    def research_template():
        return PlainTextResponse("Document Type: ACADEMIC_BOM\nReference: Your paper title or DOI\n"
            "Supplier: Supplier name | supplier.example.org | Street, city, country\n"
            "Contact Name: Name from the paper\nContact Institution Domain: university.example.org\n"
            "Item: Requested material and quantity\n",
            headers={"Content-Disposition": 'attachment; filename="research-materials-template.txt"'})

    @router.post("/research/cases")
    async def research_submit(background_tasks: BackgroundTasks, raw: str = Form(""), sample: str = Form(""), file: UploadFile | None = None):
        source = "pasted-research-document"
        try:
            if sample:
                if mode != "demo" or sample not in samples:
                    raise NeedsReview("Unknown sample")
                raw, source = (fixtures / sample).read_text(encoding="utf-8"), sample
            elif file is not None and file.filename:
                raw = await run_in_threadpool(read_document, await file.read(8 * 1024 * 1024 + 1), file.filename)
                source = file.filename
            if not raw.strip() or len(raw) > 120000:
                raise NeedsReview("Provide a non-empty document within the supported size")
        except NeedsReview as exc:
            return RedirectResponse("/research?error=" + quote(str(exc)), status_code=303)
        cid = engine.store.ingest(raw, source)
        if engine.store.can_retry_extraction(engine.store.get(cid)):
            try:
                engine.prepare_extraction_retry(cid)
            except Conflict:
                pass  # Another request has already reopened it; keep the same case.
        background_tasks.add_task(engine.run, cid)
        return RedirectResponse(f"/research/cases/{cid}", status_code=303)

    @router.get("/research/cases/{cid}")
    def research_case(cid: str, tab: str = "decision"):
        try:
            case = engine.store.audit(cid)
        except KeyError as exc:
            raise HTTPException(404, "Research case not found") from exc
        state = case["state"]
        doc = state.get("extraction", {}).get("document", {})
        tabs = {"decision": "Review", "evidence": "Evidence", "location": "Location", "draft": "Team draft", "activity": "Activity"}
        if tab not in tabs:
            raise HTTPException(404, "Unknown case tab")
        nav = '<nav class="tabs" aria-label="Research case">' + "".join(f'<a href="/research/cases/{cid}?tab={key}"' + (' aria-current="page"' if tab == key else '') + f'>{label}</a>' for key, label in tabs.items()) + '</nav>'
        if tab == "decision":
            notes = "".join(f'<li><b>{escape(f["text"])}</b> ' + "".join(f'<a class="evref" href="/research/cases/{cid}?tab=evidence#{escape(e)}">{escape(e)}</a>' for e in f["evidence_ids"]) + '</li>' for f in state.get("findings", []))
            suppliers = "".join(f'<li>{escape(s["name"])} <span class="hint">{escape(s.get("domain") or "Website not supplied")}</span></li>' for s in doc.get("suppliers", []))
            contact = state.get("contact", {})
            affiliations = ((contact.get("profile", {}).get("experience") or {}).get("employment_details") or {}).get("current", [])
            listed = "; ".join(str(r.get("name") or r.get("crustdata_company_id") or "unknown") for r in affiliations) or "Not established"
            body = f'<div class="cols even"><section class="panel"><div class="panel-head"><h2>Suppliers</h2></div><div class="panel-body"><ul>{suppliers or "<li>Not yet resolved</li>"}</ul></div></section><section class="panel"><div class="panel-head"><h2>Author or contact</h2></div><div class="panel-body"><p>{escape((doc.get("contact") or {}).get("name", "Not supplied"))}</p><p>Profile-listed current affiliation: {escape(listed)}</p><p class="hint">This is a source claim, not proof of who uploaded the document. A move does not imply supplier fraud.</p></div></section></div><section class="panel"><div class="panel-head"><h2>What needs review</h2></div><div class="panel-body"><ul>{notes or "<li>No review findings recorded yet.</li>"}</ul></div></section>'
        elif tab == "evidence":
            entries = []
            for e in state.get("evidence", {}).values():
                source = e["source"] or "Unknown source"
                source_html = (f'<a href="{escape(source, quote=True)}" target="_blank" rel="noopener noreferrer">{escape(source)}</a>'
                               if source.startswith("https://") else escape(source))
                entries.append(f'<li id="{escape(e["id"])}"><span class="id">{escape(e["id"])}</span><div><b>{escape(e["summary"])}</b><p class="meta">{source_html} · Retrieved {escape(e["retrieved_at"])} · Observed {escape(e.get("observed_at") or "unknown")}</p><details><summary>Source record and request</summary><pre>{escape(json.dumps(e,indent=2,ensure_ascii=False))}</pre></details></div></li>')
            body = '<section class="panel"><div class="panel-head"><h2>Evidence and provenance</h2></div><ul class="evidence">' + ''.join(entries) + '</ul></section>'
        elif tab == "location":
            cards = []
            for loc in state.get("locations", []):
                geo, comparison = loc["geocode"], loc["comparison"]
                cards.append(f'<section class="panel"><div class="panel-head"><h2>{escape(loc["supplier"])}</h2></div><div class="panel-body"><dl class="facts"><dt>Latitude / longitude</dt><dd>{escape(json.dumps(geo.get("coordinates", {})))}</dd><dt>Precision</dt><dd>{escape(geo.get("precision", "unknown"))}</dd><dt>Distance to reference</dt><dd>{comparison.get("distance_m", "Not established")} m</dd><dt>Comparison</dt><dd>{escape(comparison["status"])}</dd><dt>Business or property use</dt><dd>Not established by geocoding</dd></dl><p class="hint">Distance uses a WGS84 geodesic. A residential address, mailing address or changed location does not prove fraud.</p><details><summary>Coordinates, source and comparison details</summary><pre>{escape(json.dumps(loc,indent=2))}</pre></details></div></section>')
            body = ''.join(cards) or '<section class="panel"><p class="empty">No location result is available. See Evidence for unavailable checks.</p></section>'
        elif tab == "draft":
            drafts = [a for a in case["actions"] if a["kind"] == "team_draft"]
            cards = []
            for a in drafts:
                p = json.loads(a["payload"])
                cards.append(f'<section class="panel"><div class="panel-head"><h2>{escape(p["subject"])}</h2></div><div class="panel-body"><p><b>To our team:</b> {escape(p["to"])}</p><p>{escape(p["body"])}</p><p class="hint">Recorded in the local team outbox. No external message was sent.</p><details open><summary>Requested materials and review notes</summary><pre>{escape(json.dumps(p,indent=2,ensure_ascii=False))}</pre></details></div></section>')
            body = ''.join(cards) or '<section class="panel"><p class="empty">No team draft has completed yet.</p></section>'
        else:
            body = '<section class="panel"><div class="panel-head"><h2>Saved workflow events</h2></div><div class="panel-body"><ol>' + ''.join(f'<li>{escape(e["at"])} · {escape(e["message"])}</li>' for e in case["events"]) + '</ol></div></section>'
        retryable = engine.store.can_retry_extraction(case)
        retry = (f'<form method="post" action="/research/cases/{cid}/retry-extraction" data-busy="Reading document…"><p class="hint">The document was held before verification. You can retry with the configured reader or submit a corrected <a href="/research/template">text template</a>.</p><button class="btn btn-line">Retry document reading</button></form>' if retryable else '')
        progress = '<p role="status">Review in progress. <a href="">Refresh status</a></p><script>setTimeout(()=>location.reload(),1500)</script>' if case["status"] == "RUNNING" else ''
        header = f'<div class="page-head"><p class="eyebrow"><a href="/research">Lab procurement</a> / {escape(cid)}</p><h1>{escape(doc.get("reference") or "Research review")}</h1>{chip(case["status"])}</div>'
        error = case.get("error") or ""
        if retryable and "ANTHROPIC_API_KEY" in error:
            error = "This document was held by the previous reader. Retry it using the shared invoice reader."
        return render("Research review", header + banner() + progress + retry + nav + body, error)

    @router.post("/research/cases/{cid}/retry-extraction")
    def retry_extraction(cid: str, background_tasks: BackgroundTasks):
        try:
            engine.prepare_extraction_retry(cid)
        except KeyError as exc:
            raise HTTPException(404, "Research case not found") from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        background_tasks.add_task(engine.run, cid)
        return RedirectResponse(f"/research/cases/{cid}", status_code=303)

    @router.post("/research/resume")
    def research_resume(background_tasks: BackgroundTasks):
        background_tasks.add_task(engine.resume)
        return RedirectResponse("/research", status_code=303)

    app.include_router(router)
    return engine
