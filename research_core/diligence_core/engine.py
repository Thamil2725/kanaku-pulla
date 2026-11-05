import json
import os
from pathlib import Path

from .actions import Sandbox
from .extraction import NeedsReview, extract
from .geo import compare_location
from .models import Document, digest, invoice_identity, minor_units, now
from .providers import DemoProviders, LiveProviders, Transport, Unavailable
from .resources import FIXTURES
from .store import Conflict, Store


STEPS = ("extract", "identify", "geocode", "decide", "act", "done")


class Engine:
    def __init__(self, data_dir, providers=None, authorizations=None, registry=None, team=None, mode=None, structured_reader=None, crustdata_key=None):
        root = Path(data_dir)
        self.store = Store(root / "core.db")
        self.structured_reader = structured_reader
        self.team = tuple(team or [x.strip().lower() for x in os.getenv("CORE_TEAM_EMAILS", "ap-team@team.local,research-team@team.local").split(",") if x.strip()])
        if not self.team or any("@" not in x for x in self.team):
            raise ValueError("Configure at least one team email")
        self.sink = Sandbox(root / "sandbox_effects.db", self.team)
        self.mode = mode or os.getenv("CORE_MODE", "demo")
        if self.mode not in ("demo", "live"):
            raise ValueError("CORE_MODE must be demo or live")
        self.providers = providers or (DemoProviders(FIXTURES / "provider_data.json") if self.mode == "demo" else LiveProviders(Transport(self.store), crustdata_key=crustdata_key))
        self.authorizations = authorizations if authorizations is not None else self._load(os.getenv("CORE_AUTHORIZATIONS"), [])
        self.registry = registry if registry is not None else self._load(os.getenv("CORE_REGISTRY"), {})
        # Synthetic trust files are loaded only by the explicit demo CLI/configuration.

    @staticmethod
    def _load(path, fallback):
        return json.loads(Path(path).read_text(encoding="utf-8")) if path else fallback

    def evidence(self, state, category, subject, summary, data, meta):
        eid = "E-" + digest([category, subject, data, meta.get("source")])[:14]
        state.setdefault("evidence", {})[eid] = {"id": eid, "category": category, "subject": subject,
            "summary": summary, "data": data, "source": meta.get("source"),
            "retrieved_at": meta.get("retrieved_at", now()), "observed_at": meta.get("observed_at"),
            "synthetic": meta.get("synthetic", False), "request": meta.get("request"), "cached": meta.get("cached", False)}
        return eid

    def finding(self, state, code, severity, text, ids):
        state.setdefault("findings", []).append({"code": code, "severity": severity, "text": text, "evidence_ids": ids})

    def _company(self, state, domain, alias):
        if not domain:
            eid = self.evidence(state, "document_claim", alias or "Unspecified institution",
                "No company or institution website was supplied; identity could not be checked", {},
                {"source": "input:document"})
            return {"status": "unverified", "evidence_id": eid, "domain": None}
        try:
            matches, meta = self.providers.company(domain)
            eid = self.evidence(state, "source_observation", domain, "Company lookup returned candidate records, not a legal-registry or sender-authentication verdict", matches, meta)
            unique = {str(m["company_data"].get("crustdata_company_id")): m for m in matches
                      if m["company_data"].get("crustdata_company_id") is not None and isinstance(m.get("confidence_score"), (int, float)) and m["confidence_score"] >= .95}
            if len(unique) != 1 or len(matches) != 1:
                return {"status": "unverified", "evidence_id": eid, "domain": domain}
            pid, match = next(iter(unique.items()))
            record = match["company_data"]
            key = self.store.entity("company", pid, record, alias, eid)
            self.store.entity("company", pid, record, domain, eid)
            return {"status": "resolved", "company_id": pid, "entity_id": key,
                    "evidence_id": eid, "domain": domain, "profile": record}
        except (Unavailable, KeyError, TypeError, ValueError) as exc:
            eid = self.evidence(state, "unavailable", domain, str(exc), {}, {"source": "provider:company"})
            return {"status": "unverified", "evidence_id": eid, "domain": domain}

    def identify(self, state):
        doc = Document.model_validate(state["extraction"]["document"])
        state["suppliers"] = [self._company(state, supplier.domain, supplier.name) for supplier in doc.suppliers]
        for supplier in state["suppliers"]:
            if supplier["status"] != "resolved":
                self.finding(state, "supplier_unverified", "hold", "Supplier identity was not uniquely resolved; absence from a dataset is not proof of fraud", [supplier["evidence_id"]])
        if not doc.contact:
            state["contact"] = {"status": "unverified"}
            self.finding(state, "contact_missing", "review", "No contact identity supplied; submitter authentication is not established", [])
            return
        institution = self._company(state, doc.contact.institution_domain, doc.contact.institution_domain)
        state["contact_institution"] = institution
        try:
            profiles, meta = self.providers.people(doc.contact.name)
            eid = self.evidence(state, "source_observation", doc.contact.name,
                "Public profiles returned by a name query; not proof that this person submitted the document", profiles, meta)
            candidates = []
            for p in profiles:
                if not p.get("crustdata_person_id"):
                    continue
                name = (p.get("basic_profile") or {}).get("name", "")
                if " ".join(name.casefold().split()) != " ".join(doc.contact.name.casefold().split()):
                    continue
                if doc.contact.profile_url:
                    url = ((p.get("social_handles") or {}).get("professional_network_identifier") or {}).get("profile_url")
                    if (url or "").rstrip("/").casefold() != doc.contact.profile_url.rstrip("/").casefold():
                        continue
                emp = (p.get("experience") or {}).get("employment_details") or {}
                current, past = emp.get("current") or [], emp.get("past") or []
                target = institution.get("company_id")
                same = lambda r: target is not None and str(r.get("crustdata_company_id")) == target
                status = "listed_current" if any(same(r) for r in current) else "listed_past" if any(same(r) for r in past) else "unverified"
                if status != "unverified":
                    candidates.append((p, status))
            if len(candidates) != 1 or meta.get("truncated"):
                state["contact"] = {"status": "unverified", "evidence_id": eid, "authenticated": False}
                self.finding(state, "contact_unverified", "review", "No unique profile match to the claimed institution; do not infer non-employment", [eid])
            else:
                p, status = candidates[0]
                key = self.store.entity("person", p["crustdata_person_id"], p, doc.contact.name, eid)
                state["contact"] = {"status": status, "entity_id": key, "profile": p, "evidence_id": eid, "authenticated": False}
                if status == "listed_past":
                    self.finding(state, "affiliation_changed", "review", "Profile lists the claimed institution as past employment; review the contact route. This does not prove fraud", [eid])
        except (Unavailable, TypeError, KeyError, ValueError) as exc:
            eid = self.evidence(state, "unavailable", doc.contact.name, str(exc), {}, {"source": "provider:people"})
            state["contact"] = {"status": "unverified", "evidence_id": eid, "authenticated": False}
            self.finding(state, "contact_unavailable", "review", "Contact profile could not be checked", [eid])

    def geocode(self, state):
        doc = Document.model_validate(state["extraction"]["document"])
        state["locations"] = []
        for claim, company in zip(doc.suppliers, state["suppliers"]):
            if not claim.address:
                self.finding(state, "address_missing", "review", "No supplier address supplied", [])
                continue
            try:
                location, meta = self.providers.geocode(claim.address)
                eid = self.evidence(state, "source_observation", claim.domain,
                    "Geocoded claimed address; property use, ownership and business legitimacy are not established", location, meta)
                trusted = self.registry.get(company.get("company_id", ""))
                comparison = compare_location(location, trusted)
                state["locations"].append({"supplier": claim.domain, "geocode": location, "comparison": comparison, "evidence_id": eid})
                if comparison["status"] == "discrepancy":
                    ref = self.evidence(state, "trusted_reference", claim.domain, "Operator-supplied registered location", trusted,
                        {"source": trusted["source"], "observed_at": trusted["observed_at"], "synthetic": self.mode == "demo"})
                    self.finding(state, "address_discrepancy", "hold", f"Precise geocode is {comparison['distance_m']} m from the trusted reference; confirm which operating location applies", [eid, ref])
                elif comparison["status"] == "unverified":
                    self.finding(state, "location_unverified", "review", comparison["reason"], [eid])
                if "po box" in claim.address.casefold() or "p.o. box" in claim.address.casefold():
                    self.finding(state, "mailing_address", "review", "A mailing address does not establish an operating site; request supporting evidence if needed", [eid])
            except (Unavailable, KeyError, TypeError, ValueError) as exc:
                eid = self.evidence(state, "unavailable", claim.domain, str(exc), {}, {"source": "provider:geocode"})
                self.finding(state, "geocode_unavailable", "review", "Address could not be located; no fraud inference made", [eid])

    def decide(self, state):
        doc = Document.model_validate(state["extraction"]["document"])
        if doc.kind == "ACADEMIC_BOM":
            if doc.invoice is not None:
                raise NeedsReview("Academic BOM must not contain a payment instruction")
            team = self.team[-1]
            key = "draft:" + digest([doc.reference.strip().casefold(), "materials-and-code-request", team])
            # Stable action content excludes dynamic timestamps/provider search ordering.
            payload = {"to": team, "subject": f"Review research materials request: {doc.reference}",
                       "paper_reference": doc.reference, "contact_claim": doc.contact.model_dump() if doc.contact else None,
                       "items": doc.items,
                       "review_reasons": sorted(state.get("findings", []), key=lambda f: (f["code"], f["text"])),
                       "profile_lists_current_affiliations": ((state.get("contact", {}).get("profile", {}).get("experience") or {}).get("employment_details") or {}).get("current", []),
                       "body": "Team-only draft: verify the corresponding author's contact route, reagent provenance and code/data availability before any external outreach. Profile-listed affiliations are leads, not authenticated addresses. No purchase is authorized."}
            state.update(decision="DRAFT_FOR_TEAM_REVIEW", action_kind="team_draft", action_key=key, action_payload=payload)
            return
        if not doc.invoice or len(doc.suppliers) != 1:
            raise NeedsReview("Corporate invoice requires exactly one supplier and complete invoice details")
        inv = doc.invoice
        supplier = state["suppliers"][0]
        wanted = {"company_id": supplier.get("company_id"), "invoice_number": invoice_identity(inv.number),
                  "amount_minor": minor_units(inv.amount), "currency": inv.currency, "account": inv.account,
                  "ifsc": inv.ifsc, "po_number": inv.po_number}
        grants = [g for g in self.authorizations if all(str(g.get(k)) == str(v) for k, v in wanted.items())
                  and g.get("approved") is True and g.get("receipt_confirmed") is True and g.get("obligation_id")
                  and g.get("source") and g.get("approved_at")]
        if inv.currency != "INR" or len(grants) != 1 or any(f["severity"] in {"hold", "review"} for f in state.get("findings", [])):
            self.finding(state, "payment_not_authorized", "hold", "Payment needs a unique trusted invoice-specific PO/receipt authorization in INR and no unresolved blocking checks", [])
            state["decision"] = "HOLD"
            return
        grant = grants[0]
        eid = self.evidence(state, "trusted_authorization", inv.number,
            "Operator supplied approval and receipt for this exact obligation; payment authority does not come from a profile", grant,
            {"source": grant["source"], "observed_at": grant["approved_at"], "synthetic": self.mode == "demo"})
        state.update(decision="SANDBOX_PAY", action_kind="sandbox_payment",
                     action_key="pay:" + digest([supplier["company_id"], invoice_identity(inv.number)]),
                     action_payload=wanted, obligation_id=grant["obligation_id"], authorization_evidence=eid)

    def act(self, cid, state):
        if state.get("decision") == "HOLD":
            return "HOLD"
        key = state["action_key"]
        row = self.store.reserve(cid, state["action_kind"], key, state["action_payload"], state.get("obligation_id"))
        if row["status"] == "complete":
            state["action_result"] = json.loads(row["result"])
        else:
            result = self.sink.execute(key, state["action_kind"], state["action_payload"])
            if os.getenv("CORE_CRASH_AT") == "after_effect":
                os._exit(137)
            self.store.complete(key, result)
            state["action_result"] = result
        return "PAID_SANDBOX" if state["action_kind"] == "sandbox_payment" else "DRAFTED_FOR_TEAM"

    def run(self, cid):
        while True:
            case = self.store.get(cid)
            step, state = case["step"], case["state"]
            if state.get("mode", self.mode) != self.mode:
                raise ValueError("Resume using the original demo/live mode; do not mix simulated and live evidence")
            state["mode"] = self.mode
            if step == "done":
                return self.store.audit(cid)
            try:
                if step == "extract":
                    state["extraction"] = extract(case["raw"], structured_reader=self.structured_reader).model_dump()
                    self.evidence(state, "document_claim", case["source"], "Extracted claims are input assertions, not verified external facts",
                                  state["extraction"], {"source": "input:" + case["source"]})
                elif step == "act":
                    status = self.act(cid, state)
                    self.store.save(cid, "done", state, status)
                    self.store.event(cid, status)
                    continue
                else:
                    getattr(self, step)(state)
                self.store.save(cid, STEPS[STEPS.index(step) + 1], state)
                self.store.event(cid, f"Completed {step}")
            except (NeedsReview, Conflict, ValueError) as exc:
                self.store.save(cid, "done", state, "HOLD", str(exc))
                self.store.event(cid, f"Held: {exc}")
                return self.store.audit(cid)
            except Exception as exc:
                # Do not include provider exception bodies/URLs that could contain credentials.
                self.store.save(cid, step, state, "ERROR", f"{type(exc).__name__}: inspect local logs/configuration")
                return self.store.audit(cid)

    def resume(self):
        with self.store.connect() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM cases WHERE step!='done'")]
        return [self.run(cid) for cid in ids]

    def prepare_extraction_retry(self, cid):
        """Reopen only a failed read that has never progressed to evidence or actions."""
        return self.store.reopen_extraction(cid, self.mode)
