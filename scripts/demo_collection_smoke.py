"""Audit persisted prepared examples without confusing them with fresh inference."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

from ops_live_smoke import Client, canonical, validate_source_facts, verify_chain

PREPARED_COUNT = 12
PREPARED_STATUSES = {"released": 5, "review_required": 4, "rejected": 2, "failed": 1}


def validate_prepared_case(case: dict) -> None:
    assert case["origin"] == "prepared_demo", "prepared rows must identify their origin"
    assert case["status"] in PREPARED_STATUSES, "a prepared example cannot simulate an active worker"
    assert case["duration_ms"] is None, "prepared duration must not become a live worker measurement"
    meta = case["prepared"]
    assert all(meta.get(key) for key in ["version", "prepared_at", "source_commit", "original_execution_id"])
    assert re.fullmatch(r"[0-9a-f]{40}", meta["source_commit"]), "prepared source commit must identify its actual build"
    assert datetime.fromisoformat(meta["prepared_at"].replace("Z", "+00:00")).tzinfo is not None
    assert case["source_hash"] == hashlib.sha256(canonical(case["source"])).hexdigest()
    result = case["result"]
    assert result and result["model_mode"] == "fixture", "prepared output must never masquerade as Bedrock execution"
    assert result["session_id"] == meta["original_execution_id"], "keep the original SDK execution correlation"
    receipts = result["receipts"]
    if receipts:
        assert verify_chain(receipts), "prepared evidence must preserve its complete hash chain"
        assert all(receipt["session_id"] == meta["original_execution_id"]
                   and receipt["source_hash"] == case["source_hash"] for receipt in receipts)
    if case["status"] == "failed":
        assert result["status"] == "refused" and result["explanation"] is None and result["explanation_text"] is None
        assert case["artifact_hash"] is None and meta["original_artifact_hash"] is None and case["patient_url"] is None
        assert not receipts or receipts[-1]["verdict"] == "refused"
        return
    assert result["status"] == "verified" and receipts and receipts[-1]["verdict"] == "verified"
    validate_source_facts(case["source"], result)
    artifact = hashlib.sha256(canonical({"structured": result["explanation"], "text": result["explanation_text"]})).hexdigest()
    assert artifact == case["artifact_hash"] == meta["original_artifact_hash"] == receipts[-1]["artifact_hash"]
    assert bool(case["patient_url"]) == (case["status"] == "released"), "only released examples may be shared"
    assert result["runtime"]["orchestrator"] == "strands.multiagent.Graph"


def audit_prepared_collection(client: Client) -> dict:
    bootstrap = client.call("/api/ops/bootstrap")
    collection = bootstrap["collection"]
    assert collection["mode"] == "prepared_demo" and collection["initialized"] is True
    assert collection["prepared_count"] == PREPARED_COUNT and collection["live_count"] == 0
    rows = client.call("/api/ops/cases?workspace=all&limit=20")
    assert rows["total"] == PREPARED_COUNT and rows["next_cursor"] is None
    assert len(rows["items"]) == PREPARED_COUNT and len({case["id"] for case in rows["items"]}) == PREPARED_COUNT
    details = [client.call("/api/ops/cases/" + case["id"]) for case in rows["items"]]
    for case in details:
        validate_prepared_case(case)
    expected_ids = {case["id"] for case in details}
    for _ in range(2):
        seeded = client.call("/api/ops/session/demo", {})
        assert seeded["collection"]["prepared_count"] == PREPARED_COUNT
        again = client.call("/api/ops/cases?workspace=all&limit=20")
        assert {case["id"] for case in again["items"]} == expected_ids and again["total"] == PREPARED_COUNT
    overview = client.call("/api/ops/overview?workspace=all")
    assert overview["counts"]["total"] == PREPARED_COUNT
    for status, count in PREPARED_STATUSES.items():
        assert overview["counts"][status] == count
    assert overview["counts"]["queued"] == overview["counts"]["processing"] == 0
    assert overview["origin_counts"]["prepared_demo"] == PREPARED_COUNT
    assert overview["origin_counts"]["live_run"] == overview["origin_counts"].get("unknown", 0) == 0
    assert sum(overview["origin_counts"].values()) == PREPARED_COUNT
    assert overview["durations"] == [], "prepared examples must not populate live inference timings"
    assert len(overview["by_facility"]) == 3 and all(row["count"] == 4 for row in overview["by_facility"])
    assert len({row["facility_id"] for row in overview["by_facility"]}) == 3
    other = Client(client.url)
    other.session(prepared_demo=True)
    other_rows = other.call("/api/ops/cases?workspace=all&limit=20")
    assert other_rows["total"] == PREPARED_COUNT
    assert not expected_ids.intersection(case["id"] for case in other_rows["items"]), "seed clones must be independently owned"
    other.call("/api/ops/cases/" + details[0]["id"], expected=404)
    return {"count": PREPARED_COUNT, "statuses": PREPARED_STATUSES,
            "model_mode": "fixture", "idempotent_seed": True, "owner_isolation": True,
            "live_duration_samples": 0, "original_execution_receipts": True}
