"""Vendor context from Crustdata: the company profile, who holds finance roles there, and a news check.

Context only. None of this can approve or block a payment; the policy never reads it. Successful lookups
are recorded as evidence (with retrieval time); failed ones are listed as unavailable context.
"""

from . import db
from .crustdata import CrustdataError

# Each entry is an all-words match on the current title. Plain "accounts" would also match sales roles
# ("Key Accounts", "Strategic Accounts"), so only finance phrasings are used.
FINANCE_WORDS = ["finance", "accounts payable", "accounts receivable", "accountant", "billing", "controller",
                 "treasury", "collections", "chief financial"]
NEWS_TERMS = '(fraud OR scam OR breach OR insolvency OR penalty OR "bank account")'


def gather(conn, case_id, cd, company_id, name):
    out = {"company": None, "people": [], "news": [], "unavailable": [], "evidence": []}

    # 1. Company profile
    try:
        company, meta = cd.enrich_company(company_id, fields=("basic_info", "headcount", "funding"))
        if company:
            info, hc, fund = company.get("basic_info") or {}, company.get("headcount") or {}, company.get("funding") or {}
            growth = hc.get("growth_percent") or {}
            profile = {
                "name": info.get("name") or name, "year_founded": info.get("year_founded"),
                "company_type": info.get("company_type"), "employees": hc.get("total"),
                "growth_6m": growth.get("6m"), "growth_12m": growth.get("12m"),
                "funding_usd": fund.get("total_investment_usd"), "last_round": fund.get("last_round_type"),
                "last_round_date": fund.get("last_fundraise_date"), "domain": info.get("primary_domain"),
                "profile_url": info.get("professional_network_url"),
            }
            out["company"] = profile
            name = profile["name"]  # Crustdata's short name ("Razorpay") searches better than the legal name
            out["evidence"].append(db.add_evidence(
                conn, case_id, "fact", "crustdata:/company/enrich", _company_line(profile),
                data=profile, url=profile["profile_url"], retrieved_at=meta["retrieved_at"]))
    except CrustdataError as exc:
        out["unavailable"].append(f"Company profile: {exc}")

    # 2. People who list a current finance role there
    try:
        people, meta = cd.people_in_roles(company_id, FINANCE_WORDS, limit=6)
        for p in people:
            bp = p.get("basic_profile") or {}
            if bp.get("name"):
                out["people"].append({"name": bp.get("name"), "title": bp.get("current_title") or ""})
        line = (f"Public profiles listing a current finance role at {name}: "
                + "; ".join(f"{p['name']} ({p['title']})" for p in out["people"])) if out["people"] else \
            f"Crustdata returned no public profiles with a current finance role at {name}"
        out["evidence"].append(db.add_evidence(conn, case_id, "fact", "crustdata:/person/search", line,
                                               data=out["people"], retrieved_at=meta["retrieved_at"]))
    except CrustdataError as exc:
        out["unavailable"].append(f"Finance people: {exc}")

    # 3. News check
    try:
        results, meta = cd.news(f'"{name}" {NEWS_TERMS}')
        for r in results[:5]:
            out["news"].append({k: r.get(k) for k in ("title", "url", "snippet", "date", "source")})
        if out["news"]:
            line = (f"News search for {name} with fraud, breach or insolvency terms returned {len(out['news'])} "
                    f"result(s); top: \"{out['news'][0]['title']}\". These are search hits, not findings about "
                    f"this invoice")
        else:
            line = f"News search for {name} with fraud, breach or insolvency terms returned no results"
        out["evidence"].append(db.add_evidence(conn, case_id, "fact", "crustdata:/web/search/live", line,
                                               data=out["news"], url=(out["news"][0]["url"] if out["news"] else None),
                                               retrieved_at=meta["retrieved_at"]))
    except CrustdataError as exc:
        out["unavailable"].append(f"News check: {exc}")
    return out


def _company_line(p):
    parts = [f"Crustdata company profile for {p['name']}: founded {p['year_founded'] or 'year unknown'}"]
    if p["employees"] is not None:
        growth = f", {p['growth_6m']:+.0f}% in 6 months" if isinstance(p["growth_6m"], (int, float)) else ""
        parts.append(f"{p['employees']:,} employees on public profiles{growth}")
    if p["funding_usd"]:
        last = f" (last round {p['last_round']}" + (f", {str(p['last_round_date'])[:7]}" if p["last_round_date"] else "") + ")" \
            if p["last_round"] else ""
        parts.append(f"total funding ${p['funding_usd'] / 1e6:,.0f}M{last}")
    if p["company_type"]:
        parts.append(p["company_type"].lower())
    return ", ".join(parts)
