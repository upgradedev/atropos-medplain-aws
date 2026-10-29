"""Prepared examples remain honest, session-scoped and atomic, without live jobs."""

import copy
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from medplain.clinical import parse_packet
from medplain.fixtures import get_packet
from medplain.gate import canonical_hash
from medplain.ops_api import router
from medplain.ops_domain import OpsError, OpsService, detail, summary
from medplain.ops_seed import SCENARIOS, SEED_VERSION, check_seed_size, load_bundle
from medplain.ops_store import Conflict, DynamoStore, MemoryStore, StoreUnavailable, reset_store_cache
from medplain.ops_validation import validate_result
from medplain.ops_worker import process_message
from test_ops_store import AtomicDynamo


@pytest.fixture(params=["memory", "dynamo"])
def backend(request):
    client = AtomicDynamo() if request.param == "dynamo" else None
    store = DynamoStore("synthetic-ops", client) if client else MemoryStore()
    return OpsService(store), store, client


def visitor(svc, prepared=False):
    response = svc.new_session(prepared_demo=prepared)
    return svc.authenticate(response["session_token"])


def assert_no_jobs(store, client):
    rows = client.rows if client else store.rows
    assert not any(sk.startswith("OUTBOX#") or pk.startswith("ALLOWANCE#") for pk, sk in rows)


def test_packaged_preparations_have_actual_sdk_evidence_and_distinct_original_executions():
    bundle = load_bundle()
    assert bundle["version"] == SEED_VERSION
    ids = []
    for name, exemplar in bundle["exemplars"].items():
        result = exemplar["result"]
        ids.append(exemplar["execution_id"])
        assert result["session_id"] == exemplar["execution_id"]
        assert result["model_mode"] == "fixture" and result["commit"] == bundle["source_commit"]
        assert result["runtime"]["orchestrator"] == "strands.multiagent.Graph"
        if name == "interrupted-fixture":
            assert result["status"] == "refused" and result["explanation"] is None
        else:
            assert result["runtime"]["graph_status"] == "completed"
            assert result["runtime"]["tools_executed"] and result["runtime"]["hook_invocations"]
            assert [r["verdict"] for r in result["receipts"]] == (
                ["refused", "verified"] if name == "dosage-mutation-stress" else ["verified"])
    assert len(set(ids)) == 4


def test_seed_is_twelve_real_server_rows_with_five_public_views_and_no_jobs(backend):
    svc, store, client = backend
    pk, session = visitor(svc, True)
    rows = store.cases(pk)
    assert len(rows) == session["case_count"] == 12
    assert Counter(c["status"] for c in rows) == {"released": 5, "review_required": 4, "rejected": 2, "failed": 1}
    assert Counter(c["facility_id"] for c in rows) == {"demo-athens": 4, "demo-thessaloniki": 4, "demo-patras": 4}
    assert_no_jobs(store, client)
    for case in rows:
        assert case["duration_ms"] is None and case["origin"] == "prepared_demo"
        assert case["ttl"] == session["ttl"] and case["execution_id"] != f"{case['id']}:1"
        assert case["execution_id"] == case["prepared"]["original_execution_id"] == case["result"]["session_id"]
        assert all(r["session_id"] == case["execution_id"] for r in case["result"]["receipts"])
        packet = parse_packet(get_packet(case["packet_id"]))
        assert validate_result(packet, case["result"], case["execution_id"]) == case["artifact_hash"]
        if case["status"] == "released":
            patient = svc.patient(case["patient_url"].split("/")[-1])
            assert patient["case"]["origin"] == "prepared_demo"
            assert patient["case"]["prepared"] == case["prepared"]
            assert patient["result"]["model_mode"] == "fixture"
        else:
            assert case["patient_url"] is None
    boot = svc.bootstrap(session, pk)["collection"]
    assert boot == {"mode": "prepared_demo", "initialized": True, "seed_version": SEED_VERSION,
                    "prepared_count": 12, "live_count": 0}
    overview = svc.overview(pk)
    assert overview["durations"] == []
    assert overview["origin_counts"] == {"prepared_demo": 12, "live_run": 0, "unknown": 0}
    assert [r["count"] for r in overview["by_facility"]] == [4, 4, 4]
    assert svc.overview(pk, "demo-patras")["by_facility"][0]["count"] == 4
    if client:
        writes = client.transactions[0]
        assert len(writes) == 18  # session Update, twelve cases and five public shares
        assert "Update" in writes[0] and "ConditionCheck" not in writes[0]


def test_seed_request_race_is_idempotent_and_never_replaces_reviewed_cohort(backend):
    svc, store, client = backend
    pk, session = visitor(svc)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: svc.seed_demo(pk, session), range(8)))
    assert all(r["collection"]["prepared_count"] == 12 for r in results)
    before = copy.deepcopy(store.cases(pk))
    assert svc.seed_demo(pk, session)["collection"]["seed_version"] == SEED_VERSION
    assert store.cases(pk) == before and store.get(pk, "SESSION")["case_count"] == 12
    assert_no_jobs(store, client)


