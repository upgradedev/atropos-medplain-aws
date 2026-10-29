"""Strict contracts for a deliberately bounded synthetic discharge format."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Medication(Contract):
    id: str
    name: str
    dose: str = Field(pattern=r"^\d+(?:\.\d+)?$")
    unit: Literal["mg", "mcg", "mL"]
    frequency: Literal["QD", "BID", "TID", "QHS"]
    status: Literal["active", "stopped"] = "active"
    route: Literal["oral"] = "oral"
    instruction: str


class Lab(Contract):
    id: str
    name: str
    value: str = Field(pattern=r"^-?\d+(?:\.\d+)?$")
    unit: str
    low: str = Field(pattern=r"^-?\d+(?:\.\d+)?$")
    high: str = Field(pattern=r"^-?\d+(?:\.\d+)?$")
    plain_label: str


class ClinicalPacket(Contract):
    packet_id: str
    title: str
    subtitle: str
    patient: str
    source_text: str
    summary: str
    medications: list[Medication]
    labs: list[Lab]
    follow_up: list[str]
    red_flags: list[str]
    fhir: dict[str, Any]
    adversarial: bool = False


class MedicationInstruction(Contract):
    medication_id: str
    name: str
    dose: str
    unit: str
    frequency: str
    status: Literal["active", "stopped"]
    text: str


class LabExplanation(Contract):
    lab_id: str
    name: str
    value: str
    unit: str
    low: str
    high: str
    status: Literal["low", "within range", "high"]
    text: str


class PatientExplanation(Contract):
    summary: str
    medications: list[MedicationInstruction]
    labs: list[LabExplanation]
    follow_up: list[str]
    red_flags: list[str]


class GateVerdict(Contract):
    passed: bool
    discrepancies: list[str] = Field(default_factory=list)


class ExplainRequest(Contract):
    packet_id: str = Field(min_length=1, max_length=80)
    stress: bool | None = None


class ReceiptVerificationRequest(Contract):
    receipts: list[dict[str, Any]] = Field(min_length=1, max_length=20)


class SourceSpan(Contract):
    source_line: int = Field(ge=1, le=25)
    source_quote: str = Field(min_length=1, max_length=320)


class ExtractedMedication(SourceSpan):
    name: str = Field(min_length=1, max_length=60)
    dose: str = Field(min_length=1, max_length=12)
    unit: Literal["mg", "mcg", "mL"]
    frequency: Literal["QD", "BID", "TID", "QHS"]
    status: Literal["active", "stopped"]


class ExtractedLab(SourceSpan):
    name: str = Field(min_length=1, max_length=60)
    value: str = Field(min_length=1, max_length=12)
    unit: Literal["mmol/L", "mg/dL", "g/dL", "10^9/L", "mL/min", "ratio"]
    low: str = Field(min_length=1, max_length=12)
    high: str = Field(min_length=1, max_length=12)


class ExtractedStatement(SourceSpan):
    text: str = Field(min_length=1, max_length=160)


class SourceExtraction(Contract):
    """Model-normalized facts, each bound to a complete line of the original note."""

    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    patient_alias: str = Field(pattern=r"^Demo-[A-Za-z0-9-]{1,32}$")
    medications: list[ExtractedMedication] = Field(min_length=1, max_length=6)
    labs: list[ExtractedLab] = Field(min_length=1, max_length=6)
    follow_up: list[ExtractedStatement] = Field(min_length=1, max_length=3)
    red_flags: list[ExtractedStatement] = Field(min_length=1, max_length=3)
