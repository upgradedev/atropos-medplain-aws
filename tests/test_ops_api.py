"""Anonymous console command-to-worker-to-review-to-patient integration."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from medplain.ops_api import router
from medplain.ops_store import Conflict, StoreUnavailable, get_store, reset_store_cache


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MEDPLAIN_MODEL_MODE", "fixture")
    monkeypatch.delenv("MEDPLAIN_OPS_TABLE", raising=False)
    reset_store_cache()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client
    reset_store_cache()


def session(client):
    response = client.post("/api/ops/session", json={})
    assert response.status_code == 201
    visitor = response.json()
    return visitor, {"X-Medplain-Session": visitor["session_token"]}


def test_actual_sdk_stress_job_review_and_public_patient_share(client):
    visitor, headers = session(client)
    bootstrap = client.get("/api/ops/bootstrap", headers=headers).json()
    assert bootstrap["session"]["demo"] and bootstrap["runtime"]["storage"].startswith("memory")
    assert bootstrap["limits"]["daily_jobs"] == 120
    response = client.post("/api/ops/cases", headers=headers, json={
        "packet_id": "dosage-mutation-stress", "facility_id": "demo-athens", "stress": True,
        "idempotency_key": "actual-sdk-stress"})
    assert response.status_code == 202 and response.json()["created"]
    case_id = response.json()["case"]["id"]
    case = client.get("/api/ops/cases/" + case_id, headers=headers).json()
    assert case["status"] == "review_required" and case["correction_count"] == 1
    assert [r["verdict"] for r in case["result"]["receipts"]] == ["refused", "verified"]
    assert case["result"]["runtime"]["orchestrator"] == "strands.multiagent.Graph"
    assert not case["patient_url"]
    review = {"decision": "release", "expected_version": case["version"], "source_hash": case["source_hash"],
              "artifact_hash": case["artifact_hash"], "comment": "Reviewed synthetic correction evidence"}
    assert client.post(f"/api/ops/cases/{case_id}/review", headers=headers, json=review).status_code == 403
    assert client.post("/api/ops/session/role", headers=headers, json={"role": "reviewer"}).json()["demo"]
    released = client.post(f"/api/ops/cases/{case_id}/review", headers=headers, json=review)
    assert released.status_code == 200, released.text
    case = released.json()["case"]
    share = case["patient_url"].split("/")[-1]
    public = client.get("/api/ops/patient/" + share)
    assert public.status_code == 200
    assert public.json()["result"]["explanation"]["medications"][0]["dose"] == "2.5"
    assert public.json()["case"]["generation"] == 1
    assert visitor["session_token"] not in public.text and visitor["session_id"] not in public.text
    counts = client.get("/api/ops/overview?workspace=demo-athens", headers=headers).json()
    assert counts["counts"]["released"] == counts["counts"]["total"] == 1
    assert len(counts["durations"]) == 1 and counts["durations"][0]["duration_ms"] >= 0


def test_actual_sdk_clean_job_auto_releases_and_idempotent_post_is_not_new_inference(client):
    _, headers = session(client)
    body = {"packet_id": "home-after-pneumonia", "facility_id": "demo-patras",
            "idempotency_key": "actual-sdk-clean"}
    first = client.post("/api/ops/cases", headers=headers, json=body).json()
    replay = client.post("/api/ops/cases", headers=headers, json=body).json()
    assert not replay["created"] and replay["job_id"] == first["job_id"]
    case = client.get("/api/ops/cases/" + first["case"]["id"], headers=headers).json()
    assert case["status"] == "released" and case["version"] == 3
    result = client.get("/api/ops/cases?workspace=demo-patras&q=pneumonia&sort=duration_ms", headers=headers).json()
    assert result["total"] == 1 and result["items"][0]["id"] == case["id"]


def test_owner_boundary_roles_and_private_inputs_are_enforced(client):
    _, first = session(client)
    _, second = session(client)
    assert client.get("/api/ops/cases").status_code == 401
    assert client.get("/api/ops/cases", headers={"X-Medplain-Session": "x" * 43}).status_code == 401
    assert client.get("/api/ops/cases/" + "0" * 32, headers=second).status_code == 404
    assert client.post("/api/ops/cases", headers=first, json={"source_text": "private record"}).status_code == 422
    assert client.post("/api/ops/session/role", headers=first, json={"role": "auditor"}).status_code == 200
    assert client.post("/api/ops/cases", headers=first, json={"packet_id": "home-after-pneumonia",
        "facility_id": "demo-athens", "idempotency_key": "read-only-role"}).status_code == 403
    assert client.get("/api/ops/cases?workspace=unknown", headers=first).status_code == 422
    assert client.get("/api/ops/cases?limit=50", headers=first).status_code == 422
    assert client.get("/api/ops/patient/unavailable").status_code == 404


def test_outage_never_falls_back_to_memory_and_has_visible_api_reason(client, monkeypatch):
    import medplain.ops_api as module

    def unavailable():
        raise StoreUnavailable()

    monkeypatch.setattr(module, "get_store", unavailable)
    response = client.post("/api/ops/session", json={})
    assert response.status_code == 503 and "configured store" in response.json()["detail"]


def test_unexpected_compare_and_swap_conflict_is_visible(client, monkeypatch):
    store = get_store()

    def conflict(*args):
        raise Conflict()

    monkeypatch.setattr(store, "put_new", conflict)
    response = client.post("/api/ops/session", json={})
    assert response.status_code == 409 and "Refresh" in response.json()["detail"]


def test_daily_allowance_is_visible_http429_and_not_consumed_by_replay(client, monkeypatch):
    import medplain.ops_api as module

    monkeypatch.setenv("MEDPLAIN_OPS_DAILY_LIMIT", "1")
    # This test checks command acceptance only, without invoking another model.
    monkeypatch.setattr(module, "schedule_memory", lambda *args: None)
    _, headers = session(client)
    body = {"packet_id": "home-after-pneumonia", "facility_id": "demo-athens", "idempotency_key": "allowance-first"}
    assert client.post("/api/ops/cases", headers=headers, json=body).status_code == 202
    assert not client.post("/api/ops/cases", headers=headers, json=body).json()["created"]
    body["idempotency_key"] = "allowance-second"
    response = client.post("/api/ops/cases", headers=headers, json=body)
    assert response.status_code == 429 and "00:00 UTC" in response.json()["detail"]
