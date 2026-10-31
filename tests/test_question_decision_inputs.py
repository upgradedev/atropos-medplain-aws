"""Lossless model-input joins, without assigning semantic relevance or expected questions."""

from copy import deepcopy
import hashlib

import pytest

from medplain import reconciliation_engine as engine
from medplain.reconciliation import source_statements, structural_issues


def _source(language="en", prefix="case-z"):
    return {"profile": "medplain-reconciliation-v1", "patient_alias": "Demo-Decision-Inputs",
            "language": language, "caregiver_concerns": "", "documents": [
                {"document_id": prefix + "-instructions", "kind": "discharge",
                 "issued_at": "2026-10-30T06:00:00Z", "effective_at": None,
                 "text": "Taper Prednisone: 15 mg by mouth QD on days 2-4; then 5 mg by mouth QD on days 5-7.\n"
                         "Pending test: culture Z; owner: service Z; follow-up: next clinic visit.\n"
                         "Pending test: sample Q; owner: unknown; follow-up: not assigned."},
                {"document_id": prefix + "-context", "kind": "caregiver_note",
                 "issued_at": "2026-10-30T06:15:00Z", "effective_at": None,
                 "text": "We do not have a question about the written instructions."}]}


def _evidence(source):
    context = engine.Context(source, "decision-input-provenance", False)
    context.index_verified = True
    return next(item for item in engine._tools(context)[engine.PLANNER]
                if item.tool_name == "check_reconciliation_constraints")()


@pytest.mark.parametrize("language", ["en", "el"])
@pytest.mark.parametrize("prefix", ["case-z", "renumbered-omega"])
def test_decision_rows_join_all_catalog_codes_preconditions_and_exact_original_occurrences(language, prefix):
    source = _source(language, prefix)
    saved = deepcopy(source)
    evidence = _evidence(source)
    rows = evidence["question_decision_inputs"]
    assert [row["code"] for row in rows] == list(evidence["question_catalog"])
    assert len(rows) == len(set(row["code"] for row in rows)) == 9
    original = {row["statement_id"]: row for row in source_statements(source)}
    flattened = []
    for row in rows:
        catalog = evidence["question_catalog"][row["code"]]
        assert row["meaning_en"] == catalog["meaning_en"]
        assert row["rendered_text"] == catalog["rendered_text"]
        assert row["eligibility_preconditions"] == evidence["code_eligibility"]["codes"][row["code"]]
        assert row["output_section"] == ("teachback" if row["code"] == "teachback" else "questions")
        expected = [item for item in evidence["already_rendered_checks"] if item["question_code"] == row["code"]]
        actual = row["displayed_structural_prompts"]
        assert [{key: value for key, value in item.items() if key != "source_statements"} for item in actual] == [
            {key: value for key, value in item.items() if key != "question_meaning_en"} for item in expected]
        for occurrence in actual:
            assert occurrence["source_statements"] == [original[sid] for sid in occurrence["statement_ids"]]
            flattened.append(occurrence["issue_id"])
    assert sorted(flattened) == sorted(item["issue_id"] for item in evidence["already_rendered_checks"])
    assert source == saved


def test_false_and_true_flags_and_multiple_occurrences_remain_displayed_not_resolved():
    evidence = _evidence(_source())
    rows = {row["code"]: row for row in evidence["question_decision_inputs"]}
    taper = rows["confirm_taper"]["displayed_structural_prompts"]
    assert len(taper) == 1 and taper[0]["requires_clarification"] is False
    pending = rows["confirm_pending_owner"]["displayed_structural_prompts"]
    assert [item["statement_ids"] for item in pending] == [["case-z-instructions:L2"], ["case-z-instructions:L3"]]
    assert [item["requires_clarification"] for item in pending] == [False, True]
    assert [item["code"] for item in pending] == ["pending_result_follow_up", "pending_follow_up_gap"]
    assert all(item["classification"] == "structural_check" for item in taper + pending)
    assert all(not ({"covered", "resolved", "disposition", "recommended_question"} & set(item))
               for item in rows.values())


@pytest.mark.parametrize("context", ["We have a question about the written instructions.",
                                    "We do not have a question about the written instructions."])
