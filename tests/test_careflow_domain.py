"""Owned workflow, transaction, capability and stale-worker counterexamples."""

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from medplain.careflow_domain import CareflowService
from medplain.careflow_store import CareflowStore
from medplain.ops_domain import LeaseBusy, OpsError, OpsService
from medplain.ops_store import Conflict, DynamoStore, MemoryStore, daily_allowance
from medplain.ops_worker import process_message
from medplain.reconciliation import examples


@pytest.fixture
def system(monkeypatch):
    monkeypatch.setenv("MEDPLAIN_MODEL_MODE", "fixture")
    monkeypatch.setenv("MEDPLAIN_OPS_DAILY_LIMIT", "120")
    store = MemoryStore()
    ops = OpsService(store)
    pk, session = ops.authenticate(ops.new_session(False)["session_token"])
    service = CareflowService(store)
    return service, ops, pk, session


def role(system, value):
    _, ops, pk, _ = system
    ops.role(pk, value)


def completed(system, *, parent=None, released=True, alias=None, facility="demo-athens", changed=True):
    _, ops, pk, session = system
    source = copy.deepcopy(parent["source"] if parent else examples()[0]["dossier"])
    if parent and changed:
        source["documents"][-1]["text"] += "\nSynthetic clinician addendum: the original questions were reviewed."
    if alias:
        source["patient_alias"] = alias
    role(system, "operator")
    case = ops.reconcile_case(pk, {**session, "role": "operator"}, source, True,
                              facility, uuid4().hex, parent_case_id=parent["id"] if parent else None)[0]
    assert process_message({"session_key": pk, "case_id": case["id"], "generation": 1}, ops.store)
    case = ops.get_case(pk, case["id"])
    assert case["status"] == "review_required"
    role(system, "reviewer")
    if released:
        case = ops.review(pk, {**session, "role": "reviewer"}, case["id"], "release", case["version"],
                          case["source_hash"], case["artifact_hash"], "Synthetic review only.")
    return case


def command(system, case_id, **values):
    _, ops, pk, _ = system
    case = ops.get_case(pk, case_id)
    return {"expected_version": case["version"], "source_hash": case["source_hash"],
            "artifact_hash": case["artifact_hash"], "idempotency_key": uuid4().hex, **values}


def actor(system, actor_id="demo-pharmacist"):
    svc, _, pk, session = system
    prior = svc.actors(pk, session)
    return svc.select_actor(pk, session, actor_id, prior["actor_version"])


def task(system, case):
    svc, _, pk, session = system
    return svc.view(pk, session, case["id"])["tasks"][0]


def assigned(system, case):
    svc, _, pk, session = system
    item = task(system, case)
    due = datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat()
    svc.assign(pk, session, case["id"], command(system, case["id"], task_id=item["id"],
                                              assignee_id="demo-pharmacist", due_at=due))
    return item


def answered(system, case):
    svc, _, pk, session = system
    item = assigned(system, case)
    actor(system)
    svc.accept(pk, session, case["id"], command(system, case["id"], task_id=item["id"], actor_id="demo-pharmacist"))
    svc.answer(pk, session, case["id"], command(system, case["id"], task_id=item["id"], actor_id="demo-pharmacist",
                                               answer_text="Private invented workflow answer, not a source instruction."))
    return item


def enabled(system, case):
    svc, _, pk, session = system
    reply = svc.enable_teachback(pk, session, case["id"], command(system, case["id"]))
    return case["patient_url"].rsplit("/", 1)[-1], reply["response_capability"], reply


def response(svc, share, cap, **updates):
    view = svc.patient_view(share, cap)
    return {"expected_version": view["case_version"], "prompt_id": view["prompt"]["prompt_id"],
            "response_text": "I will ask the discharge team to clarify these source instructions.",
            "synthetic_confirmed": True, "idempotency_key": uuid4().hex, **updates}


def usage(store):
    key, _, _ = daily_allowance()
    return (store.get(key, "JOBS") or {}).get("used", 0)


