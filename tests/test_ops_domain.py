"""Durable operations invariants, including adversarial publication boundaries."""

import copy
import time

import pytest

from medplain.clinical import build_explanation, check_interactions, render_explanation
from medplain.gate import ReceiptChain, SafetyGate
from medplain.ops_domain import LeaseBusy, OpsError, OpsService, detail, outbox_for
from medplain.ops_store import Conflict, MemoryStore
from medplain.schemas import ClinicalPacket


@pytest.fixture
def cohort():
    svc = OpsService(MemoryStore())
    visitor = svc.new_session()
    pk, session = svc.authenticate(visitor["session_token"])
    return svc, pk, session, visitor


def create(cohort, packet="home-after-pneumonia", facility="demo-athens", key="first-request", stress=False):
    svc, pk, session, _ = cohort
    return svc.create(pk, session, packet, facility, stress, key)[0]


def evidence(claimed, corrected=False):
    """Independent deterministic fixture, not a model or production result."""
    packet = ClinicalPacket.model_validate(claimed["source"])
    candidate = build_explanation(packet)
    chain = ReceiptChain(claimed["execution_id"], packet)
    if corrected:
        changed = candidate.model_copy(deep=True)
        changed.medications[0].dose = "999"
        changed.medications[0].text = "Unsupported changed medicine"
        chain.append(changed, SafetyGate(packet).validate(changed))
    chain.append(candidate, SafetyGate(packet).validate(candidate))
    return {"status": "verified", "explanation": candidate.model_dump(),
            "explanation_text": render_explanation(candidate), "receipts": chain.receipts,
            "interactions": check_interactions(packet), "correction_count": int(corrected),
            "session_id": claimed["execution_id"], "model_mode": "fixture", "error": None,
            "trace": [{"agent": "AtroposClinicalSafetyGate", "status": "verified"}]}


def finish(cohort, case, corrected=False):
    svc, pk, _, _ = cohort
    claimed = svc.claim(pk, case["id"], case["generation"])
    assert svc.complete(pk, claimed, evidence(claimed, corrected), 123)
    return svc.get_case(pk, case["id"])


def test_session_token_is_hashed_and_expiration_is_enforced(cohort):
    svc, pk, session, visitor = cohort
    assert visitor["session_token"] not in repr(svc.store.rows)
    assert pk.startswith("SESSION#") and len(pk) == 72
    assert session["roles"] == ["operator", "reviewer", "auditor", "admin"]
    assert svc.bootstrap(session)["limits"]["max_cases"] == 20
    assert len(svc.bootstrap(session)["facilities"]) == 3
    for token in (None, "short", "x" * 43):
        with pytest.raises(OpsError) as error:
            svc.authenticate(token)
        assert error.value.status == 401
    svc.store.rows[(pk, "SESSION")]["ttl"] = int(time.time()) - 1
    with pytest.raises(OpsError, match=""):
        svc.authenticate(visitor["session_token"])


def test_create_transaction_has_outbox_and_idempotent_request_fingerprint(cohort):
    svc, pk, session, _ = cohort
    case = create(cohort)
    assert svc.store.get(pk, outbox_for(case)["sk"])["case_id"] == case["id"]
    replay, created, job = svc.create(pk, session, "home-after-pneumonia", "demo-athens", False, "first-request")
    assert not created and replay["id"] == case["id"] and job.endswith(":1")
    assert svc.store.get(pk, "SESSION")["case_count"] == 1
    with pytest.raises(OpsError) as conflict:
        svc.create(pk, session, "dosage-mutation-stress", "demo-athens", True, "first-request")
    assert conflict.value.status == 409
    assert detail(case)["source"]["fhir"]["resourceType"] == "Bundle"


def test_session_case_limit_is_atomic_even_with_stale_session_snapshot(cohort):
    svc, pk, session, _ = cohort
    for index in range(20):
        create(cohort, key=f"request-{index}")
    with pytest.raises(OpsError) as error:
        create(cohort, key="over-the-limit")
    assert error.value.status == 429
    # A replay still works at the limit, including after the session count refresh.
    refreshed = svc.store.get(pk, "SESSION")
    assert not svc.create(pk, refreshed, "home-after-pneumonia", "demo-athens", False, "request-0")[1]
    with pytest.raises(OpsError) as error:
        svc.create(pk, refreshed, "home-after-pneumonia", "demo-athens", False, "new-key-at-limit")
    assert error.value.status == 429
    assert len(svc.store.cases(pk)) == 20 and session["case_count"] == 0


