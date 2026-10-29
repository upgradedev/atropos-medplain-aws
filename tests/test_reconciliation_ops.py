"""Dossier intake keeps durable ownership, exact review and public-read gates."""
import copy
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from medplain.ops_api import router
from medplain.ops_domain import OpsError, OpsService
from medplain.ops_store import MemoryStore, reset_store_cache
from medplain.ops_worker import process_message
from medplain.reconciliation import PROFILE, examples


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


def owner(client):
    session = client.post("/api/ops/session", json={"prepared_demo": False}).json()
    return {"X-Medplain-Session": session["session_token"]}


def command(**updates):
    return {"dossier": copy.deepcopy(examples()[0]["dossier"]), "synthetic_confirmed": True,
            "facility_id": "demo-athens", "idempotency_key": "new-dossier-command", **updates}


def queued_service(monkeypatch):
    monkeypatch.setenv("MEDPLAIN_MODEL_MODE", "fixture")
    svc = OpsService(MemoryStore())
    pk, session = svc.authenticate(svc.new_session(False)["session_token"])
    case = svc.reconcile_case(pk, session, **command())[0]
    return svc, pk, session, case


def review(svc, pk, session, case):
    return svc.review(pk, {**session, "role": "reviewer"}, case["id"], "release", case["version"],
                      case["source_hash"], case["artifact_hash"], "Invented clarification card reviewed.")


def test_public_sources_and_real_sdk_dossier_review_public_journey(client):
    response = client.get("/api/ops/reconciliation-examples")
    assert response.status_code == 200 and response.json()["profile"] == PROFILE
    assert len(response.json()["examples"]) >= 3
    material = response.json()["reference_material"]
    assert material["identity_snapshot"]["status"] == "pinned_reference_snapshot"
    assert "unavailable" in material["interaction_service"]
    headers = owner(client)
    accepted = client.post("/api/ops/cases/reconcile", headers=headers, json=command())
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["case"]["source_profile"] == PROFILE
    case_id = accepted.json()["case"]["id"]
    case = client.get("/api/ops/cases/" + case_id, headers=headers).json()
    assert case["source"]["profile"] == PROFILE and case["intake"] is None
    assert case["status"] == "review_required" and case["patient_url"] is None
    assert case["result"]["status"] == "verified" and case["result"]["patient_regimen"] is None
    assert case["result"]["runtime"]["orchestrator"] == "strands.multiagent.Graph"
    assert case["result"]["artifact"]["kind"] == "clarification_plan"
    assert client.get("/api/ops/cases/" + case_id, headers=owner(client)).status_code == 404
    client.post("/api/ops/session/role", headers=headers, json={"role": "reviewer"})
    released = client.post(f"/api/ops/cases/{case_id}/review", headers=headers, json={
        "decision": "release", "expected_version": case["version"], "source_hash": case["source_hash"],
        "artifact_hash": case["artifact_hash"], "comment": "Clarification only; no regimen authorized."})
    assert released.status_code == 200, released.text
    share_id = released.json()["case"]["patient_url"].rsplit("/", 1)[-1]
    public = client.get("/api/ops/patient/" + share_id)
    assert public.status_code == 200, public.text
    payload = public.json()
    assert payload["source"] == case["source"]
    assert payload["result"]["artifact"] == case["result"]["artifact"]
    assert payload["result"]["patient_regimen"] is None and "No medication regimen" in payload["scope"]
    assert headers["X-Medplain-Session"] not in json.dumps(payload)
    assert "SESSION#" not in json.dumps(payload)


@pytest.mark.parametrize("updates", [
    {"synthetic_confirmed": False}, {"synthetic_confirmed": 1}, {"synthetic_confirmed": "true"},
    {"facility_id": "not-a-demo-desk"}, {"stress": "false"}, {"unknown": "extra-field"},
    {"dossier": {"profile": PROFILE}},
])
def test_bad_commands_do_not_create_jobs(client, monkeypatch, updates):
    scheduled = []
    monkeypatch.setattr("medplain.ops_api.schedule_memory", lambda *args: scheduled.append(True))
    headers = owner(client)
    response = client.post("/api/ops/cases/reconcile", headers=headers, json=command(**updates))
    assert response.status_code == 422
    assert not scheduled and client.get("/api/ops/cases", headers=headers).json()["total"] == 0


