"""New note command, immutable provenance, SDK worker and exact-source release."""

import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from medplain.intake import TEMPLATE
from medplain.ops_api import router
from medplain.ops_domain import OpsError, OpsService, detail
from medplain.ops_store import MemoryStore, reset_store_cache
from medplain.ops_worker import process_message


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MEDPLAIN_MODEL_MODE", "fixture")
    monkeypatch.delenv("MEDPLAIN_OPS_TABLE", raising=False)
    reset_store_cache()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as browser:
        yield browser
    reset_store_cache()


def headers(client):
    session = client.post("/api/ops/session", json={"prepared_demo": False}).json()
    return {"X-Medplain-Session": session["session_token"]}


def body(**updates):
    return {"source_text": TEMPLATE, "synthetic_confirmed": True, "facility_id": "demo-athens",
            "idempotency_key": "new-invented-source", **updates}


def test_template_is_public_and_new_note_follows_actual_worker_review_release(client):
    template = client.get("/api/ops/intake-template")
    assert template.status_code == 200
    assert template.json()["source_text"] == TEMPLATE
    assert "does not detect" in template.json()["privacy_notice"]
    owner = headers(client)
    accepted = client.post("/api/ops/cases/import", headers=owner, json=body())
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["case"]["source_kind"] == "imported_note"
    case_id = accepted.json()["case"]["id"]
    case = client.get("/api/ops/cases/" + case_id, headers=owner).json()
    assert case["status"] == "review_required" and case["patient_url"] is None
    assert case["intake"]["source_text"] == TEMPLATE
    assert case["result"]["intake_validation"]["parser_verified"] is True
    assert case["result"]["runtime"]["orchestrator"] == "strands.multiagent.Graph"
    assert client.get("/api/ops/cases/" + case_id, headers=headers(client)).status_code == 404
    replay = client.post("/api/ops/cases/import", headers=owner, json=body())
    assert replay.status_code == 202 and replay.json()["created"] is False
    assert replay.json()["job_id"] == accepted.json()["job_id"]
    client.post("/api/ops/session/role", headers=owner, json={"role": "reviewer"})
    released = client.post(f"/api/ops/cases/{case_id}/review", headers=owner, json={
        "decision": "release", "expected_version": case["version"], "source_hash": case["source_hash"],
        "artifact_hash": case["artifact_hash"], "comment": "Synthetic original note and extraction reviewed"})
    assert released.status_code == 200, released.text
    url = released.json()["case"]["patient_url"].split("/")[-1]
    public = client.get("/api/ops/patient/" + url)
    assert public.status_code == 200
    assert public.json()["source"]["fhir"]["identifier"]["value"] == case["intake"]["source_sha256"]


@pytest.mark.parametrize("updates", [
    {"synthetic_confirmed": False}, {"synthetic_confirmed": 1}, {"synthetic_confirmed": "true"},
    {"source_text": TEMPLATE + "\nTake extra medicine when needed."}, {"source_text": "x" * 8001},
    {"stress": True}, {"facility_id": "unavailable"},
])
def test_rejected_input_never_queues_or_calls_model(client, monkeypatch, updates):
    called = []
    monkeypatch.setattr("medplain.ops_api.schedule_memory", lambda *args: called.append(True))
    owner = headers(client)
    response = client.post("/api/ops/cases/import", headers=owner, json=body(**updates))
    assert response.status_code == 422
    assert called == []
    assert client.get("/api/ops/cases", headers=owner).json()["total"] == 0


def test_raw_source_idempotency_collision_and_daily_limit_are_preserved(client, monkeypatch):
    monkeypatch.setattr("medplain.ops_api.schedule_memory", lambda *args: None)
    monkeypatch.setenv("MEDPLAIN_OPS_DAILY_LIMIT", "1")
    owner = headers(client)
    assert client.post("/api/ops/cases/import", headers=owner, json=body()).status_code == 202
    changed = body(source_text=TEMPLATE.replace("500 mg", "250 mg"))
    assert client.post("/api/ops/cases/import", headers=owner, json=changed).status_code == 409
    assert not client.post("/api/ops/cases/import", headers=owner, json=body()).json()["created"]
    assert client.post("/api/ops/cases/import", headers=owner,
                       json=body(idempotency_key="different-accepted-job")).status_code == 429


def test_import_requires_operator_role_and_unknown_extra_fields_fail(client):
    owner = headers(client)
    client.post("/api/ops/session/role", headers=owner, json={"role": "auditor"})
    assert client.post("/api/ops/cases/import", headers=owner, json=body()).status_code == 403
    assert client.post("/api/ops/cases/import", json=body()).status_code == 401
    assert client.post("/api/ops/cases/import", headers=owner, json=body(patient_name="private")).status_code == 422


def test_saved_raw_note_is_immutable_across_worker_retry_and_public_read(monkeypatch):
    monkeypatch.setenv("MEDPLAIN_MODEL_MODE", "fixture")
    svc = OpsService(MemoryStore())
    pk, session = svc.authenticate(svc.new_session()["session_token"])
    case = svc.import_case(pk, session, TEMPLATE, True, "demo-athens", "import-once")[0]
    original = copy.deepcopy(case["intake"])
    assert detail(case)["source"] and detail(case)["result"] is None
    assert process_message({"session_key": pk, "case_id": case["id"], "generation": 1}, svc.store)
    current = svc.get_case(pk, case["id"])
    session["role"] = "reviewer"
    rejected = svc.review(pk, session, current["id"], "reject", current["version"],
                          current["source_hash"], current["artifact_hash"], "Synthetic retry exercise")
    session["role"] = "operator"
    retry = svc.retry(pk, session, rejected["id"], rejected["version"], "retry-original-note")[0]
    assert retry["intake"] == original
    assert process_message({"session_key": pk, "case_id": case["id"], "generation": 2}, svc.store)
    current = svc.get_case(pk, case["id"])
    session["role"] = "reviewer"
    svc.store.rows[(pk, "CASE#" + current["id"])]["intake"]["source_text"] += "\n"
    with pytest.raises(OpsError, match="independent publication"):
        svc.review(pk, session, current["id"], "release", current["version"], current["source_hash"],
                   current["artifact_hash"], "Do not release changed raw evidence")


def test_worker_rejects_changed_original_note_before_sdk_and_cannot_publish(monkeypatch):
    monkeypatch.setenv("MEDPLAIN_MODEL_MODE", "fixture")
    svc = OpsService(MemoryStore())
    pk, session = svc.authenticate(svc.new_session()["session_token"])
    case = svc.import_case(pk, session, TEMPLATE, True, "demo-athens", "raw-change-before-worker")[0]
    svc.store.rows[(pk, "CASE#" + case["id"])]["intake"]["source_sha256"] = "0" * 64
    assert process_message({"session_key": pk, "case_id": case["id"], "generation": 1}, svc.store)
    current = svc.get_case(pk, case["id"])
    assert current["status"] == "failed" and current["artifact_hash"] is None
    assert "No patient explanation" in current["error"]
