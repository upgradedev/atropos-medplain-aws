"""Independent literal expectations and negative tests for the new profile."""

import copy

import pytest
from pydantic import ValidationError

from medplain.reconciliation import (
    PROFILE, artifact_digest, baseline_selection, build_artifact, deterministic_baseline,
    examples, gate_errors, source_statements, structural_issues, validate_dossier,
    validate_reconciliation_result, validate_selection,
)
from medplain.reconciliation_models import PublicationSelection


def dossier(first="Take Aspirin 81 mg by mouth QD.", second="Stop Aspirin 81 mg by mouth QD."):
    return {"profile": PROFILE, "patient_alias": "Demo-Independent", "language": "en", "caregiver_concerns": "",
            "documents": [
                {"document_id": "home", "kind": "home_list", "issued_at": "2026-10-01T09:00:00Z", "effective_at": None, "text": first},
                {"document_id": "discharge", "kind": "discharge", "issued_at": "2026-10-02T09:00:00Z", "effective_at": None, "text": second}]}


@pytest.mark.parametrize("example", examples(), ids=lambda row: row["example_id"])
def test_complete_baseline_accepts_every_family_without_prescribing(example):
    source = validate_dossier(example["dossier"])
    result = deterministic_baseline(source)
    validate_reconciliation_result(source, result)
    assert result["status"] == "verified"
    assert result["patient_regimen"] is None
    assert result["artifact"]["patient_regimen"] is None
    assert "not a new prescription" in result["artifact"]["notice"] or "όχι νέα συνταγή" in result["artifact"]["notice"]


def test_action_does_not_leak_across_medicine_clauses():
    source = dossier(second="Stop Aspirin 81 mg by mouth QD; take Metformin 500 mg by mouth BID.")
    medications = source_statements(source)[1]["clinical"]["medications"]
    assert [(row["name"], row["dose"], row["frequency"], row["action"]) for row in medications] == [
        ("Aspirin", "81", "QD", "stop"), ("Metformin", "500", "BID", "take")]


def test_hold_stays_distinct_and_unsupported_negation_is_uncertain():
    source = dossier(second="Hold Aspirin 81 mg by mouth QD.")
    rows = source_statements(source)
    assert rows[1]["clinical"]["medications"][0]["action"] == "hold"
    assert any(row["code"] == "stop_status_conflict" for row in structural_issues(rows))
    source["documents"][1]["text"] = "Do not stop Aspirin 81 mg by mouth QD."
    row = source_statements(source)[1]
    assert row["clinical"]["medications"][0]["action"] == "uncertain"
    assert row["clinical"]["uncertain"] is True
    assert row["citation"]["quote"] == "Do not stop Aspirin 81 mg by mouth QD."


@pytest.mark.parametrize("prefix", ["Do not discontinue", "Not currently taking", "No longer using", "Never take"])
def test_unrecognized_negations_never_become_confident_stop_or_take(prefix):
    source = dossier(second=f"{prefix} Aspirin 81 mg by mouth QD.")
    row = source_statements(source)[1]
    assert row["clinical"]["medications"][0]["action"] == "uncertain"
    assert row["clinical"]["uncertain"] is True


def test_partial_known_regimen_keeps_unsupported_modifier_uncertain():
    source = dossier(second="Take Aspirin 81 mg by mouth QD except on clinic days.")
    assert source_statements(source)[1]["clinical"]["uncertain"] is True


@pytest.mark.parametrize("anticoagulant,other,label", [
    ("Warfarin", "Ibuprofen", "dailymed-warfarin"), ("Warfarin", "Naproxen", "dailymed-warfarin"),
    ("Apixaban", "Ibuprofen", "dailymed-apixaban"), ("Apixaban", "Naproxen", "dailymed-apixaban"),
])
def test_selected_label_review_floor_is_bounded_and_source_cited(anticoagulant, other, label):
    source = dossier(first=f"Taking {anticoagulant} 2.5 mg by mouth QD.", second=f"Take {other} 200 mg by mouth TID.")
    result = deterministic_baseline(source)
    issues = [row for row in result["artifact"]["structural_issues"] if row["code"] == "selected_label_interaction_review"]
    assert len(issues) == 1
    assert issues[0]["statement_ids"] == ["home:L1", "discharge:L1"]
    assert issues[0]["reference_ids"] == [label]
    assert issues[0]["classification"] == "selected_label_reference"
    assert "not established" in issues[0]["scope"]
    assert "not a confirmed contraindication" in issues[0]["question_text"]
    validate_reconciliation_result(source, result)