def test_assignment_acceptance_answer_are_not_closure_and_preserve_original_evidence(system):
    svc, ops, pk, session = system
    case = completed(system)
    original = copy.deepcopy(case)
    item = answered(system, case)
    current = ops.get_case(pk, case["id"])
    state = svc.view(pk, session, case["id"])
    saved = next(t for t in state["tasks"] if t["id"] == item["id"])
    assert saved["status"] == "answered" and saved["answer"]["authoritative"] is False
    assert saved["closure"] is None and saved["revision"] is None
    assert [e["type"] for e in saved["events"]] == ["careflow_assigned", "careflow_accepted", "careflow_answered"]
    for key in ("source", "result", "source_hash", "artifact_hash", "patient_url", "status", "generation"):
        assert current[key] == original[key]
    public = ops.patient(case["patient_url"].rsplit("/", 1)[-1])
    assert "Private invented workflow answer" not in json.dumps(public)
    assert "careflow" not in public and current["version"] == original["version"] + 3


def test_only_selected_assignee_can_accept_and_only_after_explicit_assignment(system):
    svc, _, pk, session = system
    case = completed(system)
    item = task(system, case)
    actor(system)
    with pytest.raises(OpsError) as error:
        svc.accept(pk, session, case["id"], command(system, case["id"], task_id=item["id"], actor_id="demo-pharmacist"))
    assert error.value.status == 409
    assigned(system, case)
    actor(system, "demo-followup-nurse")
    with pytest.raises(OpsError) as error:
        svc.accept(pk, session, case["id"], command(system, case["id"], task_id=item["id"], actor_id="demo-pharmacist"))
    assert error.value.status == 403
    with pytest.raises(OpsError) as error:
        svc.accept(pk, session, case["id"], command(system, case["id"], task_id=item["id"], actor_id="demo-followup-nurse"))
    assert error.value.status == 403


def test_role_is_reloaded_and_cannot_be_forged_by_stale_session(system):
    svc, _, pk, session = system
    case = completed(system)
    item = assigned(system, case)
    actor(system)
    role(system, "auditor")
    with pytest.raises(OpsError) as error:
        svc.accept(pk, {**session, "role": "admin"}, case["id"],
                   command(system, case["id"], task_id=item["id"], actor_id="demo-pharmacist"))
    assert error.value.status == 403


def test_owner_isolation_and_actor_compare_and_swap(system):
    svc, ops, pk, session = system
    case = completed(system)
    other, outsider = ops.authenticate(ops.new_session(False)["session_token"])
    assert svc.actors(other, outsider)["selected_actor_id"] is None
    with pytest.raises(OpsError) as error:
        svc.view(other, outsider, case["id"])
    assert error.value.status == 404
    actor(system)
    with pytest.raises(OpsError) as error:
        svc.select_actor(pk, session, "demo-followup-nurse", 0)
    assert error.value.status == 409
    assert svc.actors(pk, session)["selected_actor_id"] == "demo-pharmacist"


def test_same_command_replays_but_conflicting_key_and_stale_version_fail(system):
    svc, ops, pk, session = system
    case = completed(system)
    item = task(system, case)
    body = command(system, case["id"], task_id=item["id"], assignee_id="demo-pharmacist",
                   due_at=datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat())
    first = svc.assign(pk, session, case["id"], body)
    assert svc.assign(pk, session, case["id"], body)["case_version"] == first["case_version"]
    for changed in ({"assignee_id": "demo-followup-nurse"}, {"idempotency_key": uuid4().hex}, {"source_hash": "0" * 64}):
        with pytest.raises(OpsError) as error:
            svc.assign(pk, session, case["id"], {**body, **changed})
        assert error.value.status == 409
    assert ops.get_case(pk, case["id"])["version"] == case["version"] + 1


@pytest.mark.parametrize("due", ["2026-11-01T12:00:00", "not-a-time", "2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z"])
def test_due_date_is_timezone_explicit_and_session_bounded(system, due):
    svc, _, pk, session = system
    case = completed(system)
    with pytest.raises(OpsError) as error:
        svc.assign(pk, session, case["id"], command(system, case["id"], task_id=task(system, case)["id"],
                                                  assignee_id="demo-pharmacist", due_at=due))
    assert error.value.status == 422