@pytest.mark.parametrize("packet,facility,stress,status", [
    ("missing", "demo-athens", False, 404),
    ("home-after-pneumonia", "real-hospital", False, 422),
    ("home-after-pneumonia", "demo-athens", True, 422),
])
def test_only_fixed_synthetic_source_selectors(cohort, packet, facility, stress, status):
    with pytest.raises(OpsError) as error:
        create(cohort, packet, facility, stress=stress)
    assert error.value.status == status


def test_whole_cohort_search_filter_sort_and_nonzero_page_offset(cohort):
    svc, pk, _, _ = cohort
    first = create(cohort, key="request-1")
    middle = create(cohort, packet="dosage-mutation-stress", facility="demo-patras", key="request-2")
    last = create(cohort, facility="demo-thessaloniki", key="request-3")
    page = svc.list_cases(pk, direction="asc", limit=1)
    assert page["items"][0]["id"] == first["id"] and page["total"] == 3
    second = svc.list_cases(pk, direction="asc", limit=1, cursor=page["next_cursor"])
    assert second["items"][0]["id"] == middle["id"]
    third = svc.list_cases(pk, direction="asc", limit=1, cursor=second["next_cursor"])
    assert third["items"][0]["id"] == last["id"] and third["next_cursor"] is None
    assert svc.list_cases(pk, q=last["id"])["total"] == 1
    assert svc.list_cases(pk, q="DOSAGE")["items"][0]["id"] == middle["id"]
    assert svc.list_cases(pk, workspace="demo-patras", status="queued")["total"] == 1
    assert svc.list_cases(pk, sort="duration_ms")["total"] == 3
    with pytest.raises(OpsError):
        svc.list_cases(pk, direction="desc", cursor=page["next_cursor"])


@pytest.mark.parametrize("arguments", [
    {"workspace": "wrong"}, {"status": "ready"}, {"sort": "source"},
    {"direction": "wrong"}, {"limit": 30}, {"cursor": "not-base64"},
])
def test_invalid_list_queries_are_visible_errors(cohort, arguments):
    svc, pk, _, _ = cohort
    with pytest.raises(OpsError) as error:
        svc.list_cases(pk, **arguments)
    assert error.value.status == 422


def test_duplicate_delivery_active_lease_and_old_attempt_fencing(cohort):
    svc, pk, _, _ = cohort
    case = create(cohort)
    first = svc.claim(pk, case["id"], 1)
    with pytest.raises(LeaseBusy):
        svc.claim(pk, case["id"], 1)
    svc.store.rows[(pk, "CASE#" + case["id"])]["lease_until"] = 0
    second = svc.claim(pk, case["id"], 1)
    assert second["lease_owner"] != first["lease_owner"]
    assert not svc.complete(pk, first, evidence(first), 111)
    assert svc.complete(pk, second, evidence(second), 222)
    assert svc.claim(pk, case["id"], 1) is None
    assert svc.claim(pk, case["id"], 2) is None
    assert not svc.complete(pk, second, evidence(second), 222)
    assert svc.claim(pk, "0" * 32, 1) is None


def test_clean_artifact_auto_releases_private_capability_and_real_metrics(cohort):
    svc, pk, _, visitor = cohort
    case = finish(cohort, create(cohort))
    assert case["status"] == "released" and case["duration_ms"] == 123
    share_id = case["patient_url"].split("/")[-1]
    public = svc.patient(share_id)
    assert public["case"]["artifact_hash"] == case["artifact_hash"]
    assert public["result"]["explanation"] == case["result"]["explanation"]
    assert visitor["session_token"] not in repr(public) and pk not in repr(public)
    assert "session_id" not in public["result"]
    assert "runtime" not in public["result"]
    overview = svc.overview(pk)
    assert overview["counts"]["total"] == overview["counts"]["released"] == 1
    assert overview["durations"][0]["duration_ms"] == 123
    assert svc.overview(pk, "demo-patras")["counts"]["total"] == 0
    with pytest.raises(OpsError):
        svc.overview(pk, "unknown")


