import json
import os
from html import escape

from fastapi import APIRouter, FastAPI, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .engine import Engine
from .extraction import NeedsReview, read_document
from .integration import ap_handoff
from .store import Conflict


class TextInput(BaseModel):
    text: str = Field(min_length=1, max_length=120000)
    source: str = Field(default="pasted-text", max_length=200)


def create_router(engine: Engine):
    router = APIRouter()

    def run_text(text, source):
        cid = engine.store.ingest(text, source)
        if engine.store.can_retry_extraction(engine.store.get(cid)):
            try:
                engine.prepare_extraction_retry(cid)
            except Conflict:
                pass
        return engine.run(cid)

    @router.post("/cases/text")
    def create_text(body: TextInput):
        return run_text(body.text, body.source)

    @router.post("/cases/upload")
    async def upload(file: UploadFile):
        data = await file.read(8 * 1024 * 1024 + 1)
        try:
            raw = await run_in_threadpool(read_document, data, file.filename or "input.txt")
        except NeedsReview as exc:
            raise HTTPException(422, str(exc)) from exc
        return await run_in_threadpool(run_text, raw, file.filename or "uploaded-document")

    @router.get("/cases")
    def cases():
        with engine.store.connect() as c:
            return [dict(r) for r in c.execute("SELECT id,status,step,source,updated_at FROM cases ORDER BY updated_at DESC")]

    @router.get("/cases/{cid}")
    def get_case(cid: str):
        try:
            return engine.store.audit(cid)
        except KeyError as exc:
            raise HTTPException(404, "Case not found") from exc

    @router.get("/cases/{cid}/ap-handoff")
    def handoff(cid: str):
        try:
            return ap_handoff(engine.store.audit(cid))
        except KeyError as exc:
            raise HTTPException(404, "Case not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/cases/{cid}/audit", response_class=HTMLResponse)
    def audit(cid: str):
        case = get_case(cid)
        state = case["state"]
        def source(e):
            value = e["source"] or "unknown"
            return f"<a href='{escape(value,quote=True)}' target='_blank' rel='noopener'>{escape(value)}</a>" if value.startswith("https://") else escape(value)
        rows = "".join(f"<tr id='{escape(e['id'])}'><td>{escape(e['id'])}</td><td>{escape(e['category'])}</td><td>{escape(e['summary'])}</td>"
                       f"<td>{source(e)}<br>Retrieved: {escape(e['retrieved_at'])}<br>Observed: {escape(e.get('observed_at') or 'unknown')}</td></tr>"
                       for e in state.get("evidence", {}).values())
        return f"<!doctype html><meta charset='utf-8'><title>Core audit</title><style>body{{font:16px system-ui;margin:3rem;max-width:1100px}}td,th{{padding:12px;border:1px solid #ccc;vertical-align:top}}table{{border-collapse:collapse}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}</style><h1>{escape(cid)}: {escape(case['status'])}</h1><p>Mode: {escape(state.get('mode','unknown'))}. Demo evidence is synthetic. Profile matching does not authenticate a sender.</p><h2>Evidence</h2><table><tr><th>ID</th><th>Type</th><th>Observation</th><th>Source and dates</th></tr>{rows}</table><h2>Decision, coordinates and actions</h2><pre>{escape(json.dumps(case,indent=2,ensure_ascii=False))}</pre>"

    @router.post("/resume")
    def resume():
        return engine.resume()

    @router.get("/sandbox-effects")
    def effects():
        return engine.sink.list()

    return router


def create_app(engine=None):
    engine = engine or Engine(os.getenv("CORE_DATA_DIR", "runtime"))
    app = FastAPI(title="Munim Core: corporate and academic diligence", version="0.1.0")
    app.include_router(create_router(engine), prefix="/core")

    @app.get("/", response_class=HTMLResponse)
    def home():
        return """<!doctype html><meta charset="utf-8"><title>Munim Core</title>
<style>body{font:17px system-ui;max-width:900px;margin:4rem auto;padding:1rem}textarea{width:100%;min-height:240px}button{padding:12px;margin:12px 0}pre{white-space:pre-wrap}a{color:#075b9c}</style>
<h1>One diligence engine. Two explicit outcomes.</h1><p>Corporate invoices: authorized sandbox payments. Academic BOMs: team-only research request drafts. Coordinates flag address discrepancies, not fraud.</p>
<p><a href="/docs">API documentation</a> · <a href="/core/cases">Cases</a> · <a href="/core/sandbox-effects">Sandbox ledger</a></p>
<h2>Paste labelled text</h2><textarea id="text" aria-label="Document text"></textarea><br><button id="submit">Process text</button>
<h2>Upload a text PDF or UTF-8 file</h2><input id="file" type="file" accept=".pdf,.txt,.md" aria-label="Document file"><button id="upload">Process file</button><p id="link"></p><pre id="result"></pre>
<script>async function show(r){const d=await r.json();document.getElementById('result').textContent=JSON.stringify(d,null,2);const l=document.getElementById('link');l.textContent='';if(d.id){const a=document.createElement('a');a.href='/core/cases/'+encodeURIComponent(d.id)+'/audit';a.textContent='Open evidence audit';l.appendChild(a)}}document.getElementById('submit').onclick=async()=>show(await fetch('/core/cases/text',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:document.getElementById('text').value})}));document.getElementById('upload').onclick=async()=>{const f=document.getElementById('file').files[0];if(!f)return;const b=new FormData();b.append('file',f);show(await fetch('/core/cases/upload',{method:'POST',body:b}))};</script>"""
    return app