def test_only_verified_direct_same_patient_revision_allows_explicit_closure(system):
    svc, ops, pk, session = system
    case = completed(system)
    item = answered(system, case)
    with pytest.raises(OpsError):
        svc.close(pk, session, case["id"], command(system, case["id"], task_id=item["id"], actor_id="demo-pharmacist",
                                                  clinician_confirmed=True, revision_source_hash=case["source_hash"],
                                                  revision_artifact_hash=case["artifact_hash"]))
    revision = completed(system, parent=case)
    before = copy.deepcopy(ops.get_case(pk, case["id"]))
    svc.link_revision(pk, session, case["id"], command(system, case["id"], task_id=item["id"],
                                                    actor_id="demo-pharmacist", revision_case_id=revision["id"]))
    close = command(system, case["id"], task_id=item["id"], actor_id="demo-pharmacist", clinician_confirmed=False,
                    revision_source_hash=revision["source_hash"], revision_artifact_hash=revision["artifact_hash"])
    with pytest.raises(OpsError):
        svc.close(pk, session, case["id"], close)
    state = svc.close(pk, session, case["id"], {**close, "clinician_confirmed": True})
    saved = next(t for t in state["tasks"] if t["id"] == item["id"])
    assert saved["status"] == "closed" and saved["closure"]["revision_case_id"] == revision["id"]
    assert ops.get_case(pk, case["id"])["source"] == before["source"]
    assert ops.get_case(pk, case["id"])["result"] == before["result"]


@pytest.mark.parametrize("changes", [{"changed": False}, {"alias": "Demo-Another"}, {"facility": "demo-patras"}])
def test_unrelated_or_identical_revision_cannot_clear_answer(system, changes):
    svc, _, pk, session = system
    case = completed(system)
    item = answered(system, case)
    revision = completed(system, parent=case, **changes)
    with pytest.raises(OpsError) as error:
        svc.link_revision(pk, session, case["id"], command(system, case["id"], task_id=item["id"],
                                                         actor_id="demo-pharmacist", revision_case_id=revision["id"]))
    assert error.value.status == 409


def test_child_changes_during_closure_transaction_never_commit(system, monkeypatch):
    svc, ops, pk, session = system
    case = completed(system)
    item = answered(system, case)
    revision = completed(system, parent=case)
    svc.link_revision(pk, session, case["id"], command(system, case["id"], task_id=item["id"],
                                                    actor_id="demo-pharmacist", revision_case_id=revision["id"]))
    real = svc.rows.close

    def raced(owner, current, expected, child):
        with ops.store.lock:
            ops.store.rows[(pk, "CASE#" + child["id"])]["version"] += 1
        return real(owner, current, expected, child)

    monkeypatch.setattr(svc.rows, "close", raced)
    with pytest.raises(OpsError) as error:
        svc.close(pk, session, case["id"], command(system, case["id"], task_id=item["id"], actor_id="demo-pharmacist",
                                                  clinician_confirmed=True, revision_source_hash=revision["source_hash"],
                                                  revision_artifact_hash=revision["artifact_hash"]))
    assert error.value.status == 409
    assert next(t for t in svc.view(pk, session, case["id"])["tasks"] if t["id"] == item["id"])["status"] == "revision_linked"


@pytest.mark.parametrize("field", ["source", "result", "task", "generation"])
def test_tampering_or_stale_workflow_binding_is_rejected(system, field):
    svc, ops, pk, session = system
    case = completed(system)
    assigned(system, case)
    saved = ops.store.rows[(pk, "CASE#" + case["id"])]
    if field == "source":
        saved["source"]["documents"][0]["text"] += " Altered."
    elif field == "result":
        saved["result"]["artifact"]["patient_regimen"] = "Invented dose"
    elif field == "task":
        saved["careflow"]["tasks"][0]["prompt"] = "Invented clinical advice"
    else:
        saved["careflow"]["generation"] += 1
    with pytest.raises(OpsError) as error:
        svc.view(pk, session, case["id"])
    assert error.value.status == 409


