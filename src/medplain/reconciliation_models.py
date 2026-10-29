"""A separate, clarification-only profile. No field can authorize a regimen."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PROFILE = "medplain-reconciliation-v1"
QUESTION_CODES = (
    "compare_instructions", "confirm_intentional_change", "confirm_taper",
    "confirm_hold_restart", "confirm_identity_form", "confirm_pending_owner",
    "caregiver_question", "teachback",
)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceDocument(Contract):
    document_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    kind: Literal["home_list", "discharge", "addendum", "caregiver_note", "lab_report"]
    issued_at: str
    effective_at: str | None = None
    text: str = Field(min_length=1, max_length=6000)


class Dossier(Contract):
    profile: Literal["medplain-reconciliation-v1"] = PROFILE
    patient_alias: str = Field(pattern=r"^Demo-[A-Za-z][A-Za-z -]{0,49}$")
    language: Literal["en", "el"] = "en"
    caregiver_concerns: str = Field(default="", max_length=1000)
    documents: list[SourceDocument] = Field(min_length=2, max_length=4)


class SourceIndex(Contract):
    """Coverage of original evidence, not a clinical interpretation."""

    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    statement_ids: list[str] = Field(min_length=1, max_length=60)


QuestionCode = Literal[
    "compare_instructions", "confirm_intentional_change", "confirm_taper",
    "confirm_hold_restart", "confirm_identity_form", "confirm_pending_owner",
    "caregiver_question", "teachback",
]


class QuestionSelection(Contract):
    """A proposed evidence link, never an instruction or verified clinical error."""

    code: QuestionCode
    statement_ids: list[str] = Field(min_length=1, max_length=8)


class ReconciliationSelection(Contract):
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    questions: list[QuestionSelection] = Field(max_length=12)
    teachback: list[QuestionSelection] = Field(min_length=1, max_length=6)


class PublicationSelection(Contract):
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_ref: str = Field(pattern=r"^clarify-[0-9a-f]{32}$")
    presentation: Literal["clarification-only-v1"]
