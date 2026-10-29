"""Independent gold facts, real official Evals execution, and failure-preserving reports."""

import copy
import importlib.util
import json

import pytest

from medplain.evaluation import (
    CANONICAL_URL, EVALS_VERSION, EXPLAINER, PARSER, VERIFIER, RemoteRunner,
    _blank, aggregate, corpus, critical_facts, expected_extraction, expected_facts,
    gate_challenges, local_outcome, publication_valid, run_evaluation, score_outcome,
    trajectory_valid, write_report,
)
from medplain.gate import canonical_hash
from medplain.intake import IntakeError, normalize_source


def test_literal_heldout_gold_is_not_normalizer_output_and_covers_critical_units_and_actions():
    cases = corpus()
    assert len(cases) == 10 and sum(c["gold"] is not None for c in cases) == 6
    for case in cases:
        if case["gold"] is None:
            with pytest.raises(IntakeError):
                normalize_source(case["source_text"])
        else:
            # Gold facts are authored separately from raw strings. This catches
            # either a wrong annotation or a normalizer regression per case.
            assert normalize_source(case["source_text"]).expected_extraction.model_dump() == expected_extraction(case)
    medications = [m for c in cases if c["gold"] for m in c["gold"]["medications"]]
    assert {m["unit"] for m in medications} == {"mg", "mcg", "mL"}
    assert {m["frequency"] for m in medications} == {"QD", "BID", "TID", "QHS"}
    assert any(m["status"] == "stopped" for m in medications)
    assert any(m["dose"] == "0.125" for m in medications)
    assert "Do not take" in cases[1]["source_text"]
    assert "note-" not in cases[0]["source_text"]  # No shipped packet selector as input.


def test_competent_baseline_preserves_all_expected_facts_and_correctly_refuses_unknowns():
    for case in corpus():
        outcome = local_outcome(case, "baseline")
        scores = score_outcome(case, outcome, "baseline")
        assert scores["validity"], outcome
        if case["gold"]:
            assert scores["extraction_exact"] and scores["field_fidelity"]
            assert outcome["independent_publication_check"]
            assert outcome["nodes"] == outcome["tools"] == []
        else:
            assert outcome["kind"] == "input_refusal" and outcome["explanation"] is None


def test_gold_checker_rejects_coherent_value_change_and_cannot_inherit_changed_expected_facts():
    case = corpus()[0]
    outcome = local_outcome(case, "baseline")
    outcome["extraction"]["medications"][0]["dose"] = "125"
    outcome["explanation"]["medications"][0]["dose"] = "125"
    scores = score_outcome(case, outcome, "baseline")
    assert not scores["extraction_exact"] and not scores["field_fidelity"]
    assert expected_facts(case)["medications"][0]["dose"] == "12.5"
    assert not publication_valid(normalize_source(case["source_text"]).packet, outcome)


def test_baseline_cannot_borrow_gold_source_lines_or_quotes():
    case = corpus()[0]
    case["gold"]["medications"][0]["source_line"] = 4
    outcome = local_outcome(case, "baseline")
    assert outcome["extraction"]["medications"][0]["source_line"] == 3
    assert outcome["extraction"]["medications"][0]["source_quote"].startswith("Take Metoprolol 12.5 mg")
    scores = score_outcome(case, outcome, "baseline")
    assert not scores["extraction_exact"] and scores["field_fidelity"]


def test_shared_gate_rejects_all_eleven_coherent_mutations_and_exposes_each_field():
    results = []
    for case in corpus()[:2]:
        results += gate_challenges(normalize_source(case["source_text"]).packet, case)
    assert len(results) == 11 and not any(r["false_accept"] for r in results)
    assert {r["id"] for r in results} >= {"decimal-dose", "mg-to-mcg", "mcg-to-mg", "ml-to-mg",
                                         "bid-to-tid", "tid-to-bid", "qhs-to-qd", "stop-negation"}
    assert all(r["discrepancies"] and len(r["coherent_artifact_hash"]) == 64 for r in results)
    stopped = next(r for r in results if r["id"] == "stop-negation")
    assert any("status" in message for message in stopped["discrepancies"])


def test_corrective_graph_trajectory_is_actual_and_fresh_tools_cannot_be_removed():
    case = corpus()[0]
    outcome = local_outcome(case, "strands", stress=True)
    assert outcome["status"] == "verified" and outcome["correction_count"] == 1
    assert outcome["nodes"] == [PARSER, VERIFIER, EXPLAINER, VERIFIER, EXPLAINER]
    assert [r["verdict"] for r in outcome["receipts"]] == ["refused", "verified"]
    assert trajectory_valid(outcome)
    changed = copy.deepcopy(outcome)
    changed["tools"] = [t for t in changed["tools"] if t["tool"] != "drug_interaction_checker"]
    assert not trajectory_valid(changed)
    changed = copy.deepcopy(outcome)
    changed["nodes"][3] = PARSER
    assert not trajectory_valid(changed)
    # Aggregate counts would be fooled by duplicated first-verifier calls.
    # Every later invocation must independently contain its own fresh tools.
    changed = copy.deepcopy(outcome)
    changed["tools"] = [t for t in changed["tools"] if t["invocation_index"] != 4]
    changed["tools"] += copy.deepcopy([t for t in changed["tools"] if t["invocation_index"] == 2])
    assert not trajectory_valid(changed)
    changed = copy.deepcopy(outcome)
    changed["tools"][0]["invocation_index"] = True
    assert not trajectory_valid(changed)
    # The deterministic baseline is allowed to rebuild safely too.
    baseline = local_outcome(case, "baseline", stress=True)
    assert baseline["status"] == "verified" and baseline["correction_count"] == 1
    assert baseline["artifact_hash"] == outcome["artifact_hash"]


