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


def fmt(paise):
    if paise is None:
        return "unknown amount"
    rupees, p = divmod(int(paise), 100)
    return f"₹{rupees:,}.{p:02d}"
