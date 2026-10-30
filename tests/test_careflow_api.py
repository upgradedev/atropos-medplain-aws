"""Real offline SDK case, ownership, public-response and review integration."""

import copy
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from medplain.careflow_api import router as careflow_router
from medplain.ops_api import router as operations_router
from medplain.ops_store import reset_store_cache
from medplain.reconciliation import examples


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MEDPLAIN_MODEL_MODE", "fixture")
    monkeypatch.setenv("MEDPLAIN_CAREFLOW_MODEL_MODE", "fixture")
    monkeypatch.delenv("MEDPLAIN_OPS_TABLE", raising=False)
    reset_store_cache()
    app = FastAPI()
    app.include_router(operations_router)
    app.include_router(careflow_router)
    with TestClient(app) as client:
        yield client
    reset_store_cache()


def owner(client):
    session = client.post("/api/ops/session", json={"prepared_demo": False}).json()
    return {"X-Medplain-Session": session["session_token"]}


def binding(view, key):
    return {"expected_version": view["case_version"], "source_hash": view["source_hash"],
            "artifact_hash": view["artifact_hash"], "idempotency_key": key}


def released_case(client, headers):
    created = client.post("/api/ops/cases/reconcile", headers=headers, json={
        "dossier": copy.deepcopy(examples()[0]["dossier"]), "synthetic_confirmed": True,
        "facility_id": "demo-athens", "idempotency_key": "careflow-api-new-case"})
    assert created.status_code == 202, created.text
    case_id = created.json()["case"]["id"]
    case = client.get(f"/api/ops/cases/{case_id}", headers=headers).json()
    assert case["status"] == "review_required" and case["result"]["status"] == "verified"
    client.post("/api/ops/session/role", headers=headers, json={"role": "reviewer"})
    response = client.post(f"/api/ops/cases/{case_id}/review", headers=headers, json={
        "decision": "release", "expected_version": case["version"],
        "source_hash": case["source_hash"], "artifact_hash": case["artifact_hash"], "comment": "Synthetic review"})
    assert response.status_code == 200, response.text
    return response.json()["case"]


