"""At-least-once queue semantics, outbox crash window and actual worker boundary."""

import json

import pytest

from medplain.ops_domain import OpsService, outbox_for
from medplain.ops_relay import handler as relay_handler
from medplain.ops_relay import relay_record
from medplain.ops_store import MemoryStore, StoreUnavailable
from medplain.ops_worker import handler as worker_handler
from medplain.ops_worker import process_message, validate_message
from test_ops_domain import evidence


@pytest.fixture
def job(monkeypatch):
    monkeypatch.setenv("MEDPLAIN_MODEL_MODE", "fixture")
    monkeypatch.setenv("MEDPLAIN_OPS_QUEUE_URL", "https://sqs.example.invalid/synthetic-jobs")
    svc = OpsService(MemoryStore())
    pk, session = svc.authenticate(svc.new_session()["session_token"])
    case = svc.create(pk, session, "home-after-pneumonia", "demo-athens", False, "queue-test-request")[0]
    return svc, pk, case, {"session_key": pk, "case_id": case["id"], "generation": 1}


def stream_record(pk, case):
    return {"eventName": "INSERT", "eventID": "not-the-sequence-number", "dynamodb": {
        "SequenceNumber": "1000007", "NewImage": {"PK": {"S": pk},
        "SK": {"S": outbox_for(case)["sk"]}, "entity": {"S": "OUTBOX"}}}}


class CapturingSQS:
    def __init__(self):
        self.messages = []

    def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return {"MessageId": "synthetic-message-id"}


def test_relay_reads_committed_outbox_and_never_sends_documents_or_tokens(job):
    svc, pk, case, message = job
    sqs = CapturingSQS()
    record = stream_record(pk, case)
    assert relay_record(record, svc.store, sqs)
    assert json.loads(sqs.messages[0]["MessageBody"]) == message
    assert set(message) == {"session_key", "case_id", "generation"}
    assert not relay_record(record, svc.store, sqs)
    assert len(sqs.messages) == 1
    assert not relay_record({"eventName": "MODIFY"}, svc.store, sqs)
    assert not relay_record({"eventName": "INSERT"}, svc.store, sqs)


def test_send_then_mark_crash_can_duplicate_delivery_but_not_publication(job, monkeypatch):
    svc, pk, case, message = job
    sqs = CapturingSQS()
    real_mark = svc.store.mark_sent
    calls = 0

    def crash_once(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise StoreUnavailable()
        real_mark(*args)

    monkeypatch.setattr(svc.store, "mark_sent", crash_once)
    with pytest.raises(StoreUnavailable):
        relay_record(stream_record(pk, case), svc.store, sqs)
    assert relay_record(stream_record(pk, case), svc.store, sqs)
    assert len(sqs.messages) == 2 and sqs.messages[0] == sqs.messages[1]

    def fixture_result(packet, execution_id, **kwargs):
        claimed = svc.get_case(pk, case["id"])
        assert claimed["execution_id"] == execution_id
        return evidence(claimed)

    monkeypatch.setattr("medplain.engine.run_packet", fixture_result)
    assert process_message(message, svc.store)
    version = svc.get_case(pk, case["id"])["version"]
    assert not process_message(message, svc.store)
    assert svc.get_case(pk, case["id"])["version"] == version
    assert svc.get_case(pk, case["id"])["status"] == "released"


def test_worker_provider_failure_is_visible_without_provider_text(job, monkeypatch):
    svc, pk, case, message = job

    def broken(*args, **kwargs):
        raise RuntimeError("private provider trace")

    monkeypatch.setattr("medplain.engine.run_packet", broken)
    assert process_message(message, svc.store)
    failed = svc.get_case(pk, case["id"])
    assert failed["status"] == "failed" and failed["result"] is None
    assert "private" not in repr(failed)


def test_worker_rejects_corrupted_source_before_inference(job, monkeypatch):
    svc, pk, case, message = job
    svc.store.rows[(pk, "CASE#" + case["id"])]["source"]["summary"] = "Source was changed"

    def must_not_call(*args, **kwargs):
        pytest.fail("A corrupted source must not reach inference")

    monkeypatch.setattr("medplain.engine.run_packet", must_not_call)
    assert process_message(message, svc.store)
    assert svc.get_case(pk, case["id"])["status"] == "failed"


@pytest.mark.parametrize("message", [
    {}, {"session_key": "raw-token", "case_id": "0" * 32, "generation": 1},
    {"session_key": "SESSION#" + "a" * 64, "case_id": "wrong", "generation": 1},
    {"session_key": "SESSION#" + "a" * 64, "case_id": "0" * 32, "generation": True},
    {"session_key": "SESSION#" + "a" * 64, "case_id": "0" * 32, "generation": 4},
    {"session_key": "SESSION#" + "a" * 64, "case_id": "0" * 32, "generation": 1, "source": "forbidden"},
])
def test_queue_selector_is_strict(message):
    with pytest.raises(ValueError):
        validate_message(message)


def test_sqs_partial_batch_reports_only_failed_message_ids(monkeypatch):
    import medplain.ops_worker as module

    observed = []

    def process(message):
        observed.append(message)
        if message["fail"]:
            raise StoreUnavailable()

    monkeypatch.setattr(module, "process_message", process)
    records = [{"messageId": "success", "body": '{"fail":false}'},
               {"messageId": "retry", "body": '{"fail":true}'},
               {"messageId": "invalid-json", "body": "{"}]
    assert worker_handler({"Records": records}, None) == {
        "batchItemFailures": [{"itemIdentifier": "retry"}, {"itemIdentifier": "invalid-json"}]}
    assert len(observed) == 2 and worker_handler({}, None) == {"batchItemFailures": []}


def test_stream_partial_batch_uses_sequence_number_not_event_id(monkeypatch):
    import medplain.ops_relay as module

    def failed(record):
        raise StoreUnavailable()

    monkeypatch.setattr(module, "relay_record", failed)
    record = {"eventID": "wrong-identifier", "dynamodb": {"SequenceNumber": "correct-sequence"}}
    assert relay_handler({"Records": [record]}, None) == {
        "batchItemFailures": [{"itemIdentifier": "correct-sequence"}]}
    assert relay_handler({}, None) == {"batchItemFailures": []}


def test_relay_cannot_enqueue_a_mismatched_outbox_identity(job):
    svc, pk, case, _ = job
    sk = outbox_for(case)["sk"]
    svc.store.rows[(pk, sk)]["case_id"] = "0" * 32
    with pytest.raises(ValueError):
        relay_record(stream_record(pk, case), svc.store, CapturingSQS())
