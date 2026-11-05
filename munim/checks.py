"""The checks: who the vendor really is, whether the sender is who they claim, and the payment policy.

The model never decides whether money moves. These rules do, so the same case
gets the same answer however the email is worded. The model only reads the
email and explains the decision.
"""

import hashlib
import json
import re

from . import config, db
from .crustdata import CrustdataError

ROLE_WORDS = {"accounts", "account", "billing", "finance", "team", "payments", "support", "ar", "ap",
              "receivables", "collections", "noreply", "no-reply", "admin", "info"}
FINANCE_WORDS = ("finance", "account", "billing", "receivable", "payable", "controller", "cfo",
                 "treasury", "collection", "revenue", "fp&a", "tax", "audit")
TWO_LEVEL_SUFFIXES = {"co.in", "org.in", "net.in", "firm.in", "gen.in", "ind.in", "co.uk", "org.uk",
                      "com.au", "com.sg", "co.jp", "com.br"}


# --- small helpers -------------------------------------------------------------

def load_register():
    return json.loads(config.VENDOR_REGISTER.read_text(encoding="utf-8"))


def email_domain(email):
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower().rstrip(".")


def split_domain(domain):
    """'billing.razorpay-pay.co.in' -> ('razorpay-pay', 'co.in')."""
    parts = domain.lower().split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_LEVEL_SUFFIXES:
        return parts[-3], ".".join(parts[-2:])
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return domain, ""


def skeleton(label):
    """Collapse characters people confuse at a glance, so razorpaay / rnazorpay / raz0rpay line up."""
    s = label.lower().replace("rn", "m").replace("vv", "w").replace("-", "").replace("_", "")
    s = s.translate(str.maketrans({"0": "o", "1": "l", "i": "l", "3": "e", "5": "s"}))
    return re.sub(r"(.)\1+", r"\1", s)  # razorpaay -> razorpay


def levenshtein(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def is_lookalike(candidate, genuine):
    """True when candidate imitates genuine without being it."""
    if not candidate or not genuine or candidate == genuine:
        return False
    if candidate.endswith("." + genuine):
        return False  # a real subdomain, e.g. billing.razorpay.com
    c_label, c_suffix = split_domain(candidate)
    g_label, g_suffix = split_domain(genuine)
    if c_label == g_label and c_suffix != g_suffix:
        return True  # razorpay.co vs razorpay.com
    if skeleton(c_label) == skeleton(g_label):
        return True
    if g_label in c_label and len(g_label) >= 5:
        return True  # razorpay-billing, razorpaypayments
    return levenshtein(c_label, g_label) <= 2 and len(g_label) >= 5


def normalize_name(name):
    name = (name or "").lower()
    name = re.sub(r"\b(private|pvt|limited|ltd|llp|inc|corp|corporation|co|company|the)\b\.?", " ", name)
    return re.sub(r"[^a-z0-9]+", " ", name).strip()


def normalize_invoice_number(number):
    """'RZP/2026/0042' and 'RZP-2026-42' are the same invoice."""
    groups = [g for g in re.split(r"[^A-Za-z0-9]+", (number or "").upper()) if g]
    return "-".join(str(int(g)) if g.isdigit() else g for g in groups)


def parse_amount(value):
    if value is None:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(value))
    try:
        return round(float(cleaned), 2) if cleaned else None
    except ValueError:
        return None


def payment_key(vendor_id, invoice_number):
    """One key per real-world debt. Amount and account are NOT part of it, so a resend with
    changed details collides with the original instead of looking like a new invoice."""
    raw = f"{vendor_id}|{normalize_invoice_number(invoice_number)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _company_brief(c):
    info = c.get("basic_info") or {}
    return {
        "crustdata_company_id": c.get("crustdata_company_id"),
        "name": info.get("name"),
        "primary_domain": info.get("primary_domain"),
        "all_domains": info.get("all_domains"),
        "confidence": c.get("confidence"),
        "profile_url": info.get("professional_network_url"),
    }


# --- 1. who is the vendor? -------------------------------------------------------

