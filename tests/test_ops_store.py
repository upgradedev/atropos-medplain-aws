"""Offline DynamoDB adapter contract, transaction races and fail-closed behavior."""

import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from medplain.ops_domain import OpsError, OpsService, outbox_for
from medplain.ops_store import (
    Conflict,
    DailyLimit,
    DynamoStore,
    MemoryStore,
    StoreUnavailable,
    daily_allowance,
    get_store,
    reset_store_cache,
)


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "TransactionCanceledException"},
                "CancellationReasons": [{"Code": "ConditionalCheckFailed"}]}


class AtomicDynamo:
    """Small independent transaction emulator, including rollback on a losing CAS.

    It does not assert AWS deployment. It exercises the exact emitted DynamoDB
    wire shape and counter conditions concurrently without network access.
    """
    def __init__(self):
        self.rows = {}
        self.lock = threading.RLock()
        self.transactions = []

    @staticmethod
    def key(item):
        return item["PK"]["S"], item["SK"]["S"]

    def get_item(self, **kwargs):
        with self.lock:
            item = self.rows.get(self.key(kwargs["Key"]))
            return {"Item": copy.deepcopy(item)} if item else {}

    def put_item(self, **kwargs):
        with self.lock:
            key = self.key(kwargs["Item"])
            if key in self.rows:
                raise ConditionalFailure()
            self.rows[key] = copy.deepcopy(kwargs["Item"])
            return {}

    def query(self, **kwargs):
        assert kwargs["ConsistentRead"] and "begins_with" in kwargs["KeyConditionExpression"]
        pk = kwargs["ExpressionAttributeValues"][":pk"]["S"]
        return {"Items": [copy.deepcopy(row) for (key, sk), row in self.rows.items()
                          if key == pk and sk.startswith("CASE#")]}

    def update_item(self, **kwargs):
        with self.lock:
            key = self.key(kwargs["Key"])
            row = self.rows.get(key)
            if not row:
                raise ConditionalFailure()
            values = kwargs["ExpressionAttributeValues"]
            if "payload" in kwargs["UpdateExpression"]:
                if int(row["ttl"]["N"]) <= int(values[":now"]["N"]):
                    raise ConditionalFailure()
                row["payload"] = values[":payload"]
            else:
                row["sent_at"] = values[":now"]
            return {}

    def transact_write_items(self, **kwargs):
        with self.lock:
            self.transactions.append(copy.deepcopy(kwargs["TransactItems"]))
            staged = copy.deepcopy(self.rows)
            for write in kwargs["TransactItems"]:
                if "Put" in write:
                    put = write["Put"]
                    key = self.key(put["Item"])
                    existing = staged.get(key)
                    if put["ConditionExpression"] == "attribute_not_exists(PK)":
                        if existing:
                            raise ConditionalFailure()
                    else:
                        values = put["ExpressionAttributeValues"]
                        if (not existing or existing["version"] != values[":expected"]
                                or int(existing["ttl"]["N"]) <= int(values[":now"]["N"])):
                            raise ConditionalFailure()
                    staged[key] = copy.deepcopy(put["Item"])
                else:
                    update = write["Update"]
                    key = self.key(update["Key"])
                    values = update["ExpressionAttributeValues"]
                    row = staged.get(key)
                    if key[0].startswith("ALLOWANCE#"):
                        used = int(row["used"]["N"]) if row else 0
                        if used >= int(values[":limit"]["N"]):
                            raise ConditionalFailure()
                        staged[key] = {**update["Key"], "used": {"N": str(used + 1)}, "ttl": values[":ttl"]}
                    else:
                        if (not row or int(row["ttl"]["N"]) <= int(values[":now"]["N"])
                                or int(row["case_count"]["N"]) >= int(values[":max"]["N"])):
                            raise ConditionalFailure()
                        row["case_count"] = {"N": str(int(row["case_count"]["N"]) + 1)}
            self.rows = staged
            return {}


@pytest.fixture
def dynamo():
    client = AtomicDynamo()
    return DynamoStore("synthetic-ops", client), client


def test_dynamo_transaction_case_outbox_share_and_count_survive_role_update(dynamo):
    store, client = dynamo
    svc = OpsService(store)
    visitor = svc.new_session()
    pk, session = svc.authenticate(visitor["session_token"])
    case, created, _ = svc.create(pk, session, "home-after-pneumonia", "demo-athens", False, "dynamo-request")
    assert created and store.get(pk, "SESSION")["case_count"] == 1
    assert len(client.transactions[0]) == 5  # session, case, outbox, idempotency, allowance
    assert any(w.get("Put", {}).get("Item", {}).get("entity") == {"S": "OUTBOX"}
               for w in client.transactions[0])
    svc.role(pk, "auditor")
    assert store.get(pk, "SESSION")["case_count"] == 1
    assert store.get(pk, "SESSION")["role"] == "auditor"
    assert svc.list_cases(pk)["items"][0]["id"] == case["id"]
    store.mark_sent(pk, outbox_for(case)["sk"])
    assert store.get(pk, outbox_for(case)["sk"])["sent_at"] > 0
    changed = copy.deepcopy(case)
    changed.update(version=2, status="failed")
    share = {"id": "opaque-share", "ttl": case["ttl"], "synthetic": True}
    store.replace(pk, changed, 1, share=share)
    assert store.get("SHARE#opaque-share", "VIEW")["synthetic"]
    with pytest.raises(Conflict):
        store.replace(pk, case, 1)
    assert store.get(pk, "CASE#" + case["id"])["version"] == 2


