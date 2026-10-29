"""Conditional synthetic operations storage. No clinical input is accepted here.

DynamoDB stores a JSON payload plus conditional-write attributes. A case and its
outbox entry commit together. The memory adapter has the same atomic contract
for offline CI; it is never an outage fallback for a configured DynamoDB table.
"""

import copy
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any


class Conflict(Exception):
    """A conditional write lost a race."""


class StoreUnavailable(Exception):
    """The configured persistent store could not serve a request."""


class DailyLimit(Exception):
    """No additional synthetic job may be accepted today."""


def daily_allowance() -> tuple[str, int, int]:
    try:
        limit = int(os.getenv("MEDPLAIN_OPS_DAILY_LIMIT", "120"))
        if not 1 <= limit <= 10000:
            raise ValueError()
    except ValueError:
        raise StoreUnavailable() from None
    now = datetime.now(timezone.utc)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return "ALLOWANCE#" + now.strftime("%Y%m%d"), limit, int(tomorrow.timestamp()) + 86400


class MemoryStore:
    storage = "memory (explicit offline synthetic mode)"
    transport_mode = "in-process background task"

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}
        self.lock = threading.RLock()

    def get(self, pk: str, sk: str) -> dict | None:
        with self.lock:
            row = self.rows.get((pk, sk))
            if row is None or row["ttl"] <= int(time.time()):
                return None
            return copy.deepcopy(row)

    def put_new(self, pk: str, sk: str, row: dict):
        with self.lock:
            if self.get(pk, sk):
                raise Conflict()
            self.rows[(pk, sk)] = copy.deepcopy(row)

    def set_role(self, pk: str, role: str):
        with self.lock:
            row = self.get(pk, "SESSION")
            if row is None:
                raise Conflict()
            row["role"] = role
            self.rows[(pk, "SESSION")] = row

    def cases(self, pk: str) -> list[dict]:
        with self.lock:
            return [copy.deepcopy(row) for (key, sk), row in self.rows.items()
                    if key == pk and sk.startswith("CASE#") and row["ttl"] > int(time.time())]

    def create(self, pk: str, case: dict, outbox: dict, idem: dict):
        with self.lock:
            session = self.get(pk, "SESSION")
            if (session is None or session["case_count"] >= 20
                    or self.get(pk, idem["sk"]) is not None):
                raise Conflict()
            self._consume_allowance()
            session["case_count"] += 1
            self.rows[(pk, "SESSION")] = session
            self.rows[(pk, "CASE#" + case["id"])] = copy.deepcopy(case)
            self.rows[(pk, outbox["sk"])] = copy.deepcopy(outbox)
            self.rows[(pk, idem["sk"])] = copy.deepcopy(idem)

    def seed(self, pk: str, cases: list[dict], shares: list[dict], collection: dict):
        """Commit a complete example collection once, without jobs or allowance writes."""
        with self.lock:
            session = self.get(pk, "SESSION")
            if (session is None or session["case_count"] != 0
                    or session.get("collection", {}).get("seed_version") is not None
                    or any(self.get(pk, "CASE#" + case["id"]) for case in cases)
                    or any(self.get("SHARE#" + share["id"], "VIEW") for share in shares)):
                raise Conflict()
            session.update(case_count=len(cases), collection=copy.deepcopy(collection))
            staged = {(pk, "SESSION"): session}
            staged.update({(pk, "CASE#" + case["id"]): copy.deepcopy(case) for case in cases})
            staged.update({("SHARE#" + share["id"], "VIEW"): copy.deepcopy(share) for share in shares})
            self.rows.update(staged)

    def replace(self, pk: str, case: dict, expected_version: int,
                outbox: dict | None = None, share: dict | None = None):
        with self.lock:
            current = self.get(pk, "CASE#" + case["id"])
            if current is None or current["version"] != expected_version:
                raise Conflict()
            if outbox and self.get(pk, outbox["sk"]):
                raise Conflict()
            if share and self.get("SHARE#" + share["id"], "VIEW"):
                raise Conflict()
            if outbox:
                self._consume_allowance()
            self.rows[(pk, "CASE#" + case["id"])] = copy.deepcopy(case)
            if outbox:
                self.rows[(pk, outbox["sk"])] = copy.deepcopy(outbox)
            if share:
                self.rows[("SHARE#" + share["id"], "VIEW")] = copy.deepcopy(share)

    def _consume_allowance(self):
        pk, limit, ttl = daily_allowance()
        current = self.rows.get((pk, "JOBS"), {"used": 0, "ttl": ttl})
        if current["used"] >= limit:
            raise DailyLimit()
        self.rows[(pk, "JOBS")] = {"used": current["used"] + 1, "ttl": ttl}

    def mark_sent(self, pk: str, sk: str):
        with self.lock:
            row = self.get(pk, sk)
            if row:
                row["sent_at"] = int(time.time())
                self.rows[(pk, sk)] = row


