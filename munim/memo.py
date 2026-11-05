"""The decision memo: every sentence cites evidence, and code checks the citations.

Claude writes the memo from the evidence and the decision. It cannot change
the decision. If any claim lacks a valid citation, Claude gets one retry with
the problem spelled out; after that a plain memo built from the findings is used.
"""

import json

from pydantic import BaseModel

from . import config


class Claim(BaseModel):
    text: str
    evidence: list[str]


class Memo(BaseModel):
    headline: str
    verified_facts: list[Claim]
    inferences: list[Claim]
    could_not_verify: list[Claim]
    next_step: str


SYSTEM = """You write the decision memo for an accounts-payable agent.
The decision has already been made by policy rules; explain it, do not change it.
Rules:
- Use only the evidence provided. Every claim must cite one or more evidence ids, like ["E3"].
- verified_facts: things a source states (kind = fact). Mention the source and its retrieval date when it matters.
- inferences: conclusions drawn from facts. Say what they rest on.
- could_not_verify: what was checked but could not be confirmed, including evidence of kind "unavailable"
  (a lookup that failed). Say so plainly; never guess.
- next_step: for a hold, name exactly the missing evidence listed in the findings' "needs", and who was asked.
- Name people and companies exactly as the evidence does. Keep each claim to one sentence.
- headline: one sentence with the decision and the main reason. next_step: what happens now."""


def validate(memo: Memo, valid_ids: set[str]) -> list[str]:
    problems = []
    for section in ("verified_facts", "inferences", "could_not_verify"):
        for i, claim in enumerate(getattr(memo, section)):
            if not claim.evidence:
                problems.append(f"{section}[{i}] has no citation: {claim.text!r}")
            bad = [e for e in claim.evidence if e not in valid_ids]
            if bad:
                problems.append(f"{section}[{i}] cites unknown evidence {bad}")
    return problems


def write_memo(decision, findings, evidence, invoice_summary):
    """Return (memo_dict, method). method is 'claude', or 'template' when Claude is unavailable or failed."""
    valid_ids = {e["id"] for e in evidence}
    if config.anthropic_available():
        try:
            memo = _claude_memo(decision, findings, evidence, invoice_summary, valid_ids)
            if memo:
                return memo.model_dump(), "claude"
        except Exception as exc:
            print(f"[memo] Claude failed ({exc.__class__.__name__}: {exc}); using template memo")
    return template_memo(decision, findings, evidence), "template"


def _claude_memo(decision, findings, evidence, invoice_summary, valid_ids):
    import anthropic

    client = anthropic.Anthropic()
    payload = {
        "decision": decision,
        "invoice": invoice_summary,
        "policy_findings": findings,
        "evidence": [{k: e[k] for k in ("id", "kind", "source", "summary", "retrieved_at")} for e in evidence],
    }
    messages = [{"role": "user", "content": json.dumps(payload, indent=1, ensure_ascii=False)}]
    for _ in range(2):
        response = client.messages.parse(
            model=config.MODEL, max_tokens=6000, system=SYSTEM, messages=messages, output_format=Memo,
        )
        memo = response.parsed_output
        if memo is None:
            return None
        problems = validate(memo, valid_ids)
        if not problems:
            return memo
        messages += [
            {"role": "assistant", "content": memo.model_dump_json()},
            {"role": "user", "content": "Fix these citation problems and return the full memo again:\n"
                                        + "\n".join(problems)},
        ]
    return None


def template_memo(decision, findings, evidence):
    headline = {
        "PAY": "Paid: every check passed.",
        "DUPLICATE": "Not paid: this invoice was already paid.",
        "HOLD": "On hold: a person needs to decide.",
        "REJECTED": "Rejected: suspected impersonation, payment blocked.",
    }[decision]
    unverified_codes = {"sender_unverified", "ungrounded_amount", "ungrounded_invoice_number",
                        "ungrounded_sender_email", "ungrounded_bank_account_number", "amount_ambiguous",
                        "amount_disagreement"}
    needs = [f["needs"]["evidence"] for f in findings if f.get("needs")]
    return {
        "headline": headline,
        "verified_facts": [{"text": e["summary"], "evidence": [e["id"]]} for e in evidence if e["kind"] == "fact"],
        "inferences": [{"text": f["text"], "evidence": f["evidence"]} for f in findings
                       if f["code"] not in unverified_codes],
        "could_not_verify": [{"text": e["summary"], "evidence": [e["id"]]} for e in evidence
                             if e["kind"] == "unavailable"]
                            + [{"text": f["text"], "evidence": f["evidence"]} for f in findings
                               if f["code"] in unverified_codes],
        "next_step": {
            "PAY": "Payment sent through the payout API; the team has been notified.",
            "DUPLICATE": "No payment made. The team has been told which ledger entry already covers it.",
            "HOLD": "To pay this I need: " + "; ".join(needs) + ". I have asked the team." if needs
                    else "Waiting for a person to approve or reject.",
            "REJECTED": "No payment made. Confirm with the vendor through the contact in our register, "
                        "never through details in this email.",
        }[decision],
    }
