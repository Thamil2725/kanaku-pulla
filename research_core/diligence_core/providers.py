"""Documented API adapters. Public profile observations never authenticate a sender."""
import json
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from .models import Coordinates, digest, now


class Unavailable(Exception):
    pass


class Transport:
    def __init__(self, store, client=None, sleep=time.sleep, clock=time.time):
        self.store = store
        self.client = client or httpx.Client(timeout=20, follow_redirects=False)
        self.sleep, self.clock = sleep, clock

    def request(self, method, url, *, payload=None, params=None, headers=None, rpm=10, secret=""):
        # Secret participates in cache isolation but is never stored in provenance.
        scope = digest([url, secret])
        key = digest([method, url, payload, {k: v for k, v in (params or {}).items() if k != "key"}, scope])
        with self.store.connect() as c:
            row = c.execute("SELECT * FROM cache WHERE key=?", (key,)).fetchone()
        if row:
            age = self.clock() - datetime.fromisoformat(row["retrieved_at"]).timestamp()
            if 0 <= age <= 3600:
                return json.loads(row["value"]), {"source": url, "retrieved_at": row["retrieved_at"], "cached": True}
        for attempt in range(3):
            with self.store.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                row = c.execute("SELECT next_at FROM requests WHERE scope=?", (scope,)).fetchone()
                slot = max(self.clock(), row[0] if row else 0)
                if slot - self.clock() > 30:
                    raise Unavailable("Local API queue is full; retry the held case later")
                c.execute("INSERT INTO requests VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET next_at=excluded.next_at",
                          (scope, slot + 60 / rpm))
            self.sleep(max(0, slot - self.clock()))
            try:
                response = self.client.request(method, url, json=payload, params=params, headers=headers)
            except httpx.HTTPError:
                if attempt == 2:
                    raise Unavailable("Network request failed after three attempts")
                self.sleep(2 ** attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 2:
                    raise Unavailable(f"HTTP {response.status_code} after three attempts")
                wait = 2 ** attempt
                retry = response.headers.get("Retry-After")
                if retry:
                    try:
                        wait = max(wait, float(retry))
                    except ValueError:
                        try:
                            wait = max(wait, parsedate_to_datetime(retry).timestamp() - self.clock())
                        except (ValueError, TypeError):
                            pass
                if wait > 30:
                    raise Unavailable("Provider requested a longer retry delay; retry later")
                self.sleep(wait)
                continue
            if response.status_code != 200:
                raise Unavailable(f"Provider returned HTTP {response.status_code}")
            try:
                data = response.json()
            except ValueError as exc:
                raise Unavailable("Provider returned invalid JSON") from exc
            timestamp = now()
            with self.store.connect() as c:
                c.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, json.dumps(data), timestamp))
            return data, {"source": url, "retrieved_at": timestamp, "cached": False}
        raise Unavailable("Retry budget exhausted")


class LiveProviders:
    mode = "live"

    def __init__(self, transport, crustdata_key=None):
        self.transport = transport
        self.crustdata_key = crustdata_key or (lambda: os.getenv("CRUSTDATA_API_KEY", ""))

    def crust(self, endpoint, payload):
        key = self.crustdata_key()
        if not key:
            raise Unavailable("CRUSTDATA_API_KEY is not configured")
        data, meta = self.transport.request("POST", "https://api.crustdata.com" + endpoint,
            payload=payload, headers={"Authorization": f"Bearer {key}", "x-api-version": "2025-11-01"},
            rpm=30, secret=key)
        meta["request"] = payload
        return data, meta

    def company(self, domain):
        data, meta = self.crust("/company/identify", {"domains": [domain], "exact_match": True})
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise Unavailable("Unexpected company response envelope")
        matches = data[0].get("matches")
        if not isinstance(matches, list) or any(not isinstance(m, dict) or not isinstance(m.get("company_data"), dict) for m in matches):
            raise Unavailable("Malformed company matches")
        return matches, meta

    def people(self, name):
        # Name candidates first; compare current/past canonical company IDs locally.
        # Never use the old flat current_company_id/name filter or a `persons` result key.
        data, meta = self.crust("/person/search", {
            "filters": {"field": "basic_profile.name", "type": "=", "value": name},
            "fields": ["basic_profile", "experience.employment_details", "social_handles"], "limit": 5})
        if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
            raise Unavailable("Unexpected person response envelope")
        if any(not isinstance(p, dict) for p in data["profiles"]):
            raise Unavailable("Malformed person profile")
        meta["truncated"] = bool(data.get("next_cursor")) or (data.get("total_count") or 0) > len(data["profiles"])
        return data["profiles"], meta

    def geocode(self, address):
        key = os.getenv("GOOGLE_MAPS_API_KEY", "")
        if not key:
            raise Unavailable("GOOGLE_MAPS_API_KEY is not configured")
        data, meta = self.transport.request("GET", "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": address, "key": key}, secret=key, rpm=10)
        meta["request"] = {"address": address}
        if not isinstance(data, dict) or data.get("status") not in ("OK", "ZERO_RESULTS"):
            raise Unavailable("Geocoder did not return a usable result")
        results = data.get("results")
        if not isinstance(results, list):
            raise Unavailable("Malformed geocoding results")
        if len(results) != 1:
            return {"status": "unknown", "reason": "No unique geocoding result", "property_use": "unknown"}, meta
        try:
            result = results[0]
            point = result["geometry"]["location"]
            coords = Coordinates(lat=point["lat"], lon=point["lng"])
            return {"status": "located", "coordinates": coords.model_dump(),
                    "precision": result["geometry"]["location_type"],
                    "partial_match": result.get("partial_match", False),
                    "formatted_address": result.get("formatted_address"),
                    "types": result.get("types", []), "property_use": "unknown"}, meta
        except (KeyError, TypeError, ValueError) as exc:
            raise Unavailable("Malformed geocoder coordinates") from exc


class DemoProviders:
    mode = "demo"

    def __init__(self, fixture_path):
        self.path = Path(fixture_path)
        self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def _get(self, group, key, default):
        return self.data.get(group, {}).get(key, default), {
            "source": "demo://" + self.path.name + "/" + group,
            "retrieved_at": now(), "observed_at": self.data["observed_at"],
            "synthetic": True, "request": {"lookup": key}, "cached": False}

    def company(self, domain):
        return self._get("companies", domain, [])

    def people(self, name):
        return self._get("people", name, [])

    def geocode(self, address):
        return self._get("geocodes", address, {"status": "unknown", "property_use": "unknown", "reason": "No demo geocode"})