class DynamoStore:
    storage = "DynamoDB"
    transport_mode = "DynamoDB transactional outbox / Streams / SQS"

    def __init__(self, table: str, client=None):
        import boto3
        from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

        self.table = table
        self.client = client or boto3.client("dynamodb")
        self.serializer = TypeSerializer()
        self.deserializer = TypeDeserializer()

    def _attrs(self, value: dict) -> dict:
        return {key: self.serializer.serialize(item) for key, item in value.items()}

    def _item(self, pk: str, sk: str, row: dict) -> dict:
        attributes = {"PK": pk, "SK": sk, "payload": json.dumps(row, separators=(",", ":")),
                      "ttl": row["ttl"], "version": row.get("version", 0),
                      "entity": "OUTBOX" if sk.startswith("OUTBOX#") else sk.split("#")[0]}
        if sk == "SESSION":
            attributes["case_count"] = row["case_count"]
            if "collection" in row:
                attributes["collection"] = json.dumps(row["collection"], separators=(",", ":"))
        return self._attrs(attributes)

    def _call(self, method, **kwargs) -> Any:
        try:
            return getattr(self.client, method)(**kwargs)
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in {"ConditionalCheckFailedException", "TransactionCanceledException"}:
                # Cancellation due to throttling is still retriable, never a memory fallback.
                reasons = getattr(exc, "response", {}).get("CancellationReasons", [])
                if not reasons or any(r.get("Code") == "ConditionalCheckFailed" for r in reasons):
                    raise Conflict() from None
            raise StoreUnavailable() from None

    def get(self, pk: str, sk: str) -> dict | None:
        response = self._call("get_item", TableName=self.table,
                              Key=self._attrs({"PK": pk, "SK": sk}), ConsistentRead=True)
        item = response.get("Item")
        if not item:
            return None
        row = json.loads(item["payload"]["S"])
        if row["ttl"] <= int(time.time()):
            return None
        if "sent_at" in item:
            row["sent_at"] = int(item["sent_at"]["N"])
        if "case_count" in item:
            row["case_count"] = int(item["case_count"]["N"])
        if "collection" in item:
            row["collection"] = json.loads(item["collection"]["S"])
        return row

    def put_new(self, pk: str, sk: str, row: dict):
        self._call("put_item", TableName=self.table, Item=self._item(pk, sk, row),
                   ConditionExpression="attribute_not_exists(PK)")

    def set_role(self, pk: str, role: str):
        # Session count is authoritative in a top-level attribute. Role writes do
        # not touch it, so a concurrent case transaction cannot lose its count.
        current = self.get(pk, "SESSION")
        if current is None:
            raise Conflict()
        current["role"] = role
        self._call("update_item", TableName=self.table, Key=self._attrs({"PK": pk, "SK": "SESSION"}),
                   UpdateExpression="SET payload = :payload",
                   ConditionExpression="#ttl > :now",
                   ExpressionAttributeNames={"#ttl": "ttl"},
                   ExpressionAttributeValues=self._attrs({":payload": json.dumps(current), ":now": int(time.time())}))

    def cases(self, pk: str) -> list[dict]:
        rows = []
        arguments = {"TableName": self.table, "ConsistentRead": True,
                     "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
                     "ExpressionAttributeValues": self._attrs({":pk": pk, ":prefix": "CASE#"})}
        while True:
            response = self._call("query", **arguments)
            rows.extend(json.loads(item["payload"]["S"]) for item in response.get("Items", [])
                        if int(item["ttl"]["N"]) > int(time.time()))
            if not response.get("LastEvaluatedKey"):
                break
            arguments["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return rows

    def _put(self, pk: str, sk: str, row: dict, condition="attribute_not_exists(PK)", **kwargs):
        return {"Put": {"TableName": self.table, "Item": self._item(pk, sk, row),
                        "ConditionExpression": condition, **kwargs}}

    def create(self, pk: str, case: dict, outbox: dict, idem: dict):
        self._transaction([
            {"Update": {"TableName": self.table, "Key": self._attrs({"PK": pk, "SK": "SESSION"}),
                        "UpdateExpression": "ADD case_count :one",
                        "ConditionExpression": "#ttl > :now AND (attribute_not_exists(case_count) OR case_count < :max)",
                        "ExpressionAttributeNames": {"#ttl": "ttl"},
                        "ExpressionAttributeValues": self._attrs({":one": 1, ":max": 20, ":now": int(time.time())})}},
            self._put(pk, "CASE#" + case["id"], case),
            self._put(pk, outbox["sk"], outbox), self._put(pk, idem["sk"], idem),
            self._allowance_write(),
        ], allowance=True)

    def replace(self, pk: str, case: dict, expected_version: int,
                outbox: dict | None = None, share: dict | None = None):
        writes = [self._put(pk, "CASE#" + case["id"], case,
                            "#version = :expected AND #ttl > :now",
                            ExpressionAttributeNames={"#version": "version", "#ttl": "ttl"},
                            ExpressionAttributeValues=self._attrs({":expected": expected_version, ":now": int(time.time())}))]
        if outbox:
            writes.append(self._put(pk, outbox["sk"], outbox))
            writes.append(self._allowance_write())
        if share:
            writes.append(self._put("SHARE#" + share["id"], "VIEW", share))
        self._transaction(writes, allowance=outbox is not None)

    def seed(self, pk: str, cases: list[dict], shares: list[dict], collection: dict):
        # Role changes only replace payload. Authoritative count/collection
        # attributes survive a concurrent role write carrying an older payload.
        self._transaction([
            {"Update": {"TableName": self.table, "Key": self._attrs({"PK": pk, "SK": "SESSION"}),
                        "UpdateExpression": "SET case_count = :count, #collection = :collection, #seeded = :yes",
                        "ConditionExpression": "#ttl > :now AND case_count = :zero AND attribute_not_exists(#seeded)",
                        "ExpressionAttributeNames": {"#ttl": "ttl", "#collection": "collection", "#seeded": "seeded"},
                        "ExpressionAttributeValues": self._attrs({":count": len(cases), ":zero": 0,
                            ":collection": json.dumps(collection, separators=(",", ":")),
                            ":yes": True, ":now": int(time.time())})}},
            *[self._put(pk, "CASE#" + case["id"], case) for case in cases],
            *[self._put("SHARE#" + share["id"], "VIEW", share) for share in shares],
        ])

    def _allowance_write(self):
        pk, limit, ttl = daily_allowance()
        return {"Update": {"TableName": self.table, "Key": self._attrs({"PK": pk, "SK": "JOBS"}),
                           "UpdateExpression": "SET #ttl = :ttl ADD used :one",
                           "ConditionExpression": "attribute_not_exists(used) OR used < :limit",
                           "ExpressionAttributeNames": {"#ttl": "ttl"},
                           "ExpressionAttributeValues": self._attrs({":ttl": ttl, ":one": 1, ":limit": limit})}}

    def _transaction(self, writes: list, allowance=False):
        try:
            self._call("transact_write_items", TransactItems=writes)
        except Conflict:
            if allowance:
                pk, limit, _ = daily_allowance()
                item = self._call("get_item", TableName=self.table,
                                  Key=self._attrs({"PK": pk, "SK": "JOBS"}), ConsistentRead=True).get("Item", {})
                if int(item.get("used", {"N": "0"})["N"]) >= limit:
                    raise DailyLimit() from None
            raise

    def mark_sent(self, pk: str, sk: str):
        self._call("update_item", TableName=self.table, Key=self._attrs({"PK": pk, "SK": sk}),
                   UpdateExpression="SET sent_at = :now", ConditionExpression="attribute_exists(PK)",
                   ExpressionAttributeValues=self._attrs({":now": int(time.time())}))


_stores = {}
_store_lock = threading.Lock()


def reset_store_cache():
    """Reset explicit offline adapters between CI tests."""
    with _store_lock:
        _stores.clear()


def get_store():
    table = os.getenv("MEDPLAIN_OPS_TABLE")
    offline = os.getenv("MEDPLAIN_MODEL_MODE", "fixture") == "fixture"
    local = os.getenv("MEDPLAIN_OPS_MEMORY") == "1" and not os.getenv("AWS_LAMBDA_FUNCTION_NAME")
    if not table and not (offline or local):
        raise StoreUnavailable()
    key = (table, offline, local)
    with _store_lock:
        if key not in _stores:
            try:
                _stores[key] = DynamoStore(table) if table else MemoryStore()
            except Exception:
                raise StoreUnavailable() from None
        return _stores[key]