def test_dynamo_daily_cap_race_is_atomic_with_no_orphan_cases_or_outbox(dynamo, monkeypatch):
    monkeypatch.setenv("MEDPLAIN_OPS_DAILY_LIMIT", "7")
    store, client = dynamo
    svc = OpsService(store)
    sessions = [svc.authenticate(svc.new_session()["session_token"]) for _ in range(20)]

    def attempt(item):
        pk, session = item
        try:
            return svc.create(pk, session, "home-after-pneumonia", "demo-athens", False, "same-key-own-session")
        except OpsError as error:
            assert error.status == 429 and "00:00 UTC" in error.message
            return None

    with ThreadPoolExecutor(max_workers=12) as executor:
        accepted = list(executor.map(attempt, sessions))
    assert sum(item is not None for item in accepted) == 7
    assert len([sk for _, sk in client.rows if sk.startswith("CASE#")]) == 7
    assert len([sk for _, sk in client.rows if sk.startswith("OUTBOX#")]) == 7
    assert client.rows[(daily_allowance()[0], "JOBS")]["used"] == {"N": "7"}
    for (pk, session), result in zip(sessions, accepted, strict=True):
        if result:
            replay = svc.create(pk, session, "home-after-pneumonia", "demo-athens", False, "same-key-own-session")
            assert not replay[1] and replay[0]["id"] == result[0]["id"]
    assert client.rows[(daily_allowance()[0], "JOBS")]["used"] == {"N": "7"}


def test_dynamo_retry_consumes_counter_atomically_and_rolls_back_when_full(dynamo, monkeypatch):
    monkeypatch.setenv("MEDPLAIN_OPS_DAILY_LIMIT", "2")
    store, client = dynamo
    svc = OpsService(store)
    pk, session = svc.authenticate(svc.new_session()["session_token"])
    case = svc.create(pk, session, "home-after-pneumonia", "demo-athens", False, "initial-case")[0]
    case.update(version=2, status="failed")
    store.replace(pk, case, 1)
    retry = svc.retry(pk, session, case["id"], 2, "dynamo-retry")[0]
    assert retry["generation"] == 2 and store.get(pk, outbox_for(retry)["sk"])
    assert not svc.retry(pk, session, case["id"], 2, "dynamo-retry")[1]
    retry.update(version=retry["version"] + 1, status="failed")
    store.replace(pk, retry, retry["version"] - 1)
    before = copy.deepcopy(client.rows)
    with pytest.raises(OpsError) as error:
        svc.retry(pk, session, case["id"], retry["version"], "over-budget-retry")
    assert error.value.status == 429 and client.rows == before


def test_dynamo_expiry_pagination_and_failure_classification(dynamo, monkeypatch):
    store, client = dynamo
    expired = {"ttl": int(time.time()) - 1, "version": 0}
    store.put_new("SESSION#expired", "CASE#expired", expired)
    assert store.get("SESSION#expired", "CASE#expired") is None
    assert store.cases("SESSION#expired") == []
    with pytest.raises(Conflict):
        store.set_role("SESSION#missing", "auditor")
    responses = iter([{"Items": [], "LastEvaluatedKey": {"PK": {"S": "next"}, "SK": {"S": "next"}}}, {"Items": []}])
    monkeypatch.setattr(client, "query", lambda **kwargs: next(responses))
    assert store.cases("SESSION#empty") == []

    class Throttle(Exception):
        response = {"Error": {"Code": "TransactionCanceledException"},
                    "CancellationReasons": [{"Code": "ProvisionedThroughputExceeded"}]}

    def unavailable(**kwargs):
        raise Throttle()

    monkeypatch.setattr(client, "get_item", unavailable)
    with pytest.raises(StoreUnavailable):
        store.get("any", "item")


def test_store_configuration_never_falls_back_after_cloud_failure(monkeypatch):
    import medplain.ops_store as module

    reset_store_cache()
    monkeypatch.delenv("MEDPLAIN_OPS_TABLE", raising=False)
    monkeypatch.delenv("MEDPLAIN_OPS_MEMORY", raising=False)
    monkeypatch.setenv("MEDPLAIN_MODEL_MODE", "fixture")
    assert isinstance(get_store(), MemoryStore) and get_store() is get_store()
    reset_store_cache()
    monkeypatch.setenv("MEDPLAIN_MODEL_MODE", "bedrock")
    with pytest.raises(StoreUnavailable):
        get_store()
    monkeypatch.setenv("MEDPLAIN_OPS_MEMORY", "1")
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    assert isinstance(get_store(), MemoryStore)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "deployed-function")
    with pytest.raises(StoreUnavailable):
        get_store()
    monkeypatch.setenv("MEDPLAIN_OPS_TABLE", "required-production-table")

    def broken_constructor(table):
        raise RuntimeError("provider failure")

    monkeypatch.setattr(module, "DynamoStore", broken_constructor)
    with pytest.raises(StoreUnavailable):
        get_store()
    reset_store_cache()


@pytest.mark.parametrize("invalid", ["NaN", "0", "-1", "10001"])
def test_invalid_daily_allowance_fails_closed(monkeypatch, invalid):
    monkeypatch.setenv("MEDPLAIN_OPS_DAILY_LIMIT", invalid)
    with pytest.raises(StoreUnavailable):
        daily_allowance()


def test_low_level_daily_limit_is_not_an_outage(dynamo, monkeypatch):
    monkeypatch.setenv("MEDPLAIN_OPS_DAILY_LIMIT", "1")
    store, client = dynamo
    pk, limit, ttl = daily_allowance()
    client.rows[(pk, "JOBS")] = {"PK": {"S": pk}, "SK": {"S": "JOBS"}, "used": {"N": str(limit)}, "ttl": {"N": str(ttl)}}
    with pytest.raises(DailyLimit):
        store._transaction([store._allowance_write()], allowance=True)
    assert json.loads(store._item("PK", "SK", {"ttl": ttl})["payload"]["S"])["ttl"] == ttl