def test_readonly_share_is_not_write_capability_and_cap_is_only_returned_on_enable(system):
    svc, ops, pk, session = system
    case = completed(system)
    item = answered(system, case)
    share, cap, reply = enabled(system, case)
    for wrong_share, wrong_cap in ((share, None), (share, share), (share, "0" * 64), ("different-share-" + "a" * 32, cap)):
        with pytest.raises(OpsError) as error:
            svc.patient_view(wrong_share, wrong_cap)
        assert error.value.status == 403
    public = svc.patient_view(share, cap)
    for value in (cap, pk, session["session_id"], "Private invented workflow answer"):
        assert value not in json.dumps(public)
    assert all(cap not in json.dumps(row) for row in ops.store.rows.values())
    assert "#teachback=" + cap in reply["response_url"]
    assert "capability_hash" not in json.dumps(reply["careflow"])
    assert next(t for t in reply["careflow"]["tasks"] if t["id"] == item["id"])["status"] == "answered"


def test_enabling_requires_released_case_and_roles_and_is_idempotent(system):
    svc, ops, pk, session = system
    case = completed(system, released=False)
    with pytest.raises(OpsError) as error:
        svc.enable_teachback(pk, session, case["id"], command(system, case["id"]))
    assert error.value.status == 409
    case = ops.review(pk, {**session, "role": "reviewer"}, case["id"], "release", case["version"],
                      case["source_hash"], case["artifact_hash"], "Synthetic release.")
    body = command(system, case["id"])
    first = svc.enable_teachback(pk, session, case["id"], body)
    again = svc.enable_teachback(pk, session, case["id"], body)
    assert again["response_capability"] == first["response_capability"]
    assert again["careflow"]["case_version"] == first["careflow"]["case_version"]
    share = case["patient_url"].rsplit("/", 1)[-1]
    second = svc.enable_teachback(pk, session, case["id"], command(system, case["id"]))
    assert second["response_capability"] != first["response_capability"]
    with pytest.raises(OpsError):
        svc.patient_view(share, first["response_capability"])


def test_response_outbox_shared_allowance_and_lost_reply_replay_are_atomic(system, monkeypatch):
    svc, ops, pk, _ = system
    case = completed(system)
    share, cap, _ = enabled(system, case)
    body = response(svc, share, cap)
    before = usage(ops.store)
    monkeypatch.setenv("MEDPLAIN_OPS_DAILY_LIMIT", str(before + 1))
    view, created, job, message = svc.submit_response(share, cap, body)
    assert created and usage(ops.store) == before + 1
    again, duplicate, same_job, same_message = svc.submit_response(share, cap, body)
    assert not duplicate and same_job == job and same_message == message and usage(ops.store) == before + 1
    assert len(again["attempts"]) == len(view["attempts"]) == 1
    assert set(message) == {"session_key", "case_id", "generation", "attempt_id", "job_kind"}
    outbox = ops.store.get(pk, "OUTBOX#CARE#" + case["id"] + "#" + message["attempt_id"])
    assert outbox["job_kind"] == "careflow_teachback" and "response_text" not in outbox
    assert "source" not in outbox and "artifact" not in outbox and cap not in json.dumps(outbox)
    assert ops.get_case(pk, case["id"])["status"] == "released"


def test_concurrent_duplicate_response_is_one_reservation_and_one_attempt(system):
    svc, ops, _, _ = system
    case = completed(system)
    share, cap, _ = enabled(system, case)
    body = response(svc, share, cap)
    before = usage(ops.store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda _: svc.submit_response(share, cap, body), range(2)))
    assert sum(row[1] for row in rows) == 1
    assert rows[0][2] == rows[1][2] and usage(ops.store) == before + 1


