import copy

import pytest

from diligence_core.extraction import NeedsReview, extract, labelled, leaves
from diligence_core.resources import FIXTURES


def test_reader_repairs_missing_quote_once_and_still_rejects_fabrication():
    raw = (FIXTURES / "academic_bom.txt").read_text()
    valid = labelled(raw)
    raw = raw.replace("Document Type: ACADEMIC_BOM", "Research request")
    valid.quotes = {path: raw for path, _ in leaves(valid.document.model_dump())}
    missing = copy.deepcopy(valid)
    missing.quotes.pop("items.0")
    calls = []
    def reader(system, text, schema):
        calls.append(system)
        assert text == raw
        return (missing if len(calls) == 1 else valid), "openrouter"
    assert extract(raw, reader).method == "openrouter_structured_with_quote_checks"
    assert len(calls) == 2 and "items.0" in calls[1]
    bad = copy.deepcopy(valid)
    bad.document.items[0] = "An invented material"
    calls.clear()
    def invalid_reader(*args):
        calls.append(1)
        return bad, "openrouter"
    with pytest.raises(NeedsReview):
        extract(raw, invalid_reader)
    assert len(calls) == 2