def test_corrected_artifact_requires_exact_hash_bound_reviewer_release(cohort):
    svc, pk, session, _ = cohort
    case = finish(cohort, create(cohort), corrected=True)
    assert case["status"] == "review_required" and not case["patient_url"]
    args = (case["id"], "release", case["version"], case["source_hash"], case["artifact_hash"], "Reviewed demo evidence")
    with pytest.raises(OpsError) as denied:
        svc.review(pk, session, *args)
    assert denied.value.status == 403
    svc.role(pk, "reviewer")
    reviewer = svc.store.get(pk, "SESSION")
    for field in (2, 3, 4):
        changed = list(args)
        changed[field] = 1 if field == 2 else "0" * 64
        with pytest.raises(OpsError) as stale:
            svc.review(pk, reviewer, *changed)
        assert stale.value.status == 409
    released = svc.review(pk, reviewer, *args)
    assert released["status"] == "released" and released["review"]["reviewed_version"] == case["version"]
    assert svc.patient(released["patient_url"].split("/")[-1])["case"]["status"] == "released"
    with pytest.raises(OpsError):
        svc.review(pk, reviewer, *args)


def test_interactions_require_review_even_without_correction(cohort):
    case = create(cohort, "dosage-mutation-stress")
    finished = finish(cohort, case)
    assert finished["correction_count"] == 0
    assert finished["status"] == "review_required" and finished["result"]["interactions"]


def test_invalid_persisted_candidate_cannot_be_released_or_shared(cohort):
    svc, pk, _, _ = cohort
    case = create(cohort)
    claimed = svc.claim(pk, case["id"], 1)
    result = evidence(claimed)
    result["explanation"]["medications"][0]["dose"] = "999"
    assert svc.complete(pk, claimed, result, 21)
    failed = svc.get_case(pk, case["id"])
    assert failed["status"] == "failed" and failed["result"] is None and not failed["patient_url"]
    with pytest.raises(OpsError):
        svc.review(pk, {"role": "admin"}, case["id"], "release", failed["version"], failed["source_hash"], "0" * 64, "")


def test_tampering_after_processing_is_caught_at_review_and_public_read(cohort):
    svc, pk, _, _ = cohort
    case = finish(cohort, create(cohort), corrected=True)
    svc.store.rows[(pk, "CASE#" + case["id"])]["result"]["explanation_text"] = "Changed narrative"
    with pytest.raises(OpsError) as error:
        svc.review(pk, {"role": "admin"}, case["id"], "release", case["version"],
                   case["source_hash"], case["artifact_hash"], "")
    assert error.value.status == 409
    clean = finish(cohort, create(cohort, key="second-clean"))
    share_id = clean["patient_url"].split("/")[-1]
    svc.store.rows[("SHARE#" + share_id, "VIEW")]["source"]["summary"] = "Changed source"
    with pytest.raises(OpsError) as error:
        svc.patient(share_id)
    assert error.value.status == 409
    for missing in ("short", "x" * 43):
        with pytest.raises(OpsError) as error:
            svc.patient(missing)
        assert error.value.status == 404


def test_failed_job_retry_generations_idempotency_and_original_worker_fence(cohort):
    svc, pk, session, _ = cohort
    case = create(cohort)
    first = svc.claim(pk, case["id"], 1)
    svc.complete(pk, first, None, 42)
    failed = svc.get_case(pk, case["id"])
    retry, created, job = svc.retry(pk, session, case["id"], failed["version"], "retry-request")
    assert created and retry["generation"] == 2 and job.endswith(":2")
    assert svc.store.get(pk, outbox_for(retry)["sk"])
    assert not svc.retry(pk, session, case["id"], failed["version"], "retry-request")[1]
    assert not svc.complete(pk, first, evidence(first), 10)
    assert svc.claim(pk, case["id"], 1) is None
    with pytest.raises(OpsError):
        svc.retry(pk, session, case["id"], failed["version"] + 1, "retry-request")
    with pytest.raises(OpsError):
        svc.retry(pk, session, case["id"], failed["version"], "another-retry")
    with pytest.raises(OpsError):
        svc.retry(pk, session, case["id"], retry["version"], "queued-retry")
    for generation in (2, 3):
        claimed = svc.claim(pk, case["id"], generation)
        svc.complete(pk, claimed, None, 10)
        failed = svc.get_case(pk, case["id"])
        if generation == 2:
            svc.retry(pk, session, case["id"], failed["version"], "third-generation")
    with pytest.raises(OpsError) as error:
        svc.retry(pk, session, case["id"], failed["version"], "fourth-generation")
    assert "three" in error.value.message