def test_active_expired_and_superseded_leases_never_overwrite_and_terminal_duplicate_is_harmless(system):
    svc, ops, pk, _ = system
    case = completed(system)
    share, cap, _ = enabled(system, case)
    _, _, _, msg = svc.submit_response(share, cap, response(svc, share, cap))
    claim = svc.claim_teachback(pk, case["id"], msg["attempt_id"], 1)
    assert claim["execution_id"] == case["id"] + ":teachback:" + msg["attempt_id"]
    with pytest.raises(LeaseBusy):
        svc.claim_teachback(pk, case["id"], msg["attempt_id"], 1)
    stored = ops.store.rows[(pk, "CASE#" + case["id"])]["careflow"]["teachback"]["attempts"][0]
    stored["lease_until"] = int(time.time()) - 1
    with pytest.raises(LeaseBusy):
        svc.complete_teachback(pk, claim, None, error="Secret provider message")
    replacement = svc.claim_teachback(pk, case["id"], msg["attempt_id"], 1)
    with pytest.raises(LeaseBusy):
        svc.complete_teachback(pk, claim, None, error="Secret provider message")
    assert svc.complete_teachback(pk, replacement, None, error="Secret provider message")
    assert svc.claim_teachback(pk, case["id"], msg["attempt_id"], 1) is None
    assert not svc.complete_teachback(pk, replacement, None)
    public = svc.patient_view(share, cap)
    assert public["attempts"][0]["status"] == "failed"
    assert "Secret provider" not in json.dumps(public)


def test_followup_requires_explicit_reviewer_even_failed_and_cap_rotation_does_not_reset_limit(system):
    svc, ops, pk, session = system
    case = completed(system)
    share, cap, _ = enabled(system, case)
    actor(system)
    for index in range(3):
        _, _, _, msg = svc.submit_response(share, cap, response(svc, share, cap))
        claim = svc.claim_teachback(pk, case["id"], msg["attempt_id"], 1)
        assert svc.complete_teachback(pk, claim, None, error="withheld")
        with pytest.raises(OpsError) as error:
            svc.submit_response(share, cap, response(svc, share, cap))
        assert error.value.status == (429 if index == 2 else 409)
        body = command(system, case["id"], actor_id="demo-pharmacist", decision="acknowledged", comment="Private note")
        with pytest.raises(OpsError):
            svc.review_teachback(pk, session, case["id"], msg["attempt_id"], body)
        svc.review_teachback(pk, session, case["id"], msg["attempt_id"], {**body, "decision": "follow_up"})
        new = svc.enable_teachback(pk, session, case["id"], command(system, case["id"]))
        cap = new["response_capability"]
    with pytest.raises(OpsError) as error:
        svc.submit_response(share, cap, response(svc, share, cap))
    assert error.value.status == 429
    public = svc.patient_view(share, cap)
    assert len(public["attempts"]) == 3
    assert all(set(a["review"]) == {"decision", "at"} for a in public["attempts"])
    assert "Private note" not in json.dumps(public)
    assert all(t["status"] == "open" for t in svc.view(pk, session, case["id"])["tasks"])


def test_bad_assessment_is_withheld_and_does_not_mutate_released_artifact(system, monkeypatch):
    svc, ops, pk, _ = system
    case = completed(system)
    share, cap, _ = enabled(system, case)
    _, _, _, msg = svc.submit_response(share, cap, response(svc, share, cap))
    claim = svc.claim_teachback(pk, case["id"], msg["attempt_id"], 1)

    def reject(*args):
        raise ValueError("Wrong source hash and fabricated clinical advice")

    monkeypatch.setattr("medplain.careflow_domain._validate_assessment", reject)
    assert svc.complete_teachback(pk, claim, {"patient_regimen": "Invented dose"})
    attempt = svc.patient_view(share, cap)["attempts"][0]
    assert attempt["status"] == "failed" and attempt["assessment"] is None
    assert attempt["error"] == "assessment_integrity_failed"
    saved = ops.get_case(pk, case["id"])
    assert saved["result"] == case["result"] and saved["artifact_hash"] == case["artifact_hash"]


