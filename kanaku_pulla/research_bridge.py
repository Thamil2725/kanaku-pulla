"""Small optional bridge to the independently maintained diligence_core package."""
import json
import os
from pathlib import Path


def research_reader(system, raw, _schema):
    from . import llm
    from diligence_core.models import AcademicExtraction
    return llm.structured(system + " This intake is for academic procurement only. Set kind to ACADEMIC_BOM "
                          "and invoice to null. Do not invent payment fields or placeholder values.", raw, AcademicExtraction)


def attach_research(app, shell, config):
    try:
        from diligence_core.host_ui import mount_research
        from diligence_core.resources import FIXTURES
    except ImportError:
        @app.get("/research")
        def research_unavailable():
            return shell("Lab procurement", '<div class="page-head"><p class="eyebrow">Research desk</p><h1>Lab procurement</h1></div><section class="panel"><div class="panel-body"><p>The research extension is not installed in this environment.</p><p>Install the project requirements with <code>python -m pip install -r requirements.txt</code>, then restart the server.</p></div></section>', active="research")
        return
    mode = os.getenv("KANAKU_RESEARCH_MODE", "demo")
    registry_path = os.getenv("KANAKU_RESEARCH_REGISTRY")
    if registry_path:
        registry = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    elif mode == "demo":
        path = FIXTURES / "registered_locations.json"
        registry = json.loads(path.read_text(encoding="utf-8"))
    else:
        registry = {}
    mount_research(app, shell, data_dir=Path(os.getenv("KANAKU_RESEARCH_DATA", config.ROOT / "research_data")),
                   team=config.TEAM_EMAILS, mode=mode, registry=registry,
                   structured_reader=research_reader, crustdata_key=config.crustdata_key)