def test_rejection_is_not_publication_and_all_reads_are_owner_scoped(cohort):
    svc, pk, session, _ = cohort
    case = finish(cohort, create(cohort), corrected=True)
    other = svc.new_session()
    other_pk, _ = svc.authenticate(other["session_token"])
    assert not svc.list_cases(other_pk)["items"]
    with pytest.raises(OpsError) as error:
        svc.get_case(other_pk, case["id"])
    assert error.value.status == 404
    rejected = svc.review(pk, {"role": "admin"}, case["id"], "reject", case["version"],
                          case["source_hash"], case["artifact_hash"], "Reprocess this synthetic packet")
    assert rejected["status"] == "rejected" and rejected["patient_url"] is None
    assert svc.retry(pk, session, case["id"], rejected["version"], "retry-rejection")[0]["status"] == "queued"


def test_auditor_cannot_mutate_and_role_is_own_demo_lens(cohort):
    svc, pk, _, _ = cohort
    svc.role(pk, "auditor")
    session = svc.store.get(pk, "SESSION")
    assert session["role"] == "auditor"
    with pytest.raises(OpsError):
        svc.create(pk, session, "home-after-pneumonia", "demo-athens", False, "auditor-attempt")
    with pytest.raises(OpsError):
        svc.retry(pk, session, "0" * 32, 1, "auditor-attempt")
    with pytest.raises(OpsError):
        svc.role(pk, "doctor")


def test_memory_transactions_do_not_partially_commit_on_conflict(cohort):
    svc, pk, _, _ = cohort
    case = create(cohort)
    changed = copy.deepcopy(case)
    changed["status"] = "failed"
    with pytest.raises(Conflict):
        svc.store.replace(pk, changed, expected_version=999, outbox={"sk": "OUTBOX#new", "ttl": case["ttl"]})
    assert svc.get_case(pk, case["id"])["status"] == "queued"
    assert svc.store.get(pk, "OUTBOX#new") is None
    with pytest.raises(Conflict):
        svc.store.put_new(pk, "CASE#" + case["id"], case)


def test_global_daily_allowance_is_atomic_and_idempotent(cohort, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from medplain.ops_store import daily_allowance

    monkeypatch.setenv("MEDPLAIN_OPS_DAILY_LIMIT", "3")
    svc, pk, session, _ = cohort
    first = create(cohort)
    assert not svc.create(pk, session, first["packet_id"], first["facility_id"], False, "first-request")[1]

    def attempt(index):
        try:
            create(cohort, key=f"concurrent-{index}")
            return True
        except OpsError as exc:
            assert exc.status == 429 and "00:00 UTC" in exc.message
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(attempt, range(12))) == 2
    assert len(svc.store.cases(pk)) == 3
    assert svc.store.rows[(daily_allowance()[0], "JOBS")]["used"] == 3
    claimed = svc.claim(pk, first["id"], 1)
    svc.complete(pk, claimed, None, 2)
    failed = svc.get_case(pk, first["id"])
    with pytest.raises(OpsError) as error:
        svc.retry(pk, session, first["id"], failed["version"], "daily-limit-retry")
    assert error.value.status == 429
    assert svc.get_case(pk, first["id"])["version"] == failed["version"]
    assert svc.store.get(pk, f"OUTBOX#{first['id']}#2") is None


def test_retry_duplicate_does_not_consume_daily_allowance(cohort, monkeypatch):
    from medplain.ops_store import daily_allowance

    monkeypatch.setenv("MEDPLAIN_OPS_DAILY_LIMIT", "2")
    svc, pk, session, _ = cohort
    case = create(cohort)
    claimed = svc.claim(pk, case["id"], 1)
    svc.complete(pk, claimed, None, 1)
    failed = svc.get_case(pk, case["id"])
    svc.retry(pk, session, case["id"], failed["version"], "bounded-retry")
    assert not svc.retry(pk, session, case["id"], failed["version"], "bounded-retry")[1]
    assert svc.store.rows[(daily_allowance()[0], "JOBS")]["used"] == 2


def test_packaged_commit_precedes_environment_and_source_only_fallback(tmp_path, monkeypatch):
    import json

    import medplain.ops_domain as module

    monkeypatch.setattr(module, "__file__", str(tmp_path / "ops_domain.py"))
    monkeypatch.setenv("MEDPLAIN_COMMIT", "environment-new-code")
    assert module.commit() == "environment-new-code"
    (tmp_path / "_build.json").write_text(json.dumps({"commit": "packaged-old-code"}), encoding="utf-8")
    assert module.commit() == "packaged-old-code"
    monkeypatch.delenv("MEDPLAIN_COMMIT")
    assert module.commit() == "packaged-old-code"
    (tmp_path / "_build.json").unlink()
    assert module.commit() == "development"
