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
    """Verified normalized facts and verbatim evidence bound to original source lines."""

    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    patient_alias: str = Field(pattern=r"^Demo-[A-Za-z0-9-]{1,32}$")
    medications: list[ExtractedMedication] = Field(min_length=1, max_length=6)
    labs: list[ExtractedLab] = Field(min_length=1, max_length=6)
    follow_up: list[ExtractedStatement] = Field(min_length=1, max_length=3)
    red_flags: list[ExtractedStatement] = Field(min_length=1, max_length=3)


class SourceLineReference(Contract):
    source_line: int = Field(
        strict=True, ge=1, le=25,
        description="Exact 1-based line number from the source tool. Cite the line for this record, not its array index.",
    )


class MedicationSelection(SourceLineReference):
    name: str = Field(min_length=1, max_length=60, description="Medicine identity copied exactly, including spelling and case.")
    dose: str = Field(min_length=1, max_length=12, description="Exact numeric source string, preserving decimal digits. Do not calculate or change it.")
    unit: Literal["mg", "mcg", "mL"] = Field(description="The medicine dose unit exactly as written, with original capitalization.")
    frequency: Literal["QD", "BID", "TID", "QHS"] = Field(
        description="once daily/QD => QD; twice daily/BID => BID; three times daily/TID => TID; at bedtime/QHS => QHS.",
    )
    status: Literal["active", "stopped"] = Field(description="Take => active. Stop or Do not take => stopped. Preserve negation.")


class LabSelection(SourceLineReference):
    name: str = Field(min_length=1, max_length=60, description="Exact lab identity before the colon, excluding the Lab prefix.")
    value: str = Field(min_length=1, max_length=12, description="Exact measured numeric source string, preserving sign and decimals.")
    unit: Literal["mmol/L", "mg/dL", "g/dL", "10^9/L", "mL/min", "ratio"]
    low: str = Field(min_length=1, max_length=12, description="Exact numeric string after reference and before to.")
    high: str = Field(min_length=1, max_length=12, description="Exact numeric string after to and before the closing parenthesis.")


class SourceSelection(Contract):
    """Extract clinical fields and cite original lines. Code attaches only verbatim source text."""

    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="Copy the source tool's source_sha256 exactly.")
    patient_alias: str = Field(pattern=r"^Demo-[A-Za-z0-9-]{1,32}$", description="Exact alias after Synthetic alias: . Keep the Demo- prefix.")
    medications: list[MedicationSelection] = Field(min_length=1, max_length=6, description="All medicine lines in their original source order, once each.")
    labs: list[LabSelection] = Field(min_length=1, max_length=6, description="All lab lines in their original source order, once each.")
    follow_up: list[SourceLineReference] = Field(min_length=1, max_length=3, description="Cite every Follow-up: line, in source order. Do not include its text.")
    red_flags: list[SourceLineReference] = Field(min_length=1, max_length=3, description="Cite every Get help: line, in source order. Do not include its text.")