def test_two_visitors_get_distinct_case_capabilities_but_original_preparation_evidence(backend):
    svc, store, _ = backend
    first, _ = visitor(svc, True)
    second, _ = visitor(svc, True)
    a, b = store.cases(first), store.cases(second)
    assert {c["id"] for c in a}.isdisjoint(c["id"] for c in b)
    assert {c["patient_url"] for c in a if c["patient_url"]}.isdisjoint(
        c["patient_url"] for c in b if c["patient_url"])
    assert {c["execution_id"] for c in a} == {c["execution_id"] for c in b}
    with pytest.raises(OpsError) as error:
        svc.get_case(second, a[0]["id"])
    assert error.value.status == 404


def test_prepared_review_is_hash_bound_and_retry_really_runs_a_new_generation(backend, monkeypatch):
    monkeypatch.setenv("MEDPLAIN_MODEL_MODE", "fixture")
    svc, store, client = backend
    pk, session = visitor(svc, True)
    case = next(c for c in store.cases(pk) if c["status"] == "review_required")
    reviewer = {**session, "role": "reviewer"}
    with pytest.raises(OpsError) as error:
        svc.review(pk, reviewer, case["id"], "release", 1, case["source_hash"], "f" * 64, "test")
    assert error.value.status == 409
    released = svc.review(pk, reviewer, case["id"], "release", 1,
                          case["source_hash"], case["artifact_hash"], "Actual synthetic review of prepared content")
    assert released["status"] == "released" and released["origin"] == "prepared_demo"
    assert svc.patient(released["patient_url"].split("/")[-1])["case"]["prepared"] == case["prepared"]
    failed = next(c for c in store.cases(pk) if c["status"] == "failed")
    retry, created, job = svc.retry(pk, session, failed["id"], 1, "prepared-failure-retry")
    assert created and job == f"{failed['id']}:2"
    assert retry["origin"] == "live_run" and retry["prepared"] == failed["prepared"]
    assert retry["prepared_history"][0]["result"] == failed["result"]
    assert store.get(pk, f"OUTBOX#{failed['id']}#2")
    assert process_message({"session_key": pk, "case_id": failed["id"], "generation": 2}, store)
    current = svc.get_case(pk, failed["id"])
    assert current["status"] == "released" and current["execution_id"] == job
    assert current["result"]["session_id"] == job and current["duration_ms"] is not None
    assert current["prepared_history"][0]["execution_id"] != job
    collection = svc.bootstrap(store.get(pk, "SESSION"), pk)["collection"]
    assert collection["prepared_count"] == 11 and collection["live_count"] == 1
    assert len(svc.overview(pk)["durations"]) == 1


def test_seed_counter_still_enforces_twenty_case_limit_for_real_jobs(backend, monkeypatch):
    monkeypatch.setenv("MEDPLAIN_OPS_DAILY_LIMIT", "120")
    svc, store, _ = backend
    pk, session = visitor(svc, True)
    for index in range(8):
        svc.create(pk, session, "home-after-pneumonia", "demo-athens", False, f"remaining-{index}")
    with pytest.raises(OpsError) as error:
        svc.create(pk, session, "home-after-pneumonia", "demo-athens", False, "twenty-one")
    assert error.value.status == 429
    assert store.get(pk, "SESSION")["case_count"] == len(store.cases(pk)) == 20


def test_seed_never_overwrites_working_session_or_allows_read_only_role(backend):
    svc, store, _ = backend
    pk, session = visitor(svc)
    svc.create(pk, session, "home-after-pneumonia", "demo-athens", False, "working-cohort")
    before = copy.deepcopy(store.cases(pk))
    with pytest.raises(OpsError) as error:
        svc.seed_demo(pk, session)
    assert error.value.status == 409 and store.cases(pk) == before
    empty_pk, empty = visitor(svc)
    with pytest.raises(OpsError) as error:
        svc.seed_demo(empty_pk, {**empty, "role": "auditor"})
    assert error.value.status == 403 and store.cases(empty_pk) == []


@pytest.mark.parametrize("change", ["source", "artifact", "binding", "mode", "manifest", "missing"])
def test_tampered_or_missing_packaged_evidence_fails_closed_without_partial_rows(tmp_path, monkeypatch, change):
    import medplain.ops_seed as module

    path = tmp_path / "prepared.json"
    bundle = load_bundle()
    exemplar = bundle["exemplars"]["home-after-pneumonia"]
    if change == "source":
        exemplar["source"]["title"] = "Altered source"
    elif change == "artifact":
        exemplar["result"]["explanation_text"] += " Take 100 mg extra."
    elif change == "binding":
        exemplar["execution_id"] = "prepared:other:" + "a" * 32
    elif change == "mode":
        exemplar["result"]["model_mode"] = "bedrock"
    if change != "manifest":
        bundle["hash"] = canonical_hash({k: v for k, v in bundle.items() if k != "hash"})
    else:
        bundle["hash"] = "0" * 64
    if change != "missing":
        path.write_text(json.dumps(bundle), encoding="utf-8")
    original = module.load_bundle
    monkeypatch.setattr(module, "load_bundle", lambda: original(path))
    svc = OpsService(MemoryStore())
    pk, session = visitor(svc)
    before = copy.deepcopy(svc.store.rows)
    with pytest.raises(StoreUnavailable):
        svc.seed_demo(pk, session)
    assert svc.store.rows == before
    with pytest.raises(StoreUnavailable):
        svc.new_session(prepared_demo=True)
    assert svc.store.rows == before


