"""Prepared examples cannot silently replace live execution or readable controls."""

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

from test_ops_live_instrument import case_fixture


@pytest.fixture(scope="module")
def instruments():
    directory = Path(__file__).parents[1] / "scripts"
    sys.path.insert(0, str(directory))
    try:
        modules = []
        for name in ["demo_collection_smoke", "console_browser_smoke", "ops_live_smoke"]:
            spec = importlib.util.spec_from_file_location("test_" + name, directory / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            modules.append(module)
        yield modules
    finally:
        sys.path.remove(str(directory))


def prepared_case(index=0, status="released"):
    case = case_fixture()
    execution = case["result"]["receipts"][0]["session_id"]
    case.update(id=f"{index:032x}", origin="prepared_demo", status=status, duration_ms=None,
                patient_url="/console/patient/prepared-capability" if status == "released" else None,
                prepared={"version": "prepared-demo-v1", "prepared_at": "2026-10-29T01:02:03+00:00",
                          "source_commit": "c" * 40, "original_execution_id": execution,
                          "original_artifact_hash": case["artifact_hash"]})
    case["result"]["session_id"] = execution
    case["result"]["runtime"] = {"orchestrator": "strands.multiagent.Graph"}
    if status == "failed":
        case.update(artifact_hash=None, patient_url=None)
        case["prepared"]["original_artifact_hash"] = None
        case["result"].update(status="refused", explanation=None, explanation_text=None, receipts=[])
    return case


@pytest.mark.parametrize("status", ["released", "review_required", "rejected", "failed"])
def test_prepared_case_preserves_original_fixture_execution(instruments, status):
    demo, _, _ = instruments
    case = prepared_case(100, status)
    assert case["id"] + ":1" != case["prepared"]["original_execution_id"]
    demo.validate_prepared_case(case)


@pytest.mark.parametrize("mutation", ["origin", "active", "duration", "bedrock", "correlation", "source_commit",
                                        "receipt", "artifact", "public_before_release"])
def test_misleading_prepared_provenance_or_state_is_rejected(instruments, mutation):
    demo, _, _ = instruments
    case = prepared_case()
    if mutation == "origin":
        case["origin"] = "live_run"
    elif mutation == "active":
        case["status"] = "processing"
    elif mutation == "duration":
        case["duration_ms"] = 9000
    elif mutation == "bedrock":
        case["result"]["model_mode"] = "bedrock"
    elif mutation == "correlation":
        case["prepared"]["original_execution_id"] = case["id"] + ":1"
    elif mutation == "source_commit":
        case["prepared"]["source_commit"] = "unknown"
    elif mutation == "receipt":
        case["result"]["receipts"][0]["hash"] = "f" * 64
    elif mutation == "artifact":
        case["prepared"]["original_artifact_hash"] = "d" * 64
    else:
        case["status"] = "review_required"
    with pytest.raises(AssertionError):
        demo.validate_prepared_case(case)


def test_prepared_failure_cannot_carry_patient_content(instruments):
    demo, _, _ = instruments
    case = prepared_case(status="failed")
    case["result"]["explanation_text"] = "Unchecked patient content"
    with pytest.raises(AssertionError):
        demo.validate_prepared_case(case)


class CollectionClient:
    created = 0

    def __init__(self, url):
        self.url = url
        offset = CollectionClient.created * 100
        CollectionClient.created += 1
        statuses = ["released"] * 5 + ["review_required"] * 4 + ["rejected"] * 2 + ["failed"]
        self.cases = [prepared_case(offset + index, status) for index, status in enumerate(statuses)]
        self.mode = "prepared_demo"
        self.duplicate_seed = False
        self.invented_duration = False

    def session(self, prepared_demo=False):
        assert prepared_demo is True, "second owner must request its own prepared collection"
        return {"demo": True}

    def call(self, path, payload=None, expected=200):
        if expected == 404:
            assert not any(case["id"] == path.split("/")[-1] for case in self.cases)
            return {"detail": "Not found in this owner"}
        if path in {"/api/ops/bootstrap", "/api/ops/session/demo"}:
            if path.endswith("/demo") and self.duplicate_seed:
                self.cases.append(prepared_case(999))
            return {"collection": {"mode": self.mode, "initialized": True, "prepared_count": 12, "live_count": 0}}
        if path.startswith("/api/ops/cases?"):
            return {"items": copy.deepcopy(self.cases), "total": len(self.cases), "next_cursor": None}
        if path.startswith("/api/ops/cases/"):
            return copy.deepcopy(next(case for case in self.cases if case["id"] == path.split("/")[-1]))
        if path.startswith("/api/ops/overview"):
            return {"counts": {"total": 12, "released": 5, "review_required": 4, "rejected": 2,
                               "failed": 1, "queued": 0, "processing": 0},
                    "origin_counts": {"prepared_demo": 12, "live_run": 0, "unknown": 0},
                    "durations": [{"duration_ms": 42}] if self.invented_duration else [],
                    "by_facility": [{"facility_id": name, "count": 4} for name in ["athens", "patras", "thessaloniki"]]}
        raise AssertionError("Unexpected collection request")


def test_collection_instrument_checks_idempotency_and_independent_owners(instruments, monkeypatch):
    demo, _, _ = instruments
    CollectionClient.created = 0
    monkeypatch.setattr(demo, "Client", CollectionClient)
    proof = demo.audit_prepared_collection(CollectionClient("https://example.invalid"))
    assert proof["count"] == 12 and proof["owner_isolation"] and proof["idempotent_seed"]
    assert CollectionClient.created == 2


@pytest.mark.parametrize("mutation", ["duplicate_seed", "invented_duration", "empty"])
def test_collection_instrument_rejects_fake_population_or_live_measurements(instruments, monkeypatch, mutation):
    demo, _, _ = instruments
    CollectionClient.created = 0
    monkeypatch.setattr(demo, "Client", CollectionClient)
    client = CollectionClient("https://example.invalid")
    if mutation == "empty":
        client.mode = "empty"
    else:
        setattr(client, mutation, True)
    with pytest.raises(AssertionError):
        demo.audit_prepared_collection(client)


def test_native_option_contrast_checker_has_nonmatching_negative_controls(instruments):
    _, browser, _ = instruments
    assert browser.contrast_ratio("rgb(0, 0, 0)", "rgb(255, 255, 255)") == pytest.approx(21)
    assert browser.contrast_ratio("color(srgb 1 1 1)", "color(srgb 0 0 0)") == pytest.approx(21)
    assert browser.contrast_ratio("rgb(245, 245, 245)", "rgb(255, 255, 255)") < 1.1
    assert browser.contrast_ratio("rgb(150, 150, 150)", "rgb(255, 255, 255)") < 4.5
    for transparent in ["rgba(0, 0, 0, 0)", "rgba(255, 255, 255, 0.5)"]:
        with pytest.raises(AssertionError, match="opaque"):
            browser.contrast_ratio("rgb(0, 0, 0)", transparent)


def test_live_http_instrument_explicitly_requests_empty_sessions(instruments, monkeypatch):
    _, _, live = instruments
    requests = []
    client = live.Client("https://example.invalid")

    def call(path, payload=None, expected=200):
        requests.append((path, payload, expected))
        return {"session_token": "issued-test-capability", "demo": True, "role": "operator"}

    monkeypatch.setattr(client, "call", call)
    client.session()
    client.session(prepared_demo=True)
    assert requests == [("/api/ops/session", {"prepared_demo": False}, 201),
                        ("/api/ops/session", {"prepared_demo": True}, 201)]
