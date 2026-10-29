"""Absent, stale or corrupt measurements never become reassuring scores."""
import copy
import importlib.util
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from medplain.evidence_api import router
from medplain.gate import canonical_hash
from medplain.live_evaluation import SK, record_key, validate_manifest
from medplain.ops_store import MemoryStore, StoreUnavailable

SOURCE = "a" * 40
URL = "https://github.com/upgradedev/atropos-medplain-aws/releases/download/synthetic-test/report.json"


def publisher():
    spec = importlib.util.spec_from_file_location("reconciliation_publisher", Path("scripts/publish_reconciliation_evaluation.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def report():
    focus = {"precision": None, "recall": None, "f1": None}
    arm = {"case_count": 30, "verified_count": 0, "workflow_refused_count": 0,
           "runtime_error_count": 0, "not_executed_count": 30, "source_coverage_count": 0,
           "source_citations_count": 0, "supported_atoms_exact_count": 0, "proper_withholding_count": 0,
           "question_focus": focus, "structural_focus": focus, "selected_interaction_review": focus}
    body = {"schema_version": 1, "profile": "medplain-reconciliation-v1", "mode": "bedrock",
            "source_commit": SOURCE, "harness_commit": SOURCE,
            "arms": {"strands": arm, "baseline": copy.deepcopy(arm)},
            "cases": [{"id": f"unexecuted-{index}"} for index in range(30)],
            "corpus": {"sha256": "b" * 64}, "ruler": {"sha256": "c" * 64},
            "references": {"snapshot_sha256": "d" * 64}, "generated_at": "2026-10-29T21:00:00Z",
            "measurement_status": {"outcome_validity": "contains_failures"}}
    return {**body, "report_sha256": canonical_hash(body)}


def signed_manifest():
    return publisher().make_manifest(report(), URL)


@pytest.fixture
def public_client(monkeypatch):
    store = MemoryStore()
    monkeypatch.setattr("medplain.evidence_api.commit", lambda: SOURCE)
    monkeypatch.setattr("medplain.ops_store.get_store", lambda: store)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client, store


def put(store, manifest):
    store.put_new(record_key(SOURCE), SK, {"ttl": int(time.time()) + 300, "manifest": manifest})


def test_no_measurement_has_explicit_reason_and_no_placeholder_counts(public_client):
    client, _ = public_client
    response = client.get("/api/reconciliation-evaluations/live")
    assert response.status_code == 200
    value = response.json()
    assert value["status"] == "not_measured" and value["source_commit"] == SOURCE
    assert "reason" in value and "arms" not in value


def test_full_denominator_including_failures_is_published_without_turning_green(public_client):
    client, store = public_client
    manifest = signed_manifest()
    put(store, manifest)
    response = client.get("/api/reconciliation-evaluations/live")
    assert response.status_code == 200 and response.json() == manifest
    assert response.json()["measurement_status"]["outcome_validity"] == "contains_failures"
    assert response.json()["arms"]["strands"]["not_executed_count"] == 30


@pytest.mark.parametrize("mutation", ["hash", "source", "denominator", "mode", "url", "nonfinite", "missing"])
def test_corrupt_or_rehashed_wrong_metadata_is_unavailable(public_client, mutation):
    client, store = public_client
    manifest = signed_manifest()
    if mutation == "hash":
        manifest["arms"]["strands"]["verified_count"] = 30
    elif mutation == "source":
        manifest["source_commit"] = "e" * 40
    elif mutation == "denominator":
        manifest["arms"]["strands"]["case_count"] = 1
    elif mutation == "mode":
        manifest["mode"] = "fixture"
    elif mutation == "url":
        manifest["report_url"] = "https://example.com/not-our-public-report.json"
    elif mutation == "nonfinite":
        manifest["arms"]["strands"]["question_focus"]["recall"] = 3
    else:
        del manifest["arms"]["strands"]["source_coverage_count"]
    if mutation != "hash":
        manifest["manifest_sha256"] = canonical_hash({k: v for k, v in manifest.items() if k != "manifest_sha256"})
    put(store, manifest)
    response = client.get("/api/reconciliation-evaluations/live")
    assert response.status_code == 503 and "arms" not in response.json()


def test_storage_outage_is_distinct_from_unmeasured(public_client, monkeypatch):
    client, _ = public_client

    def unavailable():
        raise StoreUnavailable()

    monkeypatch.setattr("medplain.ops_store.get_store", unavailable)
    response = client.get("/api/reconciliation-evaluations/live")
    assert response.status_code == 503
    assert "No comparison scores" in response.json()["detail"]


def test_manifest_self_hash_matches_shared_runtime_contract():
    value = signed_manifest()
    assert validate_manifest(value, SOURCE) == value


@pytest.mark.parametrize("mutation", ["source", "fixture", "missing_rows", "bad_hash"])
def test_publisher_refuses_changed_or_incomplete_measurements(mutation):
    value = report()
    if mutation == "source":
        value["harness_commit"] = "f" * 40
    elif mutation == "fixture":
        value["mode"] = "fixture"
    elif mutation == "missing_rows":
        value["cases"].pop()
    else:
        value["report_sha256"] = "0" * 64
    if mutation != "bad_hash":
        value["report_sha256"] = canonical_hash({k: v for k, v in value.items() if k != "report_sha256"})
    with pytest.raises(ValueError):
        publisher().make_manifest(value, URL)
