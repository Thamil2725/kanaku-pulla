import io
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from diligence_core.api import create_app
from diligence_core.extraction import NeedsReview, extract, labelled, leaves, read_document, validate_quotes
from diligence_core.geo import compare_location, distance_m
from diligence_core.models import Coordinates, minor_units
from diligence_core.providers import LiveProviders, Transport, Unavailable
from .conftest import FIXTURES, submit


def test_geodesic_validated_ranges_and_known_distance():
    assert distance_m(Coordinates(lat=0, lon=0), Coordinates(lat=0, lon=1)) == pytest.approx(111319.49, abs=.1)
    with pytest.raises(ValidationError):
        Coordinates(lat=91, lon=0)
    with pytest.raises(ValidationError):
        Coordinates(lat=float("nan"), lon=0)


def test_approximate_coordinates_do_not_become_discrepancy(engine):
    location = engine.providers.data["geocodes"]["Flat 4, Example Apartments, Mumbai, India"]
    location["precision"] = "APPROXIMATE"
    assert compare_location(location, engine.registry["1001"])["status"] == "unverified"


@pytest.mark.parametrize("amount", ["0", "-1", "NaN", "inf", "12.345", "1x2", "1e3"])
def test_invalid_money(amount):
    with pytest.raises(ValueError):
        minor_units(amount)


def transport_for(engine, handler):
    ticks = [datetime.now(timezone.utc).timestamp()]
    sleep = lambda n: ticks.__setitem__(0, ticks[0] + n)
    return Transport(engine.store, httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleep, clock=lambda: ticks[0])


def test_429_retry_and_cache_keep_original_retrieval_time(engine):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "2"}) if len(calls) == 1 else httpx.Response(200, json={"ok": True})
    transport = transport_for(engine, handler)
    _, first = transport.request("POST", "https://provider.example.org/test", payload={"a": 1})
    _, cached = transport.request("POST", "https://provider.example.org/test", payload={"a": 1})
    assert len(calls) == 2
    assert cached["cached"] and cached["retrieved_at"] == first["retrieved_at"]


def test_exhausted_timeout_is_bounded(engine):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("test")
    with pytest.raises(Unavailable):
        transport_for(engine, handler).request("POST", "https://provider.example.org/timeout")
    assert len(calls) == 3


def test_long_retry_after_does_not_hammer_provider(engine):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})
    with pytest.raises(Unavailable, match="longer retry"):
        transport_for(engine, handler).request("POST", "https://provider.example.org/rate-limit")
    assert len(calls) == 1


def test_person_request_and_profile_envelope(engine, monkeypatch):
    monkeypatch.setenv("CRUSTDATA_API_KEY", "test-secret-not-real")
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"profiles": [], "total_count": 0})
    provider = LiveProviders(transport_for(engine, handler))
    profiles, meta = provider.people("Alex Example")
    payload = json.loads(calls[0].content)
    assert isinstance(payload["filters"], dict)
    assert payload["filters"]["field"] == "basic_profile.name"
    assert calls[0].headers["x-api-version"] == "2025-11-01"
    assert profiles == []
    assert "test-secret" not in json.dumps(meta)


def test_malformed_company_response_becomes_unavailable(engine, monkeypatch):
    monkeypatch.setenv("CRUSTDATA_API_KEY", "test")
    provider = LiveProviders(transport_for(engine, lambda r: httpx.Response(200, json=[1])))
    with pytest.raises(Unavailable):
        provider.company("example.org")


def test_live_geocoder_parses_coordinates_without_property_classification(engine, monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-secret")
    data = {"status": "OK", "results": [{"geometry": {"location": {"lat": 26, "lng": 80}, "location_type": "ROOFTOP"}, "types": ["subpremise"]}]}
    provider = LiveProviders(transport_for(engine, lambda r: httpx.Response(200, json=data)))
    result, meta = provider.geocode("Apartment 4")
    assert result["coordinates"] == {"lat": 26, "lon": 80}
    assert result["property_use"] == "unknown"
    assert "test-secret" not in json.dumps(meta)


def pdf_bytes(text=None):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    if text:
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        lines = ["BT /F1 10 Tf 30 760 Td"]
        for line in text.splitlines():
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            lines += [f"({escaped}) Tj", "0 -15 Td"]
        stream.set_data(("\n".join(lines) + "\nET").encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO(); writer.write(output)
    return output.getvalue()


def test_pdf_upload_extracts_and_completes(engine):
    raw = (FIXTURES / "academic_bom.txt").read_text()
    client = TestClient(create_app(engine))
    response = client.post("/core/cases/upload", files={"file": ("bom.pdf", pdf_bytes(raw), "application/pdf")})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "DRAFTED_FOR_TEAM"
    assert "[Page 1]" in response.json()["raw"]


def test_blank_and_malformed_pdf_fail_explicitly(engine):
    for content in [pdf_bytes(), b"not a PDF"]:
        with pytest.raises(NeedsReview):
            read_document(content, "scan.pdf")
    client = TestClient(create_app(engine))
    assert client.post("/core/cases/upload", files={"file": ("scan.pdf", pdf_bytes())}).status_code == 422


def test_missing_unstructured_extractor_becomes_hold(engine):
    case = engine.run(engine.store.ingest("Unstructured claim without parseable fields", "raw.txt"))
    assert case["status"] == "HOLD"
    assert not engine.sink.list()


def test_conflicting_labelled_totals_hold(engine):
    case = submit(engine, transform=lambda s: s + "\nTotal: 999.00\n")
    assert case["status"] == "HOLD"


def test_quote_checker_rejects_invented_and_partial_numeric_values():
    raw = (FIXTURES / "corporate_invoice.txt").read_text()
    result = labelled(raw)
    result.quotes = {path: raw for path, _ in leaves(result.document.model_dump())}
    result.document.invoice.amount = "5000.00"
    with pytest.raises(NeedsReview):
        validate_quotes(result, raw)
