"""Separate V3 workflow commands. Original clinical routes stay unchanged."""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header

from medplain.careflow_domain import CareflowService
from medplain.careflow_models import (
    AcceptRequest, ActorRequest, AnswerRequest, AssignRequest, CloseRequest,
    EnableTeachbackRequest, LinkRevisionRequest, ResponseRequest, TeachbackReviewRequest,
)
from medplain.careflow_retrieval import EvidenceQuery
from medplain.ops_api import Auth, OpsRoute
from medplain.ops_store import MemoryStore, get_store

router = APIRouter(prefix="/api/ops", route_class=OpsRoute)


def service() -> CareflowService:
    return CareflowService(get_store())


Care = Annotated[CareflowService, Depends(service)]
Capability = Annotated[str | None, Header(alias="X-Medplain-Teachback")]


@router.get("/careflow/actors")
def actors(auth: Auth, svc: Care):
    _, pk, session = auth
    return svc.actors(pk, session)


@router.post("/careflow/actor")
def select_actor(body: ActorRequest, auth: Auth, svc: Care):
    _, pk, session = auth
    return svc.select_actor(pk, session, **body.model_dump())


@router.get("/cases/{case_id}/careflow")
def careflow(case_id: str, auth: Auth, svc: Care):
    _, pk, session = auth
    return svc.view(pk, session, case_id)


@router.post("/cases/{case_id}/careflow/assign")
def assign(case_id: str, body: AssignRequest, auth: Auth, svc: Care):
    _, pk, session = auth
    return svc.assign(pk, session, case_id, body.model_dump())


@router.post("/cases/{case_id}/careflow/accept")
def accept(case_id: str, body: AcceptRequest, auth: Auth, svc: Care):
    _, pk, session = auth
    return svc.accept(pk, session, case_id, body.model_dump())


@router.post("/cases/{case_id}/careflow/answer")
def answer(case_id: str, body: AnswerRequest, auth: Auth, svc: Care):
    _, pk, session = auth
    return svc.answer(pk, session, case_id, body.model_dump())


@router.post("/cases/{case_id}/careflow/link-revision")
def link_revision(case_id: str, body: LinkRevisionRequest, auth: Auth, svc: Care):
    _, pk, session = auth
    return svc.link_revision(pk, session, case_id, body.model_dump())


@router.post("/cases/{case_id}/careflow/close")
def close(case_id: str, body: CloseRequest, auth: Auth, svc: Care):
    _, pk, session = auth
    return svc.close(pk, session, case_id, body.model_dump())


@router.post("/cases/{case_id}/careflow/enable-teachback")
def enable_teachback(case_id: str, body: EnableTeachbackRequest, auth: Auth, svc: Care):
    _, pk, session = auth
    return svc.enable_teachback(pk, session, case_id, body.model_dump())


@router.get("/careflow/patient/{share_id}")
def patient_teachback(share_id: str, svc: Care, capability: Capability = None):
    return svc.patient_view(share_id, capability)


@router.post("/careflow/patient/{share_id}/responses", status_code=202)
def patient_response(share_id: str, body: ResponseRequest, background: BackgroundTasks,
                     svc: Care, capability: Capability = None):
    view, created, job_id, message = svc.submit_response(share_id, capability, body.model_dump())
    if created and isinstance(svc.store, MemoryStore):
        from medplain.careflow_worker import process_message

        sk = f"OUTBOX#CARE#{message['case_id']}#{message['attempt_id']}"
        svc.store.mark_sent(message["session_key"], sk)
        background.add_task(process_message, message, svc.store)
    return {"careflow": view, "created": created, "job_id": job_id}


@router.post("/cases/{case_id}/careflow/teachback/{attempt_id}/review")
def review_teachback(case_id: str, attempt_id: str, body: TeachbackReviewRequest,
                    auth: Auth, svc: Care):
    _, pk, session = auth
    return svc.review_teachback(pk, session, case_id, attempt_id, body.model_dump())


@router.post("/careflow/evidence/search")
def search_evidence(body: EvidenceQuery, auth: Auth):
    # Retrieval is a bounded read over packaged, versioned snapshots. The
    # response supplies workflow guidance and cannot replace patient facts.
    from medplain.careflow_retrieval import search_workflow_evidence
    from medplain.ops_domain import OpsError

    try:
        return search_workflow_evidence(body.model_dump(mode="json"))
    except (ValueError, TypeError):
        raise OpsError(422, "Use the supported evidence query, topic and applicability filters.") from None


@router.get("/careflow/profile")
def profile():
    return {"profile": "medplain-careflow-v3", "status": "separate-source-version",
            "scope": "Synthetic clarification ownership, educational retrieval and provisional teach-back assessment.",
            "staff_identity": "Explicit demo actor selection; no hospital authentication.",
            "clinical_authority": "No medication regimen is authorized. Original clinical gates remain required.",
            "worker_handler": "medplain.careflow_worker.handler",
            "relay_handler": "medplain.careflow_relay.handler"}
