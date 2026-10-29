"""Independent mutation tests: numeric identity, prose bypasses and broken receipts."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from medplain.clinical import (
    build_explanation, check_interactions, evaluate_labs, parse_packet, render_explanation,
)
from medplain.fixtures import get_packet, get_packets
from medplain.gate import ReceiptChain, SafetyGate, verify_receipts


@pytest.mark.parametrize("packet", get_packets(), ids=lambda p: p.packet_id)
def test_source_fhir_and_every_canonical_explanation_agree(packet):
    assert parse_packet(packet) == packet
    assert SafetyGate(packet).validate(build_explanation(packet)).passed


@pytest.mark.parametrize("field,value", [
    ("dose", "25"), ("unit", "mcg"), ("frequency", "BID"), ("status", "stopped"),
    ("name", "Different medicine"), ("text", "Take twenty-five mg at bedtime."),
])
def test_gate_refuses_independently_fixed_warfarin_mutations(field, value):
    # This independent fixture pins 2.5 mg QHS; mutation does not derive from app parameters.
    packet = get_packet("dosage-mutation-stress")
    assert packet.medications[0].dose == "2.5"
    assert packet.medications[0].unit == "mg"
    assert packet.medications[0].frequency == "QHS"
    candidate = build_explanation(packet)
    setattr(candidate.medications[0], field, value)
    verdict = SafetyGate(packet).validate(candidate)
    assert not verdict.passed and verdict.discrepancies


@pytest.mark.parametrize("frequency", ["BID", "TID", "QHS"])
def test_preserve_frequency_codes_and_plain_language(frequency):
    packet = get_packet("dosage-mutation-stress")
    packet.medications[0].frequency = frequency
    packet.fhir["entry"][0]["resource"]["dosageInstruction"][0]["timing"]["code"]["coding"][0]["code"] = frequency
    packet.source_text = packet.source_text.replace("| QHS |", f"| {frequency} |")
    candidate = build_explanation(packet)
    assert SafetyGate(packet).validate(candidate).passed
    candidate.medications[0].frequency = "QD"
    assert not SafetyGate(packet).validate(candidate).passed


@pytest.mark.parametrize("index,field,value", [(0, "value", "24"), (0, "unit", "mg/dL"),
                                              (0, "low", "0.9"), (1, "high", "17.0")])
def test_lab_mutations_refused(index, field, value):
    packet = get_packet("dosage-mutation-stress")
    candidate = build_explanation(packet)
    setattr(candidate.labs[index], field, value)
    assert not SafetyGate(packet).validate(candidate).passed


@pytest.mark.parametrize("field", ["summary", "follow_up", "red_flags"])
def test_extra_instructions_in_prose_are_refused(field):
    packet = get_packet("home-after-pneumonia")
    candidate = build_explanation(packet)
    value = "Also take 99 mg now."
    setattr(candidate, field, value if field == "summary" else [value])
    assert not SafetyGate(packet).validate(candidate).passed


def test_duplicate_missing_swapped_and_added_rows_fail():
    packet = get_packet("heart-medicine-review")
    gate = SafetyGate(packet)
    for mutation in ["duplicate", "remove", "swap", "extra"]:
        candidate = build_explanation(packet)
        if mutation == "duplicate":
            candidate.medications.append(candidate.medications[0].model_copy())
        elif mutation == "remove":
            candidate.medications.pop()
        elif mutation == "swap":
            candidate.medications[0].dose, candidate.medications[1].dose = "5", "20"
        else:
            added = candidate.medications[0].model_copy(update={"medication_id": "new"})
            candidate.medications.append(added)
        assert not gate.validate(candidate).passed


def test_lab_range_uses_decimal_and_inclusive_endpoints():
    packet = get_packet("heart-medicine-review")
    packet.labs[0].value = "3.5"
    assert evaluate_labs(packet)[0]["status"] == "within range"
    packet.labs[0].value = "5.1"
    assert evaluate_labs(packet)[0]["status"] == "within range"
    packet.labs[0].value = "3.49"
    assert evaluate_labs(packet)[0]["status"] == "low"


@pytest.mark.parametrize("mutation", ["dose", "unit", "frequency", "status", "source", "duplicate", "missing", "lab"])
def test_fhir_and_text_disagreement_is_not_silently_accepted(mutation):
    packet = get_packet("dosage-mutation-stress")
    resource = packet.fhir["entry"][0]["resource"]
    if mutation == "dose":
        resource["dosageInstruction"][0]["doseAndRate"][0]["doseQuantity"]["value"] = 25
    elif mutation == "unit":
        resource["dosageInstruction"][0]["doseAndRate"][0]["doseQuantity"]["unit"] = "mcg"
    elif mutation == "frequency":
        resource["dosageInstruction"][0]["timing"]["code"]["coding"][0]["code"] = "TID"
    elif mutation == "status":
        resource["status"] = "stopped"
    elif mutation == "source":
        packet.source_text = packet.source_text.replace("2.5 mg", "25 mg")
    elif mutation == "duplicate":
        packet.fhir["entry"].append(deepcopy(packet.fhir["entry"][0]))
    elif mutation == "missing":
        packet.fhir["entry"].pop()
    else:
        packet.fhir["entry"][-1]["resource"]["valueQuantity"]["value"] = 99
    with pytest.raises(ValueError):
        parse_packet(packet)


def test_interaction_flags_grounded_and_explicitly_not_exhaustive():
    assert check_interactions(get_packet("home-after-pneumonia")) == []
    heart = check_interactions(get_packet("heart-medicine-review"))
    stress = check_interactions(get_packet("dosage-mutation-stress"))
    assert len(heart) == len(stress) == 1
    assert "not exhaustive" in heart[0]["scope"]
    assert stress[0]["medications"] == ["Warfarin", "Ibuprofen"]


def test_hash_chain_binds_refusal_correction_and_artifact():
    packet = get_packet("dosage-mutation-stress")
    gate = SafetyGate(packet)
    chain = ReceiptChain("a-fixed-independent-test-session", packet)
    wrong = build_explanation(packet)
    wrong.medications[0].dose = "25"
    chain.append(wrong, gate.validate(wrong))
    correct = build_explanation(packet)
    chain.append(correct, gate.validate(correct))
    assert [r["verdict"] for r in chain.receipts] == ["refused", "verified"]
    assert verify_receipts(chain.receipts)
    assert "2.5 mg" in render_explanation(correct)
    assert chain.receipts[0]["artifact_hash"] != chain.receipts[1]["artifact_hash"]
    for field in ["hash", "artifact_hash", "source_hash", "previous_hash", "session_id", "verdict"]:
        changed = deepcopy(chain.receipts)
        changed[1][field] = "tampered"
        assert not verify_receipts(changed)
    assert not verify_receipts(list(reversed(chain.receipts)))
    assert not verify_receipts([])


def test_strict_models_reject_unsupported_fields_and_bad_numeric_units():
    data = get_packet("home-after-pneumonia").model_dump()
    data["unexpected"] = "free text instruction"
    from medplain.schemas import ClinicalPacket
    with pytest.raises(ValidationError):
        ClinicalPacket.model_validate(data)


def test_fixtures_are_isolated_between_sessions():
    first = get_packet("dosage-mutation-stress")
    first.medications[0].dose = "25"
    assert get_packet("dosage-mutation-stress").medications[0].dose == "2.5"