def test_selected_label_review_does_not_claim_comprehensive_or_current_exposure():
    source = dossier("Stop Warfarin 2.5 mg by mouth QD.", "Stop Ibuprofen 200 mg by mouth TID.")
    assert not any(row["code"] == "selected_label_interaction_review" for row in structural_issues(source_statements(source)))
    source = dossier("Taking Aspirin 81 mg by mouth QD.", "Take Metformin 500 mg by mouth BID.")
    result = deterministic_baseline(source)
    assert not any(row["code"] == "selected_label_interaction_review" for row in result["artifact"]["structural_issues"])
    assert "unavailable" in result["artifact"]["references"]["interaction_service"]


def test_no_latest_wins_and_no_automatic_stop_resolution():
    issues = structural_issues(source_statements(dossier()))
    conflict = next(row for row in issues if row["code"] == "stop_status_conflict")
    assert conflict["statement_ids"] == ["home:L1", "discharge:L1"]
    assert conflict["requires_clarification"] is True


def test_explicit_intentional_change_is_not_labelled_unexplained_error():
    source = examples()[1]["dossier"]
    issues = structural_issues(source_statements(source))
    assert any(row["code"] == "documented_intentional_change" and not row["requires_clarification"] for row in issues)
    assert not any(row["code"] in {"dose_schedule_conflict", "stop_status_conflict"} for row in issues)


def test_taper_and_hold_keep_complete_order_condition_and_authority():
    taper = source_statements(examples()[2]["dossier"])[0]["clinical"]
    assert taper["medications"] == []
    assert taper["taper_medicine"] == "Prednisone"
    assert [(phase["sequence"], phase["dose"], phase["start_day"], phase["end_day"]) for phase in taper["taper_phases"]] == [
        (1, "20", "1", "3"), (2, "10", "4", "6")]
    assert taper["terminal_action"] == "stop"
    held = source_statements(examples()[3]["dossier"])[1]["clinical"]
    assert held["condition_text"] == "systolic blood pressure < 100 mmHg AND dizziness = present"
    assert held["logic_tokens"] == ["<", "AND", "="]
    assert held["restart_authority"] == "only after the discharge team confirms."


def test_unknown_clinical_prose_is_retained_not_omitted_or_rejected():
    source = dossier(second="The chart has unfamiliar instructions: use the prior plan unless the clinic calls.")
    result = deterministic_baseline(source)
    row = result["artifact"]["source_statements"][1]
    assert row["citation"]["quote"] == source["documents"][1]["text"]
    assert row["clinical"]["uncertain"] is True
    assert any(issue["code"] == "uncertain_source_wording" for issue in result["artifact"]["structural_issues"])
    validate_reconciliation_result(source, result)


def test_unicode_crlf_citation_offsets_are_raw_bytes_and_physical_lines():
    source = dossier(second="\r\nΔοκιμή\r\nTake Aspirin 81 mg by mouth QD.\r\n")
    rows = [row for row in source_statements(source) if row["citation"]["document_id"] == "discharge"]
    assert [row["citation"]["line"] for row in rows] == [2, 3]
    raw = source["documents"][1]["text"].encode("utf-8")
    for row in rows:
        citation = row["citation"]
        assert raw[citation["byte_start"]:citation["byte_end"]].decode("utf-8") == citation["quote"]


@pytest.mark.parametrize("location", ["document", "concerns"])
def test_unpaired_surrogates_fail_before_source_hashing_or_model_use(location):
    source = dossier()
    if location == "document":
        source["documents"][0]["text"] += chr(0xD800)
    else:
        source["caregiver_concerns"] = chr(0xDFFF)
    with pytest.raises(ValueError, match="valid UTF-8"):
        validate_dossier(source)


def test_real_greek_and_emoji_are_valid_quoted_utf8():
    source = dossier(second="Ο φροντιστής έχει μια ερώτηση 🙂")
    rows = source_statements(source)
    assert rows[1]["citation"]["quote"] == "Ο φροντιστής έχει μια ερώτηση 🙂"


