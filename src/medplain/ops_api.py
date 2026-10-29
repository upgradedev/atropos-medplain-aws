"""Anonymous synthetic operations API. Demo roles are not staff identities."""

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import Field, field_validator

from medplain.ops_domain import OpsError, OpsService, detail, summary
from medplain.ops_store import Conflict, MemoryStore, StoreUnavailable, get_store
from medplain.schemas import Contract

logger = logging.getLogger(__name__)


class OpsRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request):
            try:
                return await original(request)
            except OpsError as exc:
                return JSONResponse(status_code=exc.status, content={"detail": exc.message})
            except Conflict:
                return JSONResponse(status_code=409, content={"detail": "The synthetic record changed. Refresh and retry."})
            except StoreUnavailable:
                logger.error("Synthetic operations storage unavailable")
                return JSONResponse(status_code=503, content={
                    "detail": "The operations API cannot reach its configured store. No action was accepted."})

        return handle


router = APIRouter(prefix="/api/ops", route_class=OpsRoute)


def service() -> OpsService:
    return OpsService(get_store())


def authenticated(svc: Annotated[OpsService, Depends(service)],
                  token: Annotated[str | None, Header(alias="X-Medplain-Session")] = None):
    pk, session = svc.authenticate(token)
    return svc, pk, session


Auth = Annotated[tuple, Depends(authenticated)]
Service = Annotated[OpsService, Depends(service)]


class RoleRequest(Contract):
    role: Literal["operator", "reviewer", "auditor", "admin"]


class SessionRequest(Contract):
    prepared_demo: bool | None = None


class CreateCaseRequest(Contract):
    packet_id: str = Field(min_length=1, max_length=80)
    facility_id: str = Field(min_length=1, max_length=80)
    stress: bool = False
    idempotency_key: str = Field(min_length=8, max_length=128)


class ImportCaseRequest(Contract):
    source_text: str = Field(min_length=1, max_length=8000)
    synthetic_confirmed: Literal[True]
    facility_id: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("synthetic_confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicitly confirm that the submitted note is wholly invented.")
        return value


class RetryRequest(Contract):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)


class ReviewRequest(Contract):
    decision: Literal["release", "reject"]
    expected_version: int = Field(ge=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    comment: str = Field(default="", max_length=1000)


def schedule_memory(background: BackgroundTasks, svc: OpsService, pk: str, case: dict, created: bool):
    if created and isinstance(svc.store, MemoryStore):
        from medplain.ops_worker import process_message

        svc.store.mark_sent(pk, f"OUTBOX#{case['id']}#{case['generation']}")
        background.add_task(process_message, {"session_key": pk, "case_id": case["id"],
                                              "generation": case["generation"]}, svc.store)


@router.post("/session", status_code=201)
def new_session(svc: Service, body: SessionRequest | None = None):
    return svc.new_session(body.prepared_demo if body else None)


@router.post("/session/demo")
def seed_demo(auth: Auth):
    svc, pk, session = auth
    return svc.seed_demo(pk, session)


@router.post("/session/role")
def set_role(body: RoleRequest, auth: Auth):
    svc, pk, _ = auth
    return svc.role(pk, body.role)


@router.get("/bootstrap")
def bootstrap(auth: Auth):
    svc, pk, session = auth
    return svc.bootstrap(session, pk)


@router.post("/cases", status_code=202)
def create_case(body: CreateCaseRequest, background: BackgroundTasks, auth: Auth):
    svc, pk, session = auth
    case, created, job_id = svc.create(pk, session, **body.model_dump())
    schedule_memory(background, svc, pk, case, created)
    return {"case": summary(case), "created": created, "job_id": job_id}


@router.get("/intake-template")
def intake_template():
    from medplain.intake import template

    return template()


@router.post("/cases/import", status_code=202)
def import_case(body: ImportCaseRequest, background: BackgroundTasks, auth: Auth):
    svc, pk, session = auth
    case, created, job_id = svc.import_case(pk, session, **body.model_dump())
    schedule_memory(background, svc, pk, case, created)
    return {"case": summary(case), "created": created, "job_id": job_id}


@router.get("/cases")
def list_cases(auth: Auth, workspace: str = "all", status: str = "", q: str = Query(default="", max_length=128),
               sort: str = "created_at", direction: str = "desc", cursor: str = Query(default="", max_length=512),
               limit: int = Query(default=10, ge=1, le=20)):
    svc, pk, _ = auth
    return svc.list_cases(pk, workspace, status, q, sort, direction, cursor, limit)


@router.get("/cases/{case_id}")
def case_detail(case_id: str, auth: Auth):
    svc, pk, _ = auth
    return detail(svc.get_case(pk, case_id))


@router.post("/cases/{case_id}/retry", status_code=202)
def retry_case(case_id: str, body: RetryRequest, background: BackgroundTasks, auth: Auth):
    svc, pk, session = auth
    case, created, job_id = svc.retry(pk, session, case_id, **body.model_dump())
    schedule_memory(background, svc, pk, case, created)
    return {"case": summary(case), "created": created, "job_id": job_id}


@router.post("/cases/{case_id}/review")
def review_case(case_id: str, body: ReviewRequest, auth: Auth):
    svc, pk, session = auth
    return {"case": detail(svc.review(pk, session, case_id, **body.model_dump()))}


@router.get("/overview")
def overview(auth: Auth, workspace: str = "all"):
    svc, pk, _ = auth
    return svc.overview(pk, workspace)


@router.get("/patient/{share_id}")
def patient(share_id: str, svc: Service):
    return svc.patient(share_id)
