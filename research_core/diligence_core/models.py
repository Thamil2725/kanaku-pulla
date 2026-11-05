from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def invoice_identity(value: str) -> str:
    # Deliberately preserve punctuation/leading zeros: fuzzy invoice merging can lose debts.
    return re.sub(r"\s+", "", value).upper()


def minor_units(value: str) -> int:
    text = value.replace(",", "").strip()
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", text):
        raise ValueError("Amount must be a positive decimal with at most two fractional digits")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("Invalid amount") from exc
    if not amount.is_finite() or amount <= 0 or amount > Decimal("100000000000"):
        raise ValueError("Amount outside supported range")
    return int(amount * 100)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Coordinates(StrictModel):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)


class Supplier(StrictModel):
    name: str = Field(min_length=1, max_length=300)
    domain: str | None = Field(default=None, min_length=3, max_length=253)
    address: str | None = Field(default=None, max_length=2000)

    @field_validator("domain")
    @classmethod
    def domain_only(cls, value):
        if value is None:
            return None
        domain = value.strip().lower().rstrip(".").encode("idna").decode("ascii")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", domain) or ".." in domain:
            raise ValueError("Supply a hostname, not a URL/email")
        return domain


class Contact(StrictModel):
    name: str = Field(min_length=1, max_length=300)
    institution_domain: str | None = None
    role: Literal["invoice_sender", "paper_author"]
    # A supplied URL is an identity claim, not authentication of the submitter.
    profile_url: str | None = None

    @field_validator("institution_domain")
    @classmethod
    def domain_only(cls, value):
        return Supplier.domain_only(value)


class Invoice(StrictModel):
    number: str = Field(min_length=1, max_length=150)
    amount: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    account: str = Field(pattern=r"^\d{9,18}$")
    ifsc: str = Field(pattern=r"^[A-Z]{4}0[A-Z0-9]{6}$")
    po_number: str = Field(min_length=1, max_length=150)

    @field_validator("amount")
    @classmethod
    def positive_amount(cls, value):
        minor_units(value)
        return value.replace(",", "").strip()


class Document(StrictModel):
    kind: Literal["INVOICE", "ACADEMIC_BOM"]
    reference: str = Field(min_length=1, max_length=500)
    suppliers: list[Supplier] = Field(min_length=1, max_length=10)
    contact: Contact | None = None
    items: list[str] = Field(default_factory=list, max_length=100)
    invoice: Invoice | None = None


class Extraction(StrictModel):
    document: Document
    quotes: dict[str, str] = Field(default_factory=dict)
    method: str = "labelled_text"


class AcademicDocument(Document):
    """Research intake has no payment fields for a reader to invent."""
    kind: Literal["ACADEMIC_BOM"] = "ACADEMIC_BOM"
    invoice: None = None


class AcademicExtraction(Extraction):
    document: AcademicDocument


class Finding(StrictModel):
    code: str
    severity: Literal["info", "review", "hold"]
    text: str
    evidence_ids: list[str]
