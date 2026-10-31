"""Literal scope/provenance controls, not a measurement of model relevance."""

from copy import deepcopy
import hashlib

import pytest

from medplain import reconciliation_engine as engine
from medplain.reconciliation import baseline_selection, build_artifact, source_statements
from medplain.reconciliation_evaluation import corpus


def _source():
    return {"profile": "medplain-reconciliation-v1", "patient_alias": "Demo-Coverage", "language": "en",
            "caregiver_concerns": "", "documents": [
                {"document_id": "orders", "kind": "discharge", "issued_at": "2026-10-30T07:00:00Z",
                 "effective_at": None,
                 "text": "Hold Clonidine if pulse < 52 bpm OR dizziness = present; restart only after the designated clinician confirms."},
                {"document_id": "support", "kind": "caregiver_note", "issued_at": "2026-10-30T08:00:00Z",
                 "effective_at": None, "text": "I have a question about the new paperwork."}]}


def _evidence(source):
    context = engine.Context(source, "literal-coverage-only", False)
    context.index_verified = True
    return next(item for item in engine._tools(context)[engine.PLANNER]
                if item.tool_name == "check_reconciliation_constraints")()


def test_single_source_rendered_question_does_not_cover_uncited_caregiver_context():
    source = _source()
    saved = deepcopy(source)
    evidence = _evidence(source)
    assert source == saved
    assert len(evidence["covered_question_scopes"]) == 1
    covered = evidence["covered_question_scopes"][0]
    assert covered["question_code"] == "confirm_hold_restart"
    assert covered["statement_ids"] == ["orders:L1"]
    assert [row["statement_id"] for row in covered["source_statements"]] == ["orders:L1"]
    assert covered["source_statements"][0]["citation"]["quote"] == saved["documents"][0]["text"]
    assert [row["statement_id"] for row in evidence["caregiver_context"]] == ["support:L1"]
    assert "support:L1" not in covered["statement_ids"]
    assert set(covered) == {"issue_id", "question_code", "question_text", "question_meaning_en",
                            "statement_ids", "source_statements"}


def test_changed_and_added_caregiver_narrative_cannot_expand_rendered_scope():
    source = _source()
    first = _evidence(source)
    changed = deepcopy(source)
    changed["documents"][1]["text"] = "I interpreted the condition differently and have another question."
    changed["documents"].append({"document_id": "another-support", "kind": "caregiver_note",
                                 "issued_at": "2026-10-30T09:00:00Z", "effective_at": None,
                                 "text": "The clinician's statement concerns my interpretation too."})
    changed["caregiver_concerns"] = "A separately entered concern about the instructions."
    second = _evidence(changed)
    assert second["source_hash"] != first["source_hash"]
    assert second["covered_question_scopes"] == first["covered_question_scopes"]
    assert [row["statement_id"] for row in second["caregiver_context"]] == [
        "support:L1", "another-support:L1", "caregiver-concerns:L1"]
    assert second["source_documents"] == changed["documents"]


def test_coverage_view_cannot_alias_original_statements_or_floor_rows():
    source = _source()
    saved = deepcopy(source)
    evidence = _evidence(source)
    original_floor = deepcopy(evidence["already_rendered_checks"])
    original_statements = deepcopy(evidence["clinical_source_statements"])
    view = evidence["covered_question_scopes"][0]
    view["statement_ids"].append("support:L1")
    view["source_statements"][0]["citation"]["quote"] = "Consumer changed the returned view."
    view["source_statements"][0]["clinical"]["logic_tokens"].append("consumer mutation")
    assert source == saved
    assert evidence["already_rendered_checks"] == original_floor
    assert evidence["clinical_source_statements"] == original_statements
    assert _evidence(source)["covered_question_scopes"][0]["statement_ids"] == ["orders:L1"]


def test_multi_source_coverage_keeps_exact_quotes_utf8_offsets_and_document_hashes():
    source = _source()
    prefix = "Σημείωση για τον φροντιστή.\n"
    first_quote = "Take Brand-C 7.5 mg by mouth QD."
    second_quote = "Take Brand-C 2.5 mg by mouth BID."
    source["documents"] = [
        {"document_id": "home", "kind": "home_list", "issued_at": "2026-10-29T07:00:00Z",
         "effective_at": None, "text": prefix + first_quote},
        {"document_id": "orders", "kind": "discharge", "issued_at": "2026-10-30T07:00:00Z",
         "effective_at": None, "text": second_quote + "\n"}]
    evidence = _evidence(source)
    covered = next(row for row in evidence["covered_question_scopes"] if row["question_code"] == "compare_instructions")
    assert covered["statement_ids"] == ["home:L2", "orders:L1"]
    assert [row["statement_id"] for row in covered["source_statements"]] == covered["statement_ids"]
    for index, (quote, start) in enumerate([(first_quote, len(prefix.encode("utf-8"))), (second_quote, 0)]):
        citation = covered["source_statements"][index]["citation"]
        document = source["documents"][index]
        raw = document["text"].encode("utf-8")
        assert citation["quote"] == quote
        assert citation["byte_start"] == start
        assert citation["byte_end"] == start + len(quote.encode("utf-8"))
        assert raw[citation["byte_start"]:citation["byte_end"]] == quote.encode("utf-8")
        assert citation["document_sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("case", corpus(), ids=lambda case: case["id"])
def test_all_frozen_corpus_scope_views_preserve_original_floor_catalog_and_source(case):
    source = deepcopy(case["dossier"])
    saved = deepcopy(source)
    evidence = _evidence(source)
    artifact = build_artifact(source, baseline_selection(source))
    floor = artifact["structural_issues"]
    assert [{key: value for key, value in row.items() if key != "question_meaning_en"}
            for row in evidence["already_rendered_checks"]] == floor
    views = evidence["covered_question_scopes"]
    assert len(views) == len(floor)
    original = {row["statement_id"]: row for row in artifact["source_statements"]}
    for row, view in zip(floor, views, strict=True):
        assert view["issue_id"] == row["issue_id"]
        assert view["question_code"] == row["question_code"]
        assert view["question_text"] == row["question_text"]
        assert view["statement_ids"] == row["statement_ids"]
        assert view["source_statements"] == [original[sid] for sid in row["statement_ids"]]
        assert view["question_meaning_en"] == evidence["question_catalog"][row["question_code"]]["meaning_en"]
    assert evidence["source_documents"] == source["documents"]
    partitioned = evidence["clinical_source_statements"] + evidence["caregiver_context"]
    by_id = {row["statement_id"]: row for row in partitioned}
    assert [by_id[sid] for sid in evidence["source_statement_order"]] == source_statements(source)
    assert source == saved


def test_unknown_scope_citation_refuses_instead_of_substituting_another_statement():
    statements = source_statements(_source())
    corrupt_floor = [{"issue_id": "invalid", "question_code": "confirm_hold_restart",
                      "statement_ids": ["absent:L1"]}]
    with pytest.raises(KeyError):
        engine._covered_question_scopes(statements, corrupt_floor, "en")