def test_real_sdk_clarification_acceptance_response_and_reviewer_journey(client):
    headers = owner(client)
    case = released_case(client, headers)
    path = f"/api/ops/cases/{case['id']}/careflow"
    view = client.get(path, headers=headers).json()
    assert view["source_hash"] == case["source_hash"] and view["tasks"]
    actors = client.get("/api/ops/careflow/actors", headers=headers).json()
    assert client.post("/api/ops/careflow/actor", headers=headers, json={
        "actor_id": "demo-pharmacist", "expected_actor_version": actors["actor_version"]}).status_code == 200
    task_id = view["tasks"][0]["id"]
    response = client.post(path + "/assign", headers=headers, json={
        **binding(view, "careflow-api-assign"), "task_id": task_id,
        "assignee_id": "demo-pharmacist", "due_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["tasks"][0]["status"] == "assigned"
    response = client.post(path + "/accept", headers=headers, json={
        **binding(view, "careflow-api-accept"), "task_id": task_id, "actor_id": "demo-pharmacist"})
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["tasks"][0]["accepted_at"]
    response = client.post(path + "/answer", headers=headers, json={
        **binding(view, "careflow-api-answer"), "task_id": task_id, "actor_id": "demo-pharmacist",
        "answer_text": "PRIVATE SYNTHETIC STAFF ANSWER. A verified source revision is still required."})
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["tasks"][0]["status"] == "answered" and view["tasks"][0]["closure"] is None
    enabled = client.post(path + "/enable-teachback", headers=headers,
                          json=binding(view, "careflow-api-response-access"))
    assert enabled.status_code == 200, enabled.text
    parts = urlsplit(enabled.json()["response_url"])
    share = parts.path.rsplit("/", 1)[-1]
    cap = parse_qs(parts.fragment)["teachback"][0]
    public_path = "/api/ops/careflow/patient/" + share
    assert client.get(public_path).status_code in {401, 403, 404}
    assert client.get(public_path, headers=headers).status_code in {401, 403, 404}
    cap_headers = {"X-Medplain-Teachback": cap}
    public = client.get(public_path, headers=cap_headers)
    assert public.status_code == 200, public.text
    assert public.headers["cache-control"] == "no-store"
    assert public.headers["vary"] == "X-Medplain-Teachback"
    assert "PRIVATE SYNTHETIC STAFF ANSWER" not in public.text
    assert "SESSION#" not in public.text and headers["X-Medplain-Session"] not in public.text
    public_view = public.json()
    body = {"expected_version": public_view["case_version"], "prompt_id": public_view["prompt"]["prompt_id"],
            "response_text": "I understand", "synthetic_confirmed": True,
            "idempotency_key": "careflow-api-actual-patient-response"}
    submitted = client.post(public_path + "/responses", headers=cap_headers, json=body)
    assert submitted.status_code == 202, submitted.text
    assert submitted.json()["attempt_id"]
    replay = client.post(public_path + "/responses", headers=cap_headers, json=body)
    assert replay.status_code == 202 and replay.json()["created"] is False
    public_view = client.get(public_path, headers=cap_headers).json()
    assert len(public_view["attempts"]) == 1
    attempt = public_view["attempts"][0]
    assert attempt["status"] == "assessed", attempt
    assert attempt["assessment"]["status"] == "provisional"
    assert attempt["assessment"]["requires_reviewer"] is True
    assert attempt["assessment"]["patient_regimen"] is None
    assert attempt["assessment"]["assessment"]["missing_statement_ids"]
    view = client.get(path, headers=headers).json()
    reviewed = client.post(path + f"/teachback/{attempt['id']}/review", headers=headers, json={
        **binding(view, "careflow-api-review-response"), "actor_id": "demo-pharmacist",
        "decision": "follow_up", "comment": "Private reviewer note must not become patient advice."})
    assert reviewed.status_code == 200, reviewed.text
    public = client.get(public_path, headers=cap_headers)
    assert "Private reviewer note" not in public.text
    assert public.json()["attempts"][0]["review"]["decision"] == "follow_up"
    unchanged = client.get(f"/api/ops/cases/{case['id']}", headers=headers).json()
    assert unchanged["source"] == case["source"] and unchanged["result"] == case["result"]
    assert unchanged["source_hash"] == case["source_hash"] and unchanged["artifact_hash"] == case["artifact_hash"]
    assert client.get("/api/ops/patient/" + share).json()["result"]["patient_regimen"] is None


def test_evidence_requires_owner_and_has_explicit_no_support(client):
    query = {"query": "handoff responsibility accepted delegate"}
    assert client.post("/api/ops/careflow/evidence/search", json=query).status_code == 401
    headers = owner(client)
    response = client.post("/api/ops/careflow/evidence/search", headers=headers, json=query)
    assert response.status_code == 200, response.text
    assert response.json()["entries"]
    missing = client.post("/api/ops/careflow/evidence/search", headers=headers,
                          json={"query": "zzzxxyynoapprovedtopic"})
    assert missing.status_code == 200 and not missing.json()["entries"]
    assert missing.json()["status"] == "no_support"
    assert client.post("/api/ops/careflow/evidence/search", headers=headers,
                       json={"query": "handoff", "url": "https://unapproved.invalid"}).status_code == 422


def test_evidence_integrity_failure_is_unavailable_without_a_fallback(client, monkeypatch):
    headers = owner(client)

    def corrupt(query):
        raise ValueError("private integrity diagnostic")

    monkeypatch.setattr("medplain.careflow_retrieval.search_workflow_evidence", corrupt)
    response = client.post("/api/ops/careflow/evidence/search", headers=headers, json={"query": "teach back"})
    assert response.status_code == 503
    assert "integrity" in response.json()["detail"] and "private" not in response.text
    assert "entries" not in response.json()