def resolve_vendor(conn, case_id, cd, fields):
    """Work out which company this invoice is really from, and whether we already know it."""
    register = load_register()
    sender_domain = email_domain(fields.get("sender_email"))
    vendor_name = fields.get("vendor_name")
    out = {"sender_domain": sender_domain, "vendor": None, "company": None,
           "domain_status": "unknown", "name_candidates": [], "evidence": [], "notes": []}

    # a) What does Crustdata say the sending domain belongs to?
    domain_company = None
    if sender_domain:
        try:
            matches, meta = cd.identify_domain(sender_domain)
            if matches:
                domain_company = _company_brief(matches[0])
                out["evidence"].append(db.add_evidence(
                    conn, case_id, "fact", "crustdata:/company/identify",
                    f"Sending domain {sender_domain} belongs to {domain_company['name']} "
                    f"(Crustdata company {domain_company['crustdata_company_id']}, primary domain "
                    f"{domain_company['primary_domain']})",
                    data=domain_company, url=domain_company.get("profile_url"),
                    retrieved_at=meta["retrieved_at"]))
            else:
                out["evidence"].append(db.add_evidence(
                    conn, case_id, "fact", "crustdata:/company/identify",
                    f"Crustdata has no company for the sending domain {sender_domain}",
                    data={"domain": sender_domain, "matches": 0}, retrieved_at=meta["retrieved_at"]))
        except CrustdataError as exc:
            out["notes"].append(f"Company lookup for {sender_domain} failed: {exc}")
            out["evidence"].append(db.add_evidence(
                conn, case_id, "unavailable", "crustdata:/company/identify",
                f"Could not look up {sender_domain}: {exc}", data={"error": str(exc)}))

    # b) What does the name on the invoice match? Fuzzy, so ties mean ambiguity.
    if vendor_name:
        try:
            matches, meta = cd.identify_name(vendor_name)
            out["name_candidates"] = [_company_brief(m) for m in matches[:5]]
            if matches:
                top = out["name_candidates"][0]["confidence"] or 0
                tied = [c for c in out["name_candidates"] if (c["confidence"] or 0) >= top - 0.05]
                names = ", ".join(f"{c['name']} ({c['primary_domain']}, {c['confidence']})" for c in tied)
                out["evidence"].append(db.add_evidence(
                    conn, case_id, "fact", "crustdata:/company/identify",
                    f"The invoice name '{vendor_name}' matches {len(tied)} compan"
                    f"{'y' if len(tied) == 1 else 'ies'} at the top confidence: {names}",
                    data=out["name_candidates"], retrieved_at=meta["retrieved_at"]))
                out["name_ambiguous"] = len(tied) > 1
        except CrustdataError as exc:
            out["notes"].append(f"Name lookup failed: {exc}")

    # c) Which register entry is it? Stable IDs first, then domain, then names.
    vendor = None
    if domain_company:
        vendor = next((v for v in register
                       if v.get("crustdata_company_id") == domain_company["crustdata_company_id"]), None)
    if not vendor and sender_domain:
        vendor = next((v for v in register if v.get("registered_domain") == sender_domain), None)
    name_vendor = None
    if vendor_name:
        wanted = normalize_name(vendor_name)
        name_vendor = next((v for v in register
                            if wanted and wanted in {normalize_name(n) for n in [v["legal_name"], *v.get("aliases", [])]}),
                           None)

    # d) How does the sending domain relate to the company it claims to be?
    claimed = vendor or name_vendor
    if claimed:
        genuine = claimed["registered_domain"]
        if sender_domain == genuine or (sender_domain or "").endswith("." + genuine):
            out["domain_status"] = "registered"
        elif is_lookalike(sender_domain, genuine):
            out["domain_status"] = "lookalike"
        elif domain_company and domain_company["crustdata_company_id"] == claimed.get("crustdata_company_id"):
            # Crustdata lists the domain for this company, but it is not the one on file.
            # Alternate-domain lists can be noisy (Razorpay's includes bitly.com), so hold.
            out["domain_status"] = "listed_alternate"
        else:
            out["domain_status"] = "unrelated"
        out["evidence"].append(db.add_evidence(
            conn, case_id, "fact", "register",
            f"Our vendor register has {claimed['legal_name']} ({claimed['vendor_id']}) with registered "
            f"domain {genuine} and Crustdata company {claimed.get('crustdata_company_id')}",
            data={k: claimed[k] for k in ("vendor_id", "legal_name", "registered_domain", "crustdata_company_id")}))
    else:
        # Not a vendor we know. Still check it is not imitating one we do know.
        for v in register:
            if is_lookalike(sender_domain, v["registered_domain"]):
                out["domain_status"] = "lookalike"
                claimed = v
                break

    if vendor:
        out["vendor"] = vendor
        out["match_basis"] = "crustdata company id" if domain_company else "registered domain"
        if name_vendor and name_vendor["vendor_id"] == vendor["vendor_id"] and vendor_name:
            out["evidence"].append(db.add_evidence(
                conn, case_id, "inference", "munim",
                f"The invoice name '{vendor_name}' and the sending domain {sender_domain} point to the same "
                f"vendor record ({vendor['vendor_id']}), so they are treated as one company"))
    elif name_vendor and out["domain_status"] != "registered":
        out["claimed_vendor"] = name_vendor  # claims to be a known vendor, but the domain does not back it
    out["claimed"] = claimed
    out["company"] = domain_company
    return out


# --- 2. is the sender who they say they are? ------------------------------------

