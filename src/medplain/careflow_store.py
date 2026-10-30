"""Additive conditional rows on the existing fail-closed operations store."""

import copy
import json
import time

from medplain.ops_store import Conflict, DynamoStore, MemoryStore


def check_size(case: dict) -> None:
    """Leave headroom for DynamoDB keys and version/TTL attributes."""
    from medplain.ops_domain import OpsError

    if len(json.dumps(case, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 380000:
        raise OpsError(413, "This synthetic workflow reached its bounded storage limit.")


class CareflowStore:
    def __init__(self, store):
        if not isinstance(store, (MemoryStore, DynamoStore)):
            raise TypeError("Careflow requires the existing conditional operations adapter")
        self.store = store

    def actor(self, pk: str) -> dict | None:
        return self.store.get(pk, "CAREFLOW_ACTOR")

    def close(self, pk: str, case: dict, expected_version: int, revision: dict):
        """Fence the independently verified child snapshot in the same commit."""
        check_size(case)
        if isinstance(self.store, MemoryStore):
            with self.store.lock:
                current = self.store.get(pk, "CASE#" + case["id"])
                child = self.store.get(pk, "CASE#" + revision["id"])
                if (current is None or current["version"] != expected_version
                        or child is None or child["version"] != revision["version"]):
                    raise Conflict()
                self.store.rows[(pk, "CASE#" + case["id"])] = copy.deepcopy(case)
            return
        self.store._transaction([
            self.store._put(pk, "CASE#" + case["id"], case, "#v = :v AND #ttl > :now",
                            ExpressionAttributeNames={"#v": "version", "#ttl": "ttl"},
                            ExpressionAttributeValues=self.store._attrs({":v": expected_version, ":now": int(time.time())})),
            {"ConditionCheck": {"TableName": self.store.table,
                                "Key": self.store._attrs({"PK": pk, "SK": "CASE#" + revision["id"]}),
                                "ConditionExpression": "#v = :v AND #ttl > :now",
                                "ExpressionAttributeNames": {"#v": "version", "#ttl": "ttl"},
                                "ExpressionAttributeValues": self.store._attrs({":v": revision["version"], ":now": int(time.time())})}},
        ])

    def select_actor(self, pk: str, row: dict, expected_version: int):
        if isinstance(self.store, MemoryStore):
            with self.store.lock:
                current = self.actor(pk)
                if (current or {}).get("version", 0) != expected_version:
                    raise Conflict()
                self.store.rows[(pk, "CAREFLOW_ACTOR")] = copy.deepcopy(row)
            return
        condition = "attribute_not_exists(PK)" if expected_version == 0 else "#v = :v AND #ttl > :now"
        arguments = {} if expected_version == 0 else {
            "ExpressionAttributeNames": {"#v": "version", "#ttl": "ttl"},
            "ExpressionAttributeValues": self.store._attrs({":v": expected_version, ":now": int(time.time())}),
        }
        self.store._call("put_item", TableName=self.store.table,
                         Item=self.store._item(pk, "CAREFLOW_ACTOR", row),
                         ConditionExpression=condition, **arguments)

    def enable(self, pk: str, case: dict, expected_version: int, cap_pk: str, capability: dict):
        """The case marker and separate write capability commit atomically."""
        check_size(case)
        if isinstance(self.store, MemoryStore):
            with self.store.lock:
                current = self.store.get(pk, "CASE#" + case["id"])
                if (current is None or current["version"] != expected_version
                        or self.store.get(cap_pk, "CAPABILITY") is not None):
                    raise Conflict()
                self.store.rows[(pk, "CASE#" + case["id"])] = copy.deepcopy(case)
                self.store.rows[(cap_pk, "CAPABILITY")] = copy.deepcopy(capability)
            return
        self.store._transaction([
            self.store._put(pk, "CASE#" + case["id"], case, "#v = :v AND #ttl > :now",
                            ExpressionAttributeNames={"#v": "version", "#ttl": "ttl"},
                            ExpressionAttributeValues=self.store._attrs({":v": expected_version, ":now": int(time.time())})),
            self.store._put(cap_pk, "CAPABILITY", capability),
        ])