def test_transaction_collision_rolls_back_counter_all_cases_and_patient_views(backend):
    svc, store, client = backend
    pk, session = visitor(svc, True)
    cases = store.cases(pk)
    fresh_pk, fresh = visitor(svc)
    shares = [store.get("SHARE#" + c["patient_url"].split("/")[-1], "VIEW")
              for c in cases if c["patient_url"]]
    before = copy.deepcopy(client.rows if client else store.rows)
    # The pre-existing share collision occurs after the session/case writes in
    # the DynamoDB transaction. All staged writes must roll back together.
    with pytest.raises(Conflict):
        store.seed(fresh_pk, cases, shares, session["collection"])
    assert (client.rows if client else store.rows) == before
    assert store.get(fresh_pk, "SESSION")["case_count"] == fresh["case_count"] == 0


def test_stale_role_payload_cannot_erase_atomic_seed_metadata():
    client = AtomicDynamo()
    store = DynamoStore("synthetic-ops", client)
    svc = OpsService(store)
    pk, stale = visitor(svc)
    svc.seed_demo(pk, stale)
    # Simulate the payload captured before the seed transaction by a concurrent
    # role request. Top-level metadata remains the authoritative read value.
    stale["role"] = "auditor"
    client.rows[(pk, "SESSION")]["payload"] = {"S": json.dumps(stale)}
    current = store.get(pk, "SESSION")
    assert current["role"] == "auditor" and current["case_count"] == 12
    assert current["collection"]["seed_version"] == SEED_VERSION
    assert svc.seed_demo(pk, {**current, "role": "admin"})["collection"]["prepared_count"] == 12


def test_size_guard_leaves_dynamodb_item_and_transaction_headroom():
    with pytest.raises(StoreUnavailable):
        check_seed_size([{"large": "x" * (301 * 1024)}] * 12, [{}] * 5)
    with pytest.raises(StoreUnavailable):
        check_seed_size([{"large": "x" * (190 * 1024)}] * 12, [{"large": "x" * (190 * 1024)}] * 5)
    with pytest.raises(StoreUnavailable):
        check_seed_size([{}] * 11, [{}] * 5)
    check_seed_size([{}] * 12, [{}] * 5)


def test_legacy_origin_is_unknown_and_legacy_collection_is_uninitialized():
    svc = OpsService(MemoryStore())
    pk, session = visitor(svc)
    session.pop("collection")
    case = svc.create(pk, session, "home-after-pneumonia", "demo-athens", False, "legacy-migration")[0]
    case.pop("origin")
    case.pop("prepared")
    case["duration_ms"] = 17
    svc.store.replace(pk, case, 1)
    assert summary(case)["origin"] == detail(case)["origin"] == "unknown"
    assert not svc.bootstrap(session, pk)["collection"]["initialized"]
    assert svc.overview(pk)["origin_counts"] == {"prepared_demo": 0, "live_run": 0, "unknown": 1}
    assert svc.overview(pk)["durations"] == []


def test_session_http_empty_compatibility_explicit_choices_and_seed_endpoint(monkeypatch):
    monkeypatch.setenv("MEDPLAIN_MODEL_MODE", "fixture")
    monkeypatch.delenv("MEDPLAIN_OPS_TABLE", raising=False)
    reset_store_cache()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        for body, initialized in [({}, False), ({"prepared_demo": False}, True)]:
            response = client.post("/api/ops/session", json=body)
            assert response.status_code == 201
            headers = {"X-Medplain-Session": response.json()["session_token"]}
            boot = client.get("/api/ops/bootstrap", headers=headers).json()
            assert boot["collection"]["mode"] == "empty" and boot["collection"]["initialized"] is initialized
            assert client.get("/api/ops/cases", headers=headers).json()["total"] == 0
        seeded = client.post("/api/ops/session", json={"prepared_demo": True})
        assert seeded.status_code == 201
        headers = {"X-Medplain-Session": seeded.json()["session_token"]}
        assert client.get("/api/ops/cases", headers=headers).json()["total"] == len(SCENARIOS) == 12
        for _ in range(2):
            response = client.post("/api/ops/session/demo", headers=headers, json={})
            assert response.status_code == 200 and response.json()["collection"]["prepared_count"] == 12
        assert client.post("/api/ops/session/demo", json={}).status_code == 401
        assert client.post("/api/ops/session", json={"source_text": "private input"}).status_code == 422
    reset_store_cache()