def verify_sender(conn, case_id, cd, fields, resolution):
    sender_name = (fields.get("sender_name") or "").strip()
    sender_email = (fields.get("sender_email") or "").strip().lower()
    claimed = resolution.get("claimed")
    out = {"status": "unverifiable", "evidence": [], "title": None, "finance_role": None}

    if claimed and sender_email in [c.lower() for c in claimed.get("approved_contacts", [])]:
        out["approved_contact"] = True
        out["evidence"].append(db.add_evidence(
            conn, case_id, "fact", "register",
            f"{sender_email} is an approved billing contact for {claimed['legal_name']} in our register"))

    words = {w.lower() for w in re.split(r"[^A-Za-z-]+", sender_name) if w}
    if not sender_name or words & ROLE_WORDS:
        out["status"] = "role_mailbox"
        out["evidence"].append(db.add_evidence(
            conn, case_id, "inference", "munim",
            f"Sender '{sender_name or sender_email}' is a shared mailbox, not a person, so employment "
            f"cannot be checked"))
        return out

    company_id = (claimed or {}).get("crustdata_company_id") or (resolution.get("company") or {}).get("crustdata_company_id")
    company_name = (claimed or {}).get("legal_name") or (resolution.get("company") or {}).get("name")
    if not company_id:
        out["evidence"].append(db.add_evidence(
            conn, case_id, "inference", "munim",
            f"No company could be resolved, so {sender_name}'s employment could not be checked"))
        return out

    try:
        profiles, meta = cd.find_person_at_company(sender_name, company_id)
    except CrustdataError as exc:
        out["status"] = "error"
        out["evidence"].append(db.add_evidence(
            conn, case_id, "unavailable", "crustdata:/person/search",
            f"Could not check {sender_name}'s employment: {exc}", data={"error": str(exc)}))
        return out

    best = None
    for p in profiles:
        emp = (p.get("experience") or {}).get("employment_details") or {}
        cur = [e for e in emp.get("current") or [] if e.get("crustdata_company_id") == company_id]
        past = [e for e in emp.get("past") or [] if e.get("crustdata_company_id") == company_id]
        name = (p.get("basic_profile") or {}).get("name")
        if cur:
            best = ("current", p, cur[0], name, emp)
            break
        if past and not best:
            latest = max(past, key=lambda e: e.get("end_date") or "")
            best = ("left", p, latest, name, emp)

    if not best:
        out["status"] = "not_found"
        out["evidence"].append(db.add_evidence(
            conn, case_id, "fact", "crustdata:/person/search",
            f"No public profile found for {sender_name} at {company_name} (Crustdata company {company_id})",
            data={"name": sender_name, "company_id": company_id, "profiles": 0},
            retrieved_at=meta["retrieved_at"]))
        return out

    status, profile, role, name, emp = best
    title = role.get("title") or ""
    out.update(status=status, title=title, person_id=profile.get("crustdata_person_id"),
               finance_role=any(w in title.lower() for w in FINANCE_WORDS))
    if status == "current":
        summary = (f"{name} currently works at {company_name} as '{title}' "
                   f"(since {(role.get('start_date') or '')[:7] or 'unknown'})")
    else:
        now_at = "; ".join(f"{e.get('title')} at {e.get('name')}" for e in emp.get("current") or []) or "unknown"
        summary = (f"{name} left {company_name} in {(role.get('end_date') or '')[:7] or 'an unknown month'} "
                   f"(last role '{title}'); their profile now shows: {now_at}")
        out["left_on"] = role.get("end_date")
        out["now_at"] = now_at
    out["evidence"].append(db.add_evidence(
        conn, case_id, "fact", "crustdata:/person/search", summary,
        data={"crustdata_person_id": profile.get("crustdata_person_id"), "role": role},
        retrieved_at=meta["retrieved_at"]))
    return out


# --- 3. the vendor's public profile (context for new or unknown vendors) ------------

def company_profile(conn, case_id, cd, company_id):
    try:
        company, meta = cd.enrich_company(company_id)
    except CrustdataError as exc:
        return {"evidence": [db.add_evidence(conn, case_id, "unavailable", "crustdata:/company/enrich",
                                             f"Could not enrich company {company_id}: {exc}")]}
    if not company:
        return {"evidence": []}
    info = company.get("basic_info") or {}
    headcount = (company.get("headcount") or {}).get("total")
    summary = (f"{info.get('name')}: founded {info.get('year_founded') or 'unknown'}, "
               f"{headcount if headcount is not None else 'unknown'} employees, "
               f"type {info.get('company_type') or 'unknown'}")
    ev = db.add_evidence(conn, case_id, "fact", "crustdata:/company/enrich", summary,
                         data={"year_founded": info.get("year_founded"), "headcount": headcount,
                               "company_type": info.get("company_type")},
                         url=info.get("professional_network_url"), retrieved_at=meta["retrieved_at"])
    return {"year_founded": info.get("year_founded"), "headcount": headcount, "evidence": [ev]}


# --- 4. the policy ----------------------------------------------------------------