def test_context_negation_is_preserved_but_does_not_select_or_expand_a_structural_occurrence(context):
    source = _source()
    original = _evidence(source)
    source["documents"][1]["text"] = context
    evidence = _evidence(source)
    assert evidence["caregiver_context"][0]["citation"]["quote"] == context
    assert evidence["question_decision_inputs"] == original["question_decision_inputs"]
    assert all("case-z-context:L1" not in item["statement_ids"]
               for row in evidence["question_decision_inputs"] for item in row["displayed_structural_prompts"])
    assert "recommended_plan" not in evidence and "expected_plan" not in evidence


@pytest.mark.parametrize("language", ["en", "el"])
def test_no_structural_occurrences_keeps_every_catalog_choice_and_empty_witness_preconditions(language):
    source = _source(language)
    source["documents"][0]["text"] = "Take Medicine-Z 2.5 mg by mouth BID."
    source["documents"][1]["text"] = "I understand the written plan."
    evidence = _evidence(source)
    assert evidence["already_rendered_checks"] == []
    assert evidence["covered_question_scopes"] == []
    rows = {row["code"]: row for row in evidence["question_decision_inputs"]}
    assert len(rows) == 9
    assert all(row["displayed_structural_prompts"] == [] for row in rows.values())
    assert rows["confirm_taper"]["eligibility_preconditions"] == {"requires_any_witness": []}
    assert rows["teachback"]["output_section"] == "teachback"
    assert rows["caregiver_question"]["output_section"] == "questions"


def test_matrix_cannot_alias_old_views_original_dossier_or_a_second_tool_result():
    source = _source()
    saved = deepcopy(source)
    evidence = _evidence(source)
    old_views = {key: deepcopy(value) for key, value in evidence.items() if not key.startswith("question_decision_inputs")}
    matrix = {row["code"]: row for row in evidence["question_decision_inputs"]}
    matrix["confirm_taper"]["meaning_en"] = "consumer mutation"
    matrix["confirm_taper"]["eligibility_preconditions"]["requires_any_witness"].append("absent:L9")
    occurrence = matrix["confirm_taper"]["displayed_structural_prompts"][0]
    occurrence["statement_ids"].append("case-z-context:L1")
    occurrence["source_statements"][0]["citation"]["quote"] = "consumer mutation"
    occurrence["source_statements"][0]["clinical"]["taper_phases"][0]["dose"] = "999"
    assert {key: value for key, value in evidence.items() if not key.startswith("question_decision_inputs")} == old_views
    assert source == saved
    assert _evidence(source)["question_decision_inputs"] != evidence["question_decision_inputs"]


def test_multibyte_quotes_offsets_hashes_and_label_reference_fields_survive_the_join():
    source = _source("el")
    prefix = "Σημείωση.\n"
    source["documents"][0]["text"] = prefix + "Take Warfarin 2.5 mg by mouth QHS.\nTake Ibuprofen 200 mg by mouth TID."
    evidence = _evidence(source)
    row = next(item for item in evidence["question_decision_inputs"] if item["code"] == "confirm_interaction_review")
    prompts = row["displayed_structural_prompts"]
    assert prompts and prompts[0]["classification"] == "selected_label_reference"
    original = next(item for item in evidence["already_rendered_checks"]
                    if item["code"] == "selected_label_interaction_review")
    assert prompts[0]["reference_ids"] == original["reference_ids"]
    assert prompts[0]["selected_pair"] == original["selected_pair"]
    raw = source["documents"][0]["text"].encode("utf-8")
    citations = [item["citation"] for item in prompts[0]["source_statements"]]
    assert citations[0]["byte_start"] == len(prefix.encode("utf-8"))
    for citation in citations:
        assert raw[citation["byte_start"]:citation["byte_end"]] == citation["quote"].encode("utf-8")
        assert citation["document_sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("corruption", ["unknown_code", "unknown_citation", "missing_eligibility"])
def test_invalid_join_inputs_fail_instead_of_substituting_a_code_or_statement(corruption):
    source = _source()
    evidence = _evidence(source)
    statements = source_statements(source)
    floor = structural_issues(statements)
    eligibility = deepcopy(evidence["code_eligibility"]["codes"])
    if corruption == "unknown_code":
        floor[0]["question_code"] = "invented_question"
    elif corruption == "unknown_citation":
        floor[0]["statement_ids"] = ["not-current:L42"]
    else:
        del eligibility["confirm_taper"]
    with pytest.raises(KeyError):
        engine._question_decision_inputs(statements, floor, evidence["question_catalog"], eligibility)
