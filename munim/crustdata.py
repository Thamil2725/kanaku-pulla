"""Thin Crustdata client: retries, a response cache, and credit accounting.

Every response is cached in SQLite with the time it was retrieved. That keeps
demo reruns deterministic and cheap, and gives each fact a retrieval date the
audit trail can show.
"""

import hashlib
import json
import time
from datetime import datetime, timezone

import httpx

from . import config, db

BASE_URL = "https://api.crustdata.com"
API_VERSION = "2025-11-01"


class CrustdataError(Exception):
    """A Crustdata call failed in a way a retry will not fix (or retries ran out)."""


class Crustdata:
    def __init__(self, conn, api_key=None, use_cache=True, max_attempts=4):
        self.conn = conn
        self.api_key = api_key if api_key is not None else config.crustdata_key()
        self.use_cache = use_cache
        self.max_attempts = max_attempts
        self.credits_used = 0.0

    def post(self, endpoint: str, body: dict):
        """Return (data, meta). meta has retrieved_at, cached and credits."""
        key = hashlib.sha256((endpoint + json.dumps(body, sort_keys=True)).encode()).hexdigest()
        if self.use_cache:
            row = self.conn.execute("SELECT * FROM api_cache WHERE key = ?", (key,)).fetchone()
            if row and _age_hours(row["retrieved_at"]) <= config.CACHE_MAX_AGE_HOURS:
                return json.loads(row["response"]), {
                    "retrieved_at": row["retrieved_at"], "cached": True, "credits": 0.0,
                }
        if not self.api_key:
            raise CrustdataError("No Crustdata API key configured (set CRUSTDATA_API_KEY)")

        headers = {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
            "x-api-version": API_VERSION,
        }
        last_error = "unknown error"
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = httpx.post(BASE_URL + endpoint, json=body, headers=headers, timeout=45)
            except httpx.HTTPError as exc:  # timeouts, connection resets, DNS
                last_error = f"network error: {exc.__class__.__name__}"
                time.sleep(min(2 ** attempt, 8))
                continue

            if resp.status_code == 429:
                reset = resp.headers.get("x-ratelimit-reset", "5")
                last_error = "rate limited (429)"
                time.sleep(min(float(reset) if reset.replace(".", "", 1).isdigit() else 5, 10))
                continue
            if resp.status_code >= 500:
                last_error = f"server error {resp.status_code}"
                time.sleep(min(2 ** attempt, 8))
                continue
            if resp.status_code in (401, 402, 403):
                hint = {401: "key rejected", 402: "out of credits", 403: "endpoint not allowed for this key"}
                raise CrustdataError(f"{resp.status_code} {hint[resp.status_code]}: {resp.text[:200]}")
            if resp.status_code >= 400:
                raise CrustdataError(f"{resp.status_code}: {resp.text[:300]}")

            try:
                data = resp.json()
            except ValueError:
                last_error = "response was not JSON"
                time.sleep(1)
                continue

            credits = float(resp.headers.get("x-credits-used") or 0)
            self.credits_used += credits
            retrieved_at = db.now()
            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO api_cache (key, endpoint, request, response, credits, retrieved_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (key, endpoint, json.dumps(body), json.dumps(data), credits, retrieved_at),
                )
            return data, {"retrieved_at": retrieved_at, "cached": False, "credits": credits}

        raise CrustdataError(f"gave up after {self.max_attempts} attempts: {last_error}")

    # --- the calls Munim makes -------------------------------------------------

    def identify_domain(self, domain: str):
        """Free. Returns (matches, meta); matches is a list of company_data dicts with confidence."""
        data, meta = self.post("/company/identify", {"domains": [domain], "exact_match": True})
        return _flatten_identify(data), meta

    def identify_name(self, name: str):
        """Free. Name matching is fuzzy, so callers must treat several close matches as ambiguous."""
        data, meta = self.post("/company/identify", {"names": [name]})
        return _flatten_identify(data), meta

    def enrich_company(self, company_id: int, fields=("basic_info", "headcount")):
        data, meta = self.post(
            "/company/enrich", {"crustdata_company_ids": [company_id], "fields": list(fields)}
        )
        matches = _flatten_identify(data)
        return (matches[0] if matches else None), meta

    def find_person_at_company(self, name: str, company_id: int):
        """People with this name who work or worked at the company."""
        body = {
            "filters": {
                "op": "and",
                "conditions": [
                    {"field": "basic_profile.name", "type": "(.)", "value": name},
                    {"field": "experience.employment_details.company_id", "type": "=", "value": company_id},
                ],
            },
            "limit": 5,
            "fields": ["basic_profile", "experience.employment_details"],
        }
        data, meta = self.post("/person/search", body)
        return data.get("profiles") or [], meta


def _age_hours(iso_ts):
    try:
        then = datetime.fromisoformat(iso_ts)
    except (TypeError, ValueError):
        return float("inf")  # unreadable timestamp: treat as stale
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


def _flatten_identify(data):
    """Identify and enrich both return [{matched_on, matches: [{confidence_score, company_data}]}]."""
    out = []
    if not isinstance(data, list):
        return out
    for entry in data:
        for match in (entry or {}).get("matches") or []:
            company = match.get("company_data") or {}
            if company.get("crustdata_company_id") is None:
                continue  # malformed record: skip rather than trust it
            out.append({"confidence": match.get("confidence_score"), **company})
    return out
