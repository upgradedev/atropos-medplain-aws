"""Durable evidence cannot become a publication bypass, even after rehashing."""

import copy

import pytest

from medplain.engine import run_packet
from medplain.fixtures import get_packet
from medplain.gate import GENESIS, canonical_hash
from medplain.ops_validation import validate_result


@pytest.fixture(scope="module")
def evidence():
    packet = get_packet("dosage-mutation-stress")
    result = run_packet(packet, "case-a:1", model_mode="fixture", stress=True)
    assert result["status"] == "verified"
    return packet, result


def rechain(result):
    previous = GENESIS
    for receipt in result["receipts"]:
        receipt["previous_hash"] = previous
        receipt["hash"] = canonical_hash({key: value for key, value in receipt.items() if key != "hash"})
        previous = receipt["hash"]


def test_verified_corrected_artifact_passes_independent_validation(evidence):
    packet, result = evidence
    assert validate_result(packet, result, "case-a:1") == result["receipts"][-1]["artifact_hash"]


def test_prior_generation_cannot_be_replayed(evidence):
    packet, result = evidence
    with pytest.raises(ValueError, match="execution"):
        validate_result(packet, result, "case-a:2")


@pytest.mark.parametrize("field,value", [
    ("session_id", "other:1"), ("source_hash", "a" * 64), ("artifact_hash", "b" * 64),
])
def test_rehashed_final_receipt_still_needs_source_session_and_artifact(evidence, field, value):
    packet, original = evidence
    result = copy.deepcopy(original)
    for receipt in result["receipts"]:
        receipt[field] = value
    rechain(result)
    with pytest.raises(ValueError):
        validate_result(packet, result, "case-a:1")


@pytest.mark.parametrize("field,value", [
    ("dose", "25"), ("unit", "mcg"), ("frequency", "BID"),
    ("text", "Take one hundred tablets before bed."),
])
def test_rehashed_mutated_artifact_fails_the_independent_gate(evidence, field, value):
    from medplain.clinical import render_explanation
    from medplain.schemas import PatientExplanation

    packet, original = evidence
    result = copy.deepcopy(original)
    result["explanation"]["medications"][0][field] = value
    candidate = PatientExplanation.model_validate(result["explanation"])
    result["explanation_text"] = render_explanation(candidate)
    result["receipts"][-1]["artifact_hash"] = canonical_hash({
        "structured": candidate.model_dump(), "text": result["explanation_text"],
    })
    rechain(result)
    with pytest.raises(ValueError, match="source-derived"):
        validate_result(packet, result, "case-a:1")


@pytest.mark.parametrize("mutation", ["text", "corrections", "verdict", "receipt", "mode", "error"])
def test_inconsistent_saved_metadata_is_not_a_publication_verdict(evidence, mutation):
    packet, original = evidence
    result = copy.deepcopy(original)
    if mutation == "text":
        result["explanation_text"] += " Take an extra dose."
    elif mutation == "corrections":
        result["correction_count"] = 0
    elif mutation == "verdict":
        result["receipts"][-1]["verdict"] = "refused"
        result["receipts"][-1]["discrepancies"] = ["changed"]
        rechain(result)
    elif mutation == "receipt":
        result["receipts"][0]["hash"] = "0" * 64
    elif mutation == "mode":
        result["model_mode"] = "silent-fallback"
    else:
        result["error"] = "Provider failed after completion"
    with pytest.raises(ValueError):
        validate_result(packet, result, "case-a:1")


def test_valid_source_snapshot_different_from_saved_source_is_rejected(evidence):
    packet, result = evidence
    changed = packet.model_copy(deep=True)
    changed.title = "A different source snapshot"
    with pytest.raises(ValueError, match="source"):
        validate_result(changed, result, "case-a:1")


def test_refusal_before_first_candidate_carries_no_patient_content(evidence):
    packet, _ = evidence
    result = {"status": "refused", "session_id": "case-a:1", "model_mode": "fixture", "receipts": [],
              "explanation": None, "explanation_text": None}
    assert validate_result(packet, result, "case-a:1") is None
    result["explanation_text"] = "An unchecked explanation"
    with pytest.raises(ValueError, match="refused"):
        validate_result(packet, result, "case-a:1")


def test_refusal_receipt_must_remain_bound_to_the_source(evidence):
    packet, original = evidence
    result = copy.deepcopy(original)
    result.update(status="refused", explanation=None, explanation_text=None)
    result["receipts"] = result["receipts"][:1]
    assert validate_result(packet, result, "case-a:1") is None
    result["receipts"][0]["source_hash"] = "a" * 64
    rechain(result)
    with pytest.raises(ValueError):
        validate_result(packet, result, "case-a:1")
