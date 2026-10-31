"""Additive queue selectors cannot smuggle patient data or change legacy jobs."""

import json
import time

import pytest

from medplain import careflow_relay, careflow_worker
from medplain.ops_store import MemoryStore, StoreUnavailable


def selector():
    return {"session_key": "SESSION#" + "a" * 64, "case_id": "b" * 32,
            "generation": 1, "job_kind": "careflow_teachback", "attempt_id": "c" * 32}


def test_v3_dispatch_preserves_original_case_worker_selector(monkeypatch):
    seen = []
    old = {key: value for key, value in selector().items() if key not in {"job_kind", "attempt_id"}}
    marker = object()
    monkeypatch.setattr(careflow_worker, "process_legacy", lambda msg, store: seen.append((msg, store)) or True)
    assert careflow_worker.process_message(old, marker)
    assert seen == [(old, marker)]


@pytest.mark.parametrize("change", [
    {"source": "patient source must never cross SQS"}, {"attempt_id": "wrong"},
    {"session_key": "raw-session-token"}, {"generation": True}, {"generation": 4},
    {"job_kind": "other-assessment"}, {"response_text": "private response"},
])
def test_careflow_selector_rejects_forged_or_data_bearing_messages(change):
    with pytest.raises(ValueError):
        careflow_worker.validate_teachback_message({**selector(), **change})


def test_outbox_send_then_mark_uses_only_selectors_and_rejects_wrong_identity(monkeypatch):
    message = selector()
    store = MemoryStore()
    sk = f"OUTBOX#CARE#{message['case_id']}#{message['attempt_id']}"
    row = {**{key: value for key, value in message.items() if key != "session_key"},
           "sk": sk, "ttl": int(time.time()) + 60, "sent_at": None}
    store.put_new(message["session_key"], sk, row)
    record = {"eventName": "INSERT", "dynamodb": {"SequenceNumber": "123", "NewImage": {
        "PK": {"S": message["session_key"]}, "SK": {"S": sk}, "entity": {"S": "OUTBOX"}}}}
    sent = []

    class Queue:
        def send_message(self, **kwargs):
            sent.append(kwargs)

    monkeypatch.setenv("MEDPLAIN_OPS_QUEUE_URL", "https://sqs.example.invalid/fixture")
    assert careflow_relay.relay_record(record, store, Queue())
    assert json.loads(sent[0]["MessageBody"]) == message
    assert not careflow_relay.relay_record(record, store, Queue())
    assert len(sent) == 1
    store.rows[(message["session_key"], sk)]["sent_at"] = None
    store.rows[(message["session_key"], sk)]["attempt_id"] = "d" * 32
    with pytest.raises(ValueError):
        careflow_relay.relay_record(record, store, Queue())
    assert len(sent) == 1


def test_careflow_sqs_batch_keeps_failure_and_legacy_message_ids(monkeypatch):
    seen = []

    def process(message):
        seen.append(message)
        if message.get("fail"):
            raise StoreUnavailable()

    monkeypatch.setattr(careflow_worker, "process_message", process)
    event = {"Records": [
        {"messageId": "ok", "body": '{"fail":false}'},
        {"messageId": "retry", "body": '{"fail":true}'},
        {"messageId": "bad-json", "body": '{'},
    ]}
    assert careflow_worker.handler(event, None) == {"batchItemFailures": [
        {"itemIdentifier": "retry"}, {"itemIdentifier": "bad-json"}]}
    assert len(seen) == 2
