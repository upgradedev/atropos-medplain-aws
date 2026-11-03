"""Bounded synthetic workflow commands, never medical source instructions."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

ActorId = Literal["demo-pharmacist", "demo-discharge-clinician", "demo-followup-nurse"]


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: StrictInt = Field(ge=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=128)


class ActorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    actor_id: ActorId
    expected_actor_version: StrictInt = Field(ge=0)


class TaskCommand(Command):
    task_id: str = Field(pattern=r"^cf-[0-9a-f]{24}$")


class AssignRequest(TaskCommand):
    assignee_id: ActorId
    due_at: str = Field(min_length=20, max_length=40)


class AcceptRequest(TaskCommand):
    actor_id: ActorId


class AnswerRequest(AcceptRequest):
    answer_text: str = Field(min_length=1, max_length=2000)


class LinkRevisionRequest(AcceptRequest):
    revision_case_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class CloseRequest(AcceptRequest):
    clinician_confirmed: StrictBool
    revision_source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class EnableTeachbackRequest(Command):
    pass


class ResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: StrictInt = Field(ge=1)
    prompt_id: str = Field(min_length=1, max_length=100)
    response_text: str = Field(min_length=1, max_length=2000)
    synthetic_confirmed: StrictBool
    idempotency_key: str = Field(min_length=8, max_length=128)


class TeachbackReviewRequest(Command):
    actor_id: ActorId
    decision: Literal["acknowledged", "follow_up"]
    comment: str = Field(default="", max_length=1000)
