"""The decision memo.

Everything a reader might take as proven is rendered by code from the evidence
records themselves: verified facts are the exact evidence summaries, claims are
what the email says, reasoning is the policy's own findings. Claude may add a
short plain-language summary, but it is shown separately and labelled as
model-written. Its citations are checked for existence, which shows where to
look, not that the sentence is true.
"""

import json

from pydantic import BaseModel

from . import config

UNVERIFIED_CODES = {"sender_unverified", "ungrounded_amount", "ungrounded_invoice_number",
                    "ungrounded_sender_email", "ungrounded_bank_account_number", "ungrounded_bank_ifsc",
                    "ungrounded_currency", "amount_ambiguous", "amount_disagreement", "currency_conflict"}

HEADLINES = {
    "PAY": "Decision: pay. Every required check passed.",
    "DUPLICATE": "Decision: do not pay. This invoice is already in the ledger.",
    "HOLD": "Decision: hold. Something specific is missing.",
    "REJECTED": "Decision: reject. Suspected fraud or a changed invoice.",
}


class Point(BaseModel):
    text: str
    evidence: list[str]


class Summary(BaseModel):
    points: list[Point]


SYSTEM = """You summarise an accounts-payable decision for a busy finance lead, in 2 to 4 short points.
The decision was made by fixed policy rules; explain it, never change or soften it.
Use only the evidence provided and cite the evidence ids each point rests on, like ["E3"].
Evidence of kind "claim" is only what the email says; never present it as verified.
Evidence of kind "unavailable" means a lookup failed; say it could not be checked.
A public profile shows what the profile lists, not proof of current employment or of who sent an email."""


def citation_problems(points, valid_ids):
    problems = []
    for i, p in enumerate(points):
        ev = p["evidence"] if isinstance(p, dict) else p.evidence
        if not ev:
            problems.append(f"point {i} has no citation")
        bad = [e for e in ev if e not in valid_ids]
        if bad:
            problems.append(f"point {i} cites unknown evidence {bad}")
    return problems


def write_memo(decision, findings, evidence, invoice_summary):
    """Return (memo_dict, method)."""
    needs = [f["needs"]["evidence"] for f in findings if f.get("needs")]
    out = {
        "headline": HEADLINES[decision],
        "verified_facts": [{"text": e["summary"], "evidence": [e["id"]]} for e in evidence if e["kind"] == "fact"],
        "claims": [{"text": e["summary"], "evidence": [e["id"]]} for e in evidence if e["kind"] == "claim"],
        "inferences": [{"text": f["text"], "evidence": f["evidence"]} for f in findings
                       if f["code"] not in UNVERIFIED_CODES],
        "could_not_verify": [{"text": e["summary"], "evidence": [e["id"]]} for e in evidence
                             if e["kind"] == "unavailable"]
                            + [{"text": f["text"], "evidence": f["evidence"]} for f in findings
                               if f["code"] in UNVERIFIED_CODES],
        "next_step": {
            "PAY": "Reserve the invoice in the ledger, then pay through the payout API and tell the team.",
            "DUPLICATE": "No payment. The team is told which ledger entry already covers it.",
            "HOLD": ("To pay this I need: " + "; ".join(needs) + ". I have asked the team.") if needs
                    else "Waiting for a person.",
            "REJECTED": "No payment. Confirm with the vendor through the contact in our register, never through "
                        "details in this email.",
        }[decision],
        "model_summary": [],
    }
    method = "rules"
    if config.anthropic_available():
        valid = {e["id"] for e in evidence}
        try:
            points = _claude_summary(decision, findings, evidence, invoice_summary)
            if points and not citation_problems(points, valid):
                out["model_summary"] = [dict(p) for p in points]
                method = "rules + Claude summary"
        except Exception as exc:
            print(f"[memo] Claude summary skipped ({exc.__class__.__name__}: {exc})")
    return out, method


def _claude_summary(decision, findings, evidence, invoice_summary):
    import anthropic

    client = anthropic.Anthropic()
    valid = {e["id"] for e in evidence}
    payload = {
        "decision": decision,
        "invoice_as_claimed": invoice_summary,
        "policy_findings": [{k: f[k] for k in ("code", "text", "evidence")} for f in findings],
        "evidence": [{k: e[k] for k in ("id", "kind", "source", "summary", "retrieved_at")} for e in evidence],
    }
    messages = [{"role": "user", "content": json.dumps(payload, indent=1, ensure_ascii=False)}]
    for _ in range(2):
        response = client.messages.parse(
            model=config.MODEL, max_tokens=4000, system=SYSTEM, messages=messages, output_format=Summary)
        parsed = response.parsed_output
        if parsed is None:
            return []
        points = [p.model_dump() for p in parsed.points]
        problems = citation_problems(points, valid)
        if not problems:
            return points
        messages += [
            {"role": "assistant", "content": parsed.model_dump_json()},
            {"role": "user", "content": "Fix these citation problems and return all points again:\n" + "\n".join(problems)},
        ]
    return []
