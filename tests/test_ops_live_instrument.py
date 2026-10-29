"""Independent live instrument must reject coherent but clinically changed JSON."""

import copy
import importlib.util
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from medplain.clinical import build_explanation, render_explanation
from medplain.fixtures import get_packet, get_packets
from medplain.gate import ReceiptChain, SafetyGate, canonical_hash


@pytest.fixture(scope="module")
def instrument():
    directory = Path(__file__).parents[1] / "scripts"
    spec = importlib.util.spec_from_file_location("ops_live_instrument", directory / "ops_live_smoke.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(directory))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(directory))
    return module


def case_fixture(packet_id="dosage-mutation-stress"):
    packet = get_packet(packet_id)
    explanation = build_explanation(packet)
    case_id = "a" * 32
    chain = ReceiptChain(case_id + ":1", packet)
    chain.append(explanation, SafetyGate(packet).validate(explanation))
    return {"id": case_id, "generation": 1, "source": packet.model_dump(),
            "source_hash": canonical_hash(packet.model_dump()), "artifact_hash": chain.receipts[-1]["artifact_hash"],
            "result": {"status": "verified", "explanation": explanation.model_dump(),
                       "explanation_text": render_explanation(explanation), "receipts": chain.receipts,
                       "model_mode": "fixture", "correction_count": 0, "commit": "b" * 40}}


def rebind(case):
    """An internally coherent forgery still must fail source fidelity checks."""
    explanation = case["result"]["explanation"]
    case["result"]["explanation_text"] = "\n\n".join([
        explanation["summary"], *[med["text"] for med in explanation["medications"]],
        *[lab["text"] for lab in explanation["labs"]],
        "Follow-up\n" + "\n".join(explanation["follow_up"]),
        "When to get help\n" + "\n".join(explanation["red_flags"]),
    ])
    case["source_hash"] = canonical_hash(case["source"])
    case["artifact_hash"] = canonical_hash({"structured": explanation, "text": case["result"]["explanation_text"]})
    previous = "0" * 64
    for receipt in case["result"]["receipts"]:
        receipt.update(source_hash=case["source_hash"], artifact_hash=case["artifact_hash"], previous_hash=previous)
        receipt["hash"] = canonical_hash({key: value for key, value in receipt.items() if key != "hash"})
        previous = receipt["hash"]


@pytest.mark.parametrize("packet_id", [packet.packet_id for packet in get_packets()])
def test_all_published_profiles_pass_independent_source_and_text_contract(instrument, packet_id):
    case = case_fixture(packet_id)
    assert instrument.validate_artifact(case) is case["result"]


def test_coherently_rehashed_tenfold_dose_is_rejected(instrument):
    case = case_fixture()
    assert case["source"]["medications"][0]["dose"] == "2.5"
    med = case["result"]["explanation"]["medications"][0]
    med["dose"] = "25"
    med["text"] = med["text"].replace("2.5 mg", "25 mg")
    rebind(case)
    assert instrument.verify_chain(case["result"]["receipts"])
    assert case["result"]["receipts"][-1]["artifact_hash"] == case["artifact_hash"]
    with pytest.raises(AssertionError, match="patient facts"):
        instrument.validate_artifact(case)


@pytest.mark.parametrize("field,value", [
    ("medication_id", "different-id"), ("name", "Different medicine"), ("dose", "25"),
    ("unit", "mcg"), ("frequency", "BID"), ("status", "stopped"), ("text", "Unsupported advice"),
])
def test_every_medication_field_is_checked_even_after_rehash(instrument, field, value):
    case = case_fixture()
    case["result"]["explanation"]["medications"][0][field] = value
    rebind(case)
    assert instrument.verify_chain(case["result"]["receipts"])
    with pytest.raises(AssertionError, match="patient facts"):
        instrument.validate_artifact(case)


@pytest.mark.parametrize("field,value", [
    ("lab_id", "different-lab"), ("name", "Different laboratory test"), ("value", "999"),
    ("unit", "different-unit"), ("low", "-999"), ("high", "999"),
    ("status", "unknown"), ("text", "Unsupported laboratory interpretation"),
])
def test_every_lab_field_and_classification_is_checked_after_rehash(instrument, field, value):
    case = case_fixture()
    case["result"]["explanation"]["labs"][0][field] = value
    rebind(case)
    with pytest.raises(AssertionError, match="patient facts"):
        instrument.validate_artifact(case)


@pytest.mark.parametrize("mutation", ["summary", "follow_up", "red_flags", "missing_medication",
                                        "duplicate_medication", "missing_lab", "duplicate_lab", "extra_field"])
def test_omissions_duplicates_and_unsupported_narratives_are_rejected(instrument, mutation):
    case = case_fixture()
    explanation = case["result"]["explanation"]
    if mutation == "summary":
        explanation["summary"] = "Unsupported summary"
    elif mutation in {"follow_up", "red_flags"}:
        explanation[mutation] = ["Unsupported care instruction"]
    elif mutation == "extra_field":
        explanation["extra_advice"] = "Take another medicine"
    else:
        collection = "medications" if mutation.endswith("medication") else "labs"
        if mutation.startswith("missing"):
            explanation[collection].pop(0)
        else:
            explanation[collection].append(copy.deepcopy(explanation[collection][0]))
    rebind(case)
    with pytest.raises(AssertionError, match="patient facts"):
        instrument.validate_artifact(case)


@pytest.mark.parametrize("mutation", ["duplicate_medication", "duplicate_lab", "invalid_unit", "invalid_status",
                                        "invalid_frequency", "inverted_range", "nan_lab", "missing_field"])
def test_unsupported_source_profile_cannot_be_self_consistently_rehashed(instrument, mutation):
    case = case_fixture()
    source = case["source"]
    if mutation == "duplicate_medication":
        source["medications"].append(copy.deepcopy(source["medications"][0]))
    elif mutation == "duplicate_lab":
        source["labs"].append(copy.deepcopy(source["labs"][0]))
    elif mutation == "invalid_unit":
        source["medications"][0]["unit"] = "g"
    elif mutation == "invalid_status":
        source["medications"][0]["status"] = "unknown"
    elif mutation == "invalid_frequency":
        source["medications"][0]["frequency"] = "PRN"
    elif mutation == "inverted_range":
        source["labs"][0].update(low="99", high="1")
    elif mutation == "nan_lab":
        source["labs"][0]["value"] = "NaN"
    else:
        source.pop("summary")
    rebind(case)
    with pytest.raises(AssertionError, match="patient facts"):
        instrument.validate_artifact(case)


def test_changed_rendered_text_is_rejected_even_with_coherent_final_hash(instrument):
    case = case_fixture()
    case["result"]["explanation_text"] += "\nTake extra medicine."
    case["artifact_hash"] = canonical_hash({"structured": case["result"]["explanation"],
                                             "text": case["result"]["explanation_text"]})
    receipt = case["result"]["receipts"][-1]
    receipt["artifact_hash"] = case["artifact_hash"]
    receipt["hash"] = canonical_hash({key: value for key, value in receipt.items() if key != "hash"})
    assert instrument.verify_chain(case["result"]["receipts"])
    with pytest.raises(AssertionError, match="patient facts"):
        instrument.validate_artifact(case)


@pytest.mark.parametrize("field,value", [("source_hash", "c" * 64), ("artifact_hash", "d" * 64), ("generation", 2)])
def test_valid_clinical_text_still_requires_source_artifact_and_generation_binding(instrument, field, value):
    case = case_fixture()
    case[field] = value
    with pytest.raises(AssertionError, match="bound together"):
        instrument.validate_artifact(case)


def test_stale_worker_commit_fails_and_matching_commit_passes(instrument):
    expected = "b" * 40
    instrument.validate_worker_commit({"commit": expected}, expected)
    for result in ({"commit": "a" * 40}, {}):
        with pytest.raises(AssertionError, match="worker commit"):
            instrument.validate_worker_commit(result, expected)


def test_publication_smoke_actually_rejects_stale_worker(instrument, monkeypatch):
    case = case_fixture("home-after-pneumonia")
    case.update(status="released", origin="live_run", duration_ms=10)
    case["result"]["commit"] = "old-worker-code"

    class InstrumentClient:
        instances = 0

        def __init__(self, url):
            self.index = InstrumentClient.instances
            InstrumentClient.instances += 1

        def session(self):
            return {"demo": True, "role": "operator"}

        def call(self, path, payload=None, expected=200):
            if path == "/":
                return b"Atropos console"
            if path == "/api/health":
                return {"commit": "b" * 40, "surface": "operations-console", "model_mode": "fixture"}
            if path == "/api/ops/bootstrap":
                return {"facilities": [{"id": str(index), "synthetic": True} for index in range(3)],
                        "packets": [{"id": packet.packet_id, "stress_available": packet.adversarial}
                                    for packet in get_packets()]}
            if path == "/api/ops/session/role":
                return {}
            if path == "/api/ops/cases":
                return {"case": {"id": case["id"]}, "created": False}
            if path == "/api/ops/cases?limit=20":
                return {"total": 0}
            if path.startswith("/api/ops/cases/"):
                assert self.index == 1 and expected == 404
                return {}
            raise AssertionError("Unexpected instrument request")

    monkeypatch.setattr(instrument, "Client", InstrumentClient)
    monkeypatch.setattr(instrument, "wait_case", lambda *args: case)
    with pytest.raises(AssertionError, match="worker commit"):
        instrument.smoke("https://example.invalid", "b" * 40, allow_fixture=True)


def test_cursor_is_encoded_without_changing_opaque_value(instrument):
    cursor = "opaque+/with=padding==&not-a-query"
    path = instrument.next_page_path(cursor)
    query = parse_qs(urlsplit(path).query)
    assert query["cursor"] == [cursor]
    assert query["workspace"] == ["all"] and query["direction"] == ["asc"]
    assert "%2B" in path and "%2F" in path and "%26" in path