def test_idempotent_replay_precedes_daily_limit_and_changed_source_collides(client, monkeypatch):
    monkeypatch.setattr("medplain.ops_api.schedule_memory", lambda *args: None)
    monkeypatch.setenv("MEDPLAIN_OPS_DAILY_LIMIT", "1")
    headers = owner(client)
    first = client.post("/api/ops/cases/reconcile", headers=headers, json=command())
    assert first.status_code == 202
    replay = client.post("/api/ops/cases/reconcile", headers=headers, json=command())
    assert replay.status_code == 202 and replay.json()["created"] is False
    changed = command()
    changed["dossier"]["documents"][0]["text"] += "\nA newly reported concern remains unresolved."
    assert client.post("/api/ops/cases/reconcile", headers=headers, json=changed).status_code == 409
    assert client.post("/api/ops/cases/reconcile", headers=headers,
                       json=command(idempotency_key="new-job-over-global-cap")).status_code == 429
    # A different intake profile shares the same daily allowance.
    assert client.post("/api/ops/cases", headers=headers, json={
        "packet_id": "home-after-pneumonia", "facility_id": "demo-athens", "stress": False,
        "idempotency_key": "legacy-job-over-global-cap"}).status_code == 429


def test_dossier_command_obeys_role_and_owner_boundaries(client):
    assert client.post("/api/ops/cases/reconcile", json=command()).status_code == 401
    headers = owner(client)
    client.post("/api/ops/session/role", headers=headers, json={"role": "auditor"})
    assert client.post("/api/ops/cases/reconcile", headers=headers, json=command()).status_code == 403


def test_source_revision_creates_new_case_and_preserves_old_evidence(monkeypatch):
    svc, pk, session, original = queued_service(monkeypatch)
    assert process_message({"session_key": pk, "case_id": original["id"], "generation": 1}, svc.store)
    completed = svc.get_case(pk, original["id"])
    old = copy.deepcopy(completed)
    source = copy.deepcopy(completed["source"])
    source["documents"][-1]["text"] += "\nThe care team has been asked to clarify the original discrepancy."
    revised = svc.reconcile_case(pk, session, source, True, "demo-athens", "source-revision-new-case",
                                 parent_case_id=original["id"])[0]
    assert revised["id"] != original["id"] and revised["parent_case_id"] == original["id"]
    assert revised["source_hash"] != old["source_hash"]
    assert svc.get_case(pk, original["id"]) == old
    assert revised["result"] is None and revised["patient_url"] is None
    outsider, other = svc.authenticate(svc.new_session(False)["session_token"])
    with pytest.raises(OpsError) as error:
        svc.reconcile_case(outsider, other, source, True, "demo-athens", "cross-owner-revision",
                           parent_case_id=original["id"])
    assert error.value.status == 404


def test_changed_dossier_is_refused_before_model_call(monkeypatch):
    svc, pk, _, case = queued_service(monkeypatch)
    svc.store.rows[(pk, "CASE#" + case["id"])]["source"]["documents"][0]["text"] += " Changed."
    calls = []
    monkeypatch.setattr("medplain.reconciliation.run_reconciliation", lambda *args, **kwargs: calls.append(True))
    assert process_message({"session_key": pk, "case_id": case["id"], "generation": 1}, svc.store)
    failed = svc.get_case(pk, case["id"])
    assert not calls and failed["status"] == "failed" and failed["artifact_hash"] is None


@pytest.mark.parametrize("mutation", ["source", "regimen", "artifact", "execution", "profile"])
def test_review_and_public_reads_revalidate_saved_evidence(monkeypatch, mutation):
    svc, pk, session, case = queued_service(monkeypatch)
    assert process_message({"session_key": pk, "case_id": case["id"], "generation": 1}, svc.store)
    completed = svc.get_case(pk, case["id"])
    released = review(svc, pk, session, completed)
    share_id = released["patient_url"].rsplit("/", 1)[-1]
    assert svc.patient(share_id)["result"]["patient_regimen"] is None
    share = svc.store.rows[("SHARE#" + share_id, "VIEW")]
    if mutation == "source":
        share["source"]["documents"][0]["text"] += " Changed after review."
    elif mutation == "regimen":
        share["result"]["patient_regimen"] = {"instruction": "Take an extra dose."}
    elif mutation == "artifact":
        share["result"]["artifact"]["source_statements"][0]["citation"]["quote"] += " Changed."
    elif mutation == "execution":
        share["execution_id"] = "another-generation:2"
    else:
        share["source_profile"] = "unsupported-profile"
    with pytest.raises(OpsError) as error:
        svc.patient(share_id)
    assert error.value.status == 409