def test_actual_fixture_assessment_review_and_public_projection_preserve_clinical_withholding(system):
    from medplain.careflow_teachback import run_teachback_assessment

    svc, ops, pk, session = system
    case = completed(system)
    share, cap, _ = enabled(system, case)
    _, _, _, msg = svc.submit_response(share, cap, response(svc, share, cap))
    claim = svc.claim_teachback(pk, case["id"], msg["attempt_id"], 1)
    result = run_teachback_assessment(claim["source"], claim["result"], claim["prompt"],
                                     claim["response_text"], claim["execution_id"], mode="fixture")
    assert result["status"] == "provisional" and result["patient_regimen"] is None
    assert svc.complete_teachback(pk, claim, result)
    private = svc.view(pk, session, case["id"])["teachback"]["attempts"][0]
    assert private["status"] == "assessed" and "runtime" in private["assessment"]
    actor(system)
    svc.review_teachback(pk, session, case["id"], msg["attempt_id"], command(
        system, case["id"], actor_id="demo-pharmacist", decision="follow_up", comment="Private synthetic assessment note"))
    public = svc.patient_view(share, cap)
    shown = public["attempts"][0]
    assert shown["status"] == "reviewed" and shown["assessment"]["requires_reviewer"] is True
    assert not {"runtime", "receipt", "evidence", "execution_id"} & set(shown["assessment"])
    assert set(shown["review"]) == {"decision", "at"}
    assert "Private synthetic assessment note" not in json.dumps(public)
    assert ops.get_case(pk, case["id"])["result"] == case["result"]
    assert all(t["status"] == "open" for t in svc.view(pk, session, case["id"])["tasks"])
    # Stored-data corruption cannot turn a fixed follow-up into patient advice.
    saved = ops.store.rows[(pk, "CASE#" + case["id"])]["careflow"]["teachback"]["attempts"][0]
    saved["assessment"]["follow_up"]["text"] = "Invented treatment advice"
    with pytest.raises(OpsError) as error:
        svc.patient_view(share, cap)
    assert error.value.status == 409


class DynamoRecorder:
    def __init__(self):
        self.calls = []

    def put_item(self, **kwargs):
        self.calls.append(("put_item", kwargs))
        return {}

    def transact_write_items(self, **kwargs):
        self.calls.append(("transact_write_items", kwargs))
        return {}


def test_dynamo_enable_and_close_use_atomic_conditional_transactions():
    recorder = DynamoRecorder()
    store = DynamoStore("synthetic-table", client=recorder)
    cf = CareflowStore(store)
    now = int(time.time())
    case = {"id": "a" * 32, "ttl": now + 3600, "version": 8}
    child = {"id": "b" * 32, "ttl": now + 3600, "version": 4}
    cf.enable("SESSION#owner", case, 7, "CFWRITE#hash", {"ttl": now + 3600})
    writes = recorder.calls[-1][1]["TransactItems"]
    assert len(writes) == 2
    assert writes[0]["Put"]["ExpressionAttributeValues"][":v"] == {"N": "7"}
    assert writes[1]["Put"]["Item"]["PK"] == {"S": "CFWRITE#hash"}
    cf.close("SESSION#owner", case, 7, child)
    writes = recorder.calls[-1][1]["TransactItems"]
    assert len(writes) == 2 and "ConditionCheck" in writes[1]
    assert writes[1]["ConditionCheck"]["Key"]["SK"] == {"S": "CASE#" + "b" * 32}
    assert writes[1]["ConditionCheck"]["ExpressionAttributeValues"][":v"] == {"N": "4"}
    cf.select_actor("SESSION#owner", {"ttl": now + 3600, "version": 2, "actor_id": "demo-pharmacist"}, 1)
    assert recorder.calls[-1][1]["ConditionExpression"] == "#v = :v AND #ttl > :now"


def test_memory_atomic_close_guard_leaves_parent_unmodified():
    store = MemoryStore()
    now = int(time.time())
    store.put_new("owner", "CASE#parent", {"id": "parent", "ttl": now + 600, "version": 1})
    store.put_new("owner", "CASE#child", {"id": "child", "ttl": now + 600, "version": 9})
    with pytest.raises(Conflict):
        CareflowStore(store).close("owner", {"id": "parent", "ttl": now + 600, "version": 2}, 1,
                                   {"id": "child", "version": 8})
    assert store.get("owner", "CASE#parent")["version"] == 1