def test_runtime_failure_is_not_an_input_refusal_or_dropped_from_denominators():
    case = corpus()[-1]
    failure = _blank("fixture")
    failure["error"] = "Injected failure for the evaluator negative proof"
    assert not score_outcome(case, failure, "strands")["validity"]
    row = {"expected": case, "outcomes": {"strands": failure},
           "scores": {"strands": score_outcome(case, failure, "strands")}}
    counts = aggregate([row], "strands", "fixture")
    assert counts["case_count"] == counts["runtime_error_count"] == 1
    assert counts["validity_passed"] == 0
    assert counts["inference_usage"]["tokens"] is None


def test_refused_valid_source_still_counts_as_failed_extraction_and_field_sample():
    case = corpus()[0]
    refusal = _blank("bedrock")
    refusal.update(status="refused", kind="model_refusal")
    row = {"expected": case, "outcomes": {"strands": refusal},
           "scores": {"strands": score_outcome(case, refusal, "strands")}}
    counts = aggregate([row], "strands", "bedrock")
    assert counts["extraction_eligible_count"] == counts["field_fidelity_eligible_count"] == 1
    assert counts["extraction_exact_count"] == counts["field_fidelity_passed"] == 0


def test_remote_runner_does_not_retry_and_retains_failed_response_and_job_budget(monkeypatch):
    runner = RemoteRunner.__new__(RemoteRunner)
    runner.url, runner.token, runner.jobs, runner.commit = CANONICAL_URL, "synthetic-test-token", 0, "f" * 40
    calls = []

    def refused(path, body=None):
        calls.append((path, body))
        return 422, {"detail": "unsupported source line", "negative_proof": True}

    monkeypatch.setattr(runner, "request", refused)
    result = runner.run(corpus()[-1])
    assert result["kind"] == "input_refusal" and result["failure_response"]["negative_proof"]
    assert len(calls) == 1 and runner.jobs == 0
    assert calls[0][1]["synthetic_confirmed"] is True and "stress" not in calls[0][1]

    def broken(path, body=None):
        calls.append((path, body))
        raise TimeoutError()

    monkeypatch.setattr(runner, "request", broken)
    runner.jobs = 7
    assert runner.run(corpus()[0])["kind"] == "runtime_error"
    assert runner.jobs == 8 and len(calls) == 2
    assert runner.run(corpus()[0])["kind"] == "budget_not_executed"
    assert len(calls) == 2


def test_remote_constructor_rejects_arbitrary_url_or_missing_release_without_network():
    for url, commit in [("https://example.com", "f" * 40), (CANONICAL_URL, "main")]:
        with pytest.raises(ValueError):
            RemoteRunner(url, commit)


@pytest.mark.skipif(importlib.util.find_spec("strands_evals") is None, reason="Optional official eval extra not installed")
def test_actual_official_case_experiment_custom_evaluators_and_report_hash(tmp_path):
    import importlib.metadata

    assert importlib.metadata.version("strands-agents-evals") == EVALS_VERSION
    report = run_evaluation("f" * 40)
    assert report["framework"]["case_experiment_executed"]
    assert report["framework"]["package"] == "strands-agents-evals"
    assert len(report["official_evaluation"]["cases"]) == 60  # 10 notes x 2 arms x 3 actual evaluators
    assert all(report["official_evaluation"]["test_passes"])
    assert report["measurement_status"]["outcome_validity"] == "all_expected"
    for counts in report["arms"].values():
        assert counts["case_count"] == counts["validity_passed"] == 10
        assert counts["extraction_exact_count"] == counts["field_fidelity_passed"] == 6
        assert counts["gate_false_accept_count"] == 0 and counts["gate_mutation_count"] == 11
        assert counts["inference_usage"]["tokens"] is None
        assert counts["input_refused_count"] == 4 and counts["model_refused_count"] == 0
        assert counts["not_executed_count"] == 0
    assert report["report_sha256"] == canonical_hash({k: v for k, v in report.items() if k != "report_sha256"})
    assert report["corpus"]["sha256"] == canonical_hash(corpus())
    path = tmp_path / "evaluation.json"
    write_report(report, path)
    assert json.loads(path.read_text())["source_commit"] == "f" * 40
    changed = copy.deepcopy(report)
    changed["arms"]["strands"]["validity_passed"] = 0
    with pytest.raises(ValueError):
        write_report(changed, path)


def test_fact_comparison_cannot_call_renderer_for_expected_answer(monkeypatch):
    import medplain.evaluation as module

    monkeypatch.setattr(module, "build_explanation", lambda packet: (_ for _ in ()).throw(AssertionError()))
    assert expected_facts(corpus()[0])["medications"][0]["dose"] == "12.5"
    assert expected_extraction(corpus()[0])["medications"][0]["frequency"] == "BID"
    assert critical_facts(None) is None