@pytest.mark.parametrize("mutation", ["quote", "dose", "unit", "frequency", "action", "phase", "condition", "logic", "authority", "floor", "extra_advice", "regimen"])
def test_gate_rejects_coherent_clinical_or_provenance_mutations(mutation):
    source = copy.deepcopy(examples()[3 if mutation in {"condition", "logic", "authority"} else 2 if mutation == "phase" else 0]["dossier"])
    selection = baseline_selection(source)
    artifact = build_artifact(source, selection)
    row = artifact["source_statements"][0]
    if mutation == "quote":
        row["citation"]["quote"] = row["citation"]["quote"].replace("10", "100")
    elif mutation in {"dose", "unit", "frequency", "action"}:
        row["clinical"]["medications"][0][mutation] = {"dose": "100", "unit": "mcg", "frequency": "TID", "action": "stop"}[mutation]
    elif mutation == "phase":
        row["clinical"]["taper_phases"].reverse()
    elif mutation in {"condition", "logic", "authority"}:
        held = artifact["source_statements"][1]["clinical"]
        if mutation == "condition":
            held["condition_text"] = held["condition_text"].replace("<", ">")
        elif mutation == "logic":
            held["logic_tokens"][1] = "OR"
        else:
            held["restart_authority"] = "when the caregiver decides"
    elif mutation == "floor":
        artifact["structural_issues"] = []
    elif mutation == "extra_advice":
        artifact["new_advice"] = "Resume the old medicine."
    else:
        artifact["patient_regimen"] = []
    assert gate_errors(source, artifact, selection)


@pytest.mark.parametrize("mutation", ["stale_source", "missing_quote", "forged_hash", "different_execution", "edited_text", "omitted_regimen_key", "bad_receipts"])
def test_durable_validator_independently_checks_saved_result(mutation):
    source = dossier()
    result = deterministic_baseline(source)
    if mutation == "stale_source":
        source["caregiver_concerns"] = "A different saved source"
    elif mutation == "missing_quote":
        result["artifact"]["source_statements"].pop()
        result["artifact_hash"] = artifact_digest(result["artifact"])
    elif mutation == "forged_hash":
        result["artifact_hash"] = "f" * 64
    elif mutation == "different_execution":
        result["session_id"] = "other-generation"
    elif mutation == "edited_text":
        result["explanation_text"] += "\nExtra advice"
    elif mutation == "omitted_regimen_key":
        result.pop("patient_regimen")
    else:
        result["receipts"] = [None]
    with pytest.raises(ValueError):
        validate_reconciliation_result(source, result)


def test_rehashed_baseline_cannot_claim_observed_sdk_execution():
    source = dossier()
    result = deterministic_baseline(source)
    result["model_mode"] = "fixture"
    with pytest.raises(ValueError):
        validate_reconciliation_result(source, result)


@pytest.mark.parametrize("case", ["foreign", "duplicate", "wrong_precondition", "advice"])
def test_model_selection_has_no_free_clinical_fields_or_invalid_citations(case):
    source = dossier()
    selected = baseline_selection(source)
    if case == "foreign":
        selected["questions"] = [{"code": "caregiver_question", "statement_ids": ["another:L1"]}]
    elif case == "duplicate":
        selected["teachback"][0]["statement_ids"] = ["home:L1", "home:L1"]
    elif case == "wrong_precondition":
        selected["questions"] = [{"code": "confirm_taper", "statement_ids": ["home:L1"]}]
    else:
        selected["clinical_advice"] = "Take the newer dose"
    with pytest.raises(ValueError):
        validate_selection(source, selected)


def test_greek_questions_do_not_translate_or_change_clinical_quotes():
    source = examples()[2]["dossier"]
    result = deterministic_baseline(source)
    assert result["artifact"]["language"] == "el"
    assert result["artifact"]["source_statements"][0]["citation"]["quote"] == source["documents"][0]["text"]
    assert "Μπορεί" in result["artifact"]["proposed_questions"][0]["text"]


@pytest.mark.parametrize("change", ["duplicate_id", "too_many_docs", "bad_time", "too_long", "control", "unknown_key"])
def test_bounds_and_contract_fail_before_model_use(change):
    source = dossier()
    if change == "duplicate_id":
        source["documents"][1]["document_id"] = "home"
    elif change == "too_many_docs":
        source["documents"] *= 3
    elif change == "bad_time":
        source["documents"][0]["issued_at"] = "yesterday"
    elif change == "too_long":
        source["documents"][0]["text"] = "x" * 6001
    elif change == "control":
        source["documents"][0]["text"] += "\x00"
    else:
        source["prescriber_verified"] = True
    with pytest.raises(ValueError):
        validate_dossier(source)


def test_publisher_schema_cannot_smuggle_clinical_advice():
    with pytest.raises(ValidationError):
        PublicationSelection.model_validate({"source_hash": "0" * 64, "artifact_ref": "clarify-" + "a" * 32,
                                             "presentation": "clarification-only-v1", "dose": "10"})
