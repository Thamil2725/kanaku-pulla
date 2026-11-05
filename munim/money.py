"""Money as integer paise, parsed with Decimal. No floats in any comparison that decides a payment."""

import re
from decimal import Decimal, InvalidOperation

SUPPORTED_CURRENCIES = {"INR"}  # first scope: rupee invoices only; anything else is held


def to_paise(value):
    """'1,18,000.00' -> 11800000. Returns None for missing, malformed, non-finite, zero or negative amounts."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    text = str(value).strip()
    if not text or text.startswith("-"):
        return None
    cleaned = re.sub(r"[,\s₹]", "", text)
    cleaned = re.sub(r"^(?:INR|Rs\.?)", "", cleaned, flags=re.IGNORECASE)
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", cleaned):
        return None
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    return int((amount * 100).to_integral_value())


def rupees_to_paise(value):
    """For trusted register numbers like 59000 or 118000.0."""
    return to_paise(format(Decimal(str(value)), "f"))


def group_indian(n: int) -> str:
    """1500000 -> '15,00,000' (lakh and crore grouping, as Indian invoices write it)."""
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def fmt(paise):
    if paise is None:
        return "unknown amount"
    rupees, p = divmod(int(paise), 100)
    return f"₹{group_indian(rupees)}.{p:02d}"
