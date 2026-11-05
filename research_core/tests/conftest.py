import json
from pathlib import Path

import pytest

from diligence_core.engine import Engine


ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "diligence_core" / "fixtures"


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.delenv("CORE_CRASH_AT", raising=False)
    monkeypatch.delenv("CORE_LLM_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return Engine(tmp_path, mode="demo",
        authorizations=json.loads((FIXTURES / "authorizations.json").read_text()),
        registry=json.loads((FIXTURES / "registered_locations.json").read_text()),
        team=["ap-team@team.local", "research-team@team.local"])


def submit(engine, filename="corporate_invoice.txt", transform=lambda raw: raw):
    text = transform((FIXTURES / filename).read_text(encoding="utf-8"))
    return engine.run(engine.store.ingest(text, filename))
