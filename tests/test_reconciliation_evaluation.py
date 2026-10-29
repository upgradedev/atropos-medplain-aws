"""Independent corpus, actual offline SDK experiment and destructive ruler proofs."""

import copy
import json
from collections import Counter

import pytest

from medplain import reconciliation_evaluation as evaluation


@pytest.fixture(scope="module")
def paired(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MEDPLAIN_MODEL_MODE", "fixture")
        patch.setenv("MEDPLAIN_COMMIT", "a" * 40)
        patch.setenv("GITHUB_SHA", "a" * 40)
        patch.setenv("AWS_EC2_METADATA_DISABLED", "true")
        patch.setenv("OTEL_SDK_DISABLED", "true")
        yield evaluation.run_evaluation("a" * 40,
                                        checkpoint=tmp_path_factory.mktemp("reconciliation") / "partial.json")


def test_complete_independent_corpus_and_four_pair_scope():
    cases = evaluation.corpus()
    assert len(cases) == len({row["id"] for row in cases}) == 30
    assert Counter(row["family"] for row in cases) == Counter({family: 5 for family in evaluation.FAMILIES})
    assert {tuple(pair["pair"]) for row in cases for pair in row["gold"]["selected_interactions"]} == set(
        evaluation.SELECTED_PAIR_UNIVERSE)
    assert sum(row["gold"]["interaction_control"] == "all_stopped" for row in cases) == 1
    assert any(not row["gold"]["questions"] for row in cases)
    assert any(not row["gold"]["structural_issues"] for row in cases)
    for index, row in enumerate(cases, start=1):
        assert "gold" not in row["dossier"]
        # Descriptive case IDs/families stay in evaluator metadata. An alias
        # such as Demo-pending-owner-gap would leak the expected problem.
        opaque_suffix = chr(65 + (index - 1) // 26) + chr(65 + (index - 1) % 26)
        assert row["dossier"]["patient_alias"] == "Demo-RC" + opaque_suffix
        assert 2 <= len(row["dossier"]["documents"]) <= 4
        assert set(row["gold"]["clinical_by_statement"]) == set(evaluation.source_records(row["dossier"]))


def test_all_thirty_real_fixture_graphs_and_full_baselines_complete(paired):
    assert paired["framework"]["case_experiment_executed"] is True
    assert paired["framework"]["package"] == "strands-agents-evals"
    assert paired["corpus"]["count"] == len(paired["cases"]) == 30
    assert paired["report_sha256"] == evaluation.digest({k: v for k, v in paired.items() if k != "report_sha256"})
    for arm in ["strands", "baseline"]:
        counters = paired["arms"][arm]
        for field in ["verified_count", "valid_outcome_count", "source_coverage_count", "source_citations_count",
                      "supported_atoms_exact_count", "proper_withholding_count"]:
            assert counters[field] == 30, (arm, field, [row["id"] for row in paired["cases"]
                                                     if not row["scores"][arm]["supported_atoms_exact"]])
        assert counters["runtime_error_count"] == counters["not_executed_count"] == 0
        assert counters["structural_focus"]["false_negative"] == 0
        assert counters["selected_interaction_review"]["true_positive"] == 4
        assert counters["selected_interaction_review"]["false_positive"] == 0
        assert counters["selected_interaction_review"]["reference_bound_positive_cases"] == 4
        assert counters["inference_usage"]["measured"] is False
    assert paired["arms"]["strands"]["fresh_trajectory_count"] == 30
    # No forced semantic-perfect assertion: fixtures deliberately replay the
    # complete baseline. Oblique context and false positives remain measured.
    for row in paired["cases"]:
        assert row["outcomes"]["strands"]["result"]["artifact"]["source_statements"] == (
            row["outcomes"]["baseline"]["result"]["artifact"]["source_statements"])
        assert row["outcomes"]["strands"]["result"]["artifact"]["structural_issues"] == (
            row["outcomes"]["baseline"]["result"]["artifact"]["structural_issues"])


def test_focus_requires_correct_context_and_does_not_reward_duplicates():
    target = {"codes": ["confirm_identity_form"],
              "required_statement_ids": ["caregiver:L1", "discharge:L2"],
              "allowed_statement_ids": ["caregiver:L1", "discharge:L2", "home:L1"]}
    correct = {"code": "confirm_identity_form", "statement_ids": ["caregiver:L1", "discharge:L2"]}
    wrong = {"code": "confirm_identity_form", "statement_ids": ["caregiver:L1", "discharge:L1"]}
    assert evaluation.match_focus([wrong], [target]) == {
        "true_positive": 0, "false_positive": 1, "false_negative": 1,
        "predicted": 1, "expected": 1, "exact": False}
    duplicate = evaluation.match_focus([correct, correct], [target])
    assert duplicate["true_positive"] == 1 and duplicate["false_positive"] == 1 and not duplicate["exact"]
    missing = evaluation.match_focus([{"code": "confirm_identity_form", "statement_ids": ["caregiver:L1"]}], [target])
    assert missing["false_negative"] == 1
    extra = evaluation.match_focus([{**correct, "statement_ids": [*correct["statement_ids"], "unrelated:L1"]}], [target])
    assert extra["false_positive"] == 1


def test_focus_maximum_matching_is_not_greedy():
    targets = [
        {"codes": ["confirm_taper"], "required_statement_ids": ["a"], "allowed_statement_ids": ["a", "b"]},
        {"codes": ["confirm_taper"], "required_statement_ids": ["b"], "allowed_statement_ids": ["a", "b"]},
    ]
    predictions = [{"code": "confirm_taper", "statement_ids": ["a", "b"]},
                   {"code": "confirm_taper", "statement_ids": ["a"]}]
    assert evaluation.match_focus(predictions, targets)["true_positive"] == 2


def test_generic_padding_wrong_citations_and_unexecuted_negative_do_not_pass(paired):
    row = next(row for row in paired["cases"] if row["id"] == "brand-generic-context")
    changed = copy.deepcopy(row["outcomes"]["strands"])
    changed["result"]["artifact"]["proposed_questions"] = [
        {"code": "caregiver_question", "statement_ids": ["caregiver-concerns:L1"]} for _ in range(12)]
    score = evaluation.score_outcome(row["expected"], changed)
    assert score["question_focus"]["true_positive"] == 0 and score["question_focus"]["false_negative"] == 1
    altered = copy.deepcopy(row["outcomes"]["strands"]["result"]["artifact"])
    altered["source_statements"][0]["citation"]["quote"] = altered["source_statements"][1]["citation"]["quote"]
    assert not evaluation.source_scores(row["expected"], altered)["source_citations"]
    altered = copy.deepcopy(row["outcomes"]["strands"]["result"]["artifact"])
    altered["source_statements"][0]["citation"]["line"] = True
    assert not evaluation.source_scores(row["expected"], altered)["source_citations"]
    negative = next(row for row in paired["cases"] if not row["expected"]["gold"]["questions"])
    unexecuted = {**evaluation.blank_outcome("bedrock"), "status": "not_executed"}
    score = evaluation.score_outcome(negative["expected"], unexecuted)
    assert not score["question_focus"]["exact"] and not score["proper_withholding"]


def test_boolean_integer_equivalence_never_passes_exact_source_ruler(paired):
    row = next(row for row in paired["cases"] if row["id"] == "taper-regular")
    artifact = copy.deepcopy(row["outcomes"]["strands"]["result"]["artifact"])
    citation = artifact["source_statements"][0]["citation"]
    assert citation["byte_start"] == 0
    citation["byte_start"] = False
    assert not evaluation.source_scores(row["expected"], artifact)["source_citations"]
    artifact = copy.deepcopy(row["outcomes"]["strands"]["result"]["artifact"])
    phase = artifact["source_statements"][0]["clinical"]["taper_phases"][0]
    assert phase["sequence"] == 1
    phase["sequence"] = True
    assert not evaluation.source_scores(row["expected"], artifact)["supported_atoms_exact"]


@pytest.mark.parametrize("case_id,field", [
    ("dose-different", "dose"), ("dose-different", "unit"), ("dose-different", "frequency"),
    ("stop-new", "action"), ("taper-regular", "start_day"), ("hold-and", "logic_tokens"),
    ("pending-owner-gap", "owner"), ("dose-different", "quote"), ("dose-different", "omission"),
    ("dose-different", "floor"), ("dose-different", "regimen"),
])
def test_coherently_rehashed_mutations_still_fail_independent_validator(paired, case_id, field):
    from medplain.reconciliation import (
        artifact_digest, render_artifact, validate_reconciliation_result,
    )

    row = next(row for row in paired["cases"] if row["id"] == case_id)
    result = copy.deepcopy(row["outcomes"]["strands"]["result"])
    artifact = result["artifact"]
    statement = next(s for s in artifact["source_statements"] if s["document_kind"] != "caregiver_note")
    clinical = statement["clinical"]
    if field in {"dose", "unit", "frequency", "action"}:
        if field == "action":
            statement = next(s for s in artifact["source_statements"]
                             if any(m["action"] == "stop" for m in s["clinical"]["medications"]))
            statement["clinical"]["medications"][0]["action"] = "take"
        else:
            clinical["medications"][0][field] = {"dose": "999", "unit": "mcg", "frequency": "TID"}[field]
    elif field == "start_day":
        clinical["taper_phases"][0]["start_day"] = "9"
    elif field == "logic_tokens":
        clinical["logic_tokens"] = [">", "OR", "="]
    elif field == "owner":
        clinical["owner"] = "invented prescriber"
    elif field == "quote":
        statement["citation"]["quote"] += " invented instruction"
    elif field == "omission":
        artifact["source_statements"].pop(0)
    elif field == "floor":
        artifact["structural_issues"] = []
    else:
        artifact["patient_regimen"] = [{"dose": "999", "unit": "mg"}]
    result["artifact_hash"] = artifact_digest(artifact)
    result["explanation_text"] = render_artifact(artifact)
    previous = "0" * 64
    for receipt in result["receipts"]:
        receipt["previous_hash"] = previous
        receipt["artifact_hash"] = result["artifact_hash"]
        receipt["hash"] = evaluation.digest({k: v for k, v in receipt.items() if k != "hash"})
        previous = receipt["hash"]
    with pytest.raises(ValueError):
        validate_reconciliation_result(row["expected"]["dossier"], result, result["session_id"])


def test_fresh_tool_and_receipt_trajectory_negative_proofs(paired):
    original = paired["cases"][0]["outcomes"]["strands"]["result"]
    result = copy.deepcopy(original)
    result["runtime"]["tools_executed"] = [row for row in result["runtime"]["tools_executed"]
                                             if row["tool"] != "lookup_reconciliation_references"]
    assert not evaluation.trajectory_valid(result)
    result = copy.deepcopy(original)
    result["runtime"]["tools_executed"][0]["invocation_index"] = 2
    assert not evaluation.trajectory_valid(result)
    result = copy.deepcopy(original)
    result["receipts"][-1]["verdict"] = "refused"
    assert not evaluation.trajectory_valid(result)


def test_bounded_failed_invocation_requires_fresh_completed_replacement(paired):
    from medplain.reconciliation import validate_reconciliation_result

    row = paired["cases"][0]
    result = copy.deepcopy(row["outcomes"]["strands"]["result"])
    runtime = result["runtime"]
    runtime["nodes_executed"].insert(0, evaluation.NODES[0])
    runtime["invocation_outcomes"].insert(0, "repair")
    for tool in runtime["tools_executed"]:
        tool["invocation_index"] += 1
    # This failed invocation intentionally has no successful tool. The
    # independently validated replacement has every fresh required tool.
    validate_reconciliation_result(row["expected"]["dossier"], result, result["session_id"])
    assert evaluation.trajectory_valid(result)
    runtime["invocation_outcomes"][0] = "completed"
    assert not evaluation.trajectory_valid(result)
    runtime["invocation_outcomes"][0] = "repair"
    runtime["tools_executed"] = [tool for tool in runtime["tools_executed"] if tool["invocation_index"] != 2]
    assert not evaluation.trajectory_valid(result)


def test_gold_and_family_never_cross_model_boundary(monkeypatch):
    import medplain.reconciliation as core

    seen = []

    def capture(dossier, session_id, mode):
        seen.append(copy.deepcopy(dossier))
        raise RuntimeError("End the boundary probe before any model execution")

    monkeypatch.setattr(core, "run_reconciliation", capture)
    case = evaluation.corpus()[0]
    outcome = evaluation.local_outcome(case, "strands")
    assert outcome["status"] == "error"
    assert seen == [case["dossier"]]
    assert set(seen[0]) == {"profile", "patient_alias", "language", "caregiver_concerns", "documents"}
    assert "gold" not in seen[0] and "family" not in seen[0]


def test_frozen_corpus_is_checked_before_any_remote_request(monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)

    def must_not_call(*args, **kwargs):
        raise AssertionError("No remote request may precede the corpus freeze check")

    monkeypatch.setattr(evaluation.RemoteRunner, "request", must_not_call)
    with pytest.raises(ValueError, match="corpus changed"):
        evaluation.run_evaluation("a" * 40, mode="bedrock", judge_url=evaluation.CANONICAL_URL,
                                  expected_corpus_sha256="0" * 64)


def fake_http(monkeypatch, commit="a" * 40, wrong_worker=False, wrong_boot=False, interrupted=False):
    """Adapter-only simulation; actual SDK behavior is tested by the full experiment above."""
    from medplain.reconciliation import (
        append_receipt, artifact_digest, baseline_selection, build_artifact,
        deterministic_baseline, render_artifact,
    )

    state = {"sessions": 0, "commands": [], "cases": {}}

    def request(self, path, payload=None):
        if path == "/api/health":
            return 200, {"commit": commit, "model_mode": "bedrock"}
        if path == "/api/ops/session":
            assert payload == {"prepared_demo": False}
            state["sessions"] += 1
            return 201, {"session_token": f"SECRET-TOKEN-{state['sessions']}", "role": "operator", "demo": True}
        if path == "/api/ops/bootstrap":
            return 200, {"runtime": {"commit": "b" * 40 if wrong_boot else commit},
                         "limits": {"max_cases": 20}, "collection": {
                             "initialized": True, "prepared_count": 0, "live_count": 0}}
        if path == "/api/ops/cases/reconcile":
            assert set(payload["dossier"]) == {"profile", "patient_alias", "language", "caregiver_concerns", "documents"}
            state["commands"].append(copy.deepcopy(payload))
            if interrupted:
                raise TimeoutError("Accepted command response was indeterminate")
            dossier = payload["dossier"]
            case_id = f"case-{len(state['commands'])}"
            selection = baseline_selection(dossier)
            artifact = build_artifact(dossier, selection)
            result = deterministic_baseline(dossier)
            result.update(artifact=artifact, artifact_hash=artifact_digest(artifact),
                          explanation_text=render_artifact(artifact), model_mode="bedrock", session_id=case_id + ":1",
                          commit="b" * 40 if wrong_worker else commit, receipts=[])
            # Explicit adapter simulation, not evidence of a model call. The
            # separate complete paired experiment above exercises the SDK.
            result["runtime"] = {
                "orchestrator": "strands.multiagent.Graph", "graph_status": "completed",
                "source_index_verified": True, "selection": selection,
                "nodes_executed": list(evaluation.NODES), "invocation_outcomes": ["completed"] * 3,
                "tools_executed": [
                    {"agent": node, "tool": name, "success": True, "invocation_index": index}
                    for index, node in enumerate(evaluation.NODES, start=1)
                    for name in evaluation.REQUIRED_TOOLS[node]
                ],
            }
            append_receipt(result["receipts"], dossier, result["session_id"], artifact, [])
            state["cases"][case_id] = {"id": case_id, "generation": 1, "source": dossier,
                                       "source_profile": evaluation.PROFILE, "source_hash": evaluation.digest(dossier),
                                       "status": "review_required", "patient_url": None, "review": None,
                                       "duration_ms": 1, "result": result}
            return 202, {"case": {"id": case_id}, "created": True, "job_id": case_id + ":1"}
        return 200, state["cases"][path.rsplit("/", 1)[1]]

    monkeypatch.setattr(evaluation.RemoteRunner, "request", request)
    return state


def test_remote_thirty_reservations_two_empty_sessions_and_remaining_denominator(monkeypatch, tmp_path):
    state = fake_http(monkeypatch)
    cases = evaluation.corpus()
    path = tmp_path / "partial.json"
    journal = evaluation.CheckpointJournal(path, cases, "a" * 40, "bedrock")
    initial = json.loads(path.read_text())
    assert len(initial["cases"]) == 30 and initial["job_reservations"] == 0
    assert all(row["outcomes"]["strands"]["status"] == "not_executed" for row in initial["cases"])
    runner = evaluation.RemoteRunner(evaluation.CANONICAL_URL, "a" * 40, journal)
    outcomes = [runner.run(case) for case in cases]
    assert all(row["independent_validation"] for row in outcomes)
    assert runner.jobs == len(state["commands"]) == 30 and state["sessions"] == 2
    assert len({row["idempotency_key"] for row in state["commands"]}) == 30
    assert runner.run(cases[0])["status"] == "not_executed" and runner.jobs == 30
    text = path.read_text()
    assert "SECRET-TOKEN" not in text
    saved = json.loads(text)
    assert len(saved["cases"]) == 30 and saved["job_reservations"] == 30


def test_remote_keeps_terminal_raw_evidence_before_bad_worker_assertion(monkeypatch, tmp_path):
    fake_http(monkeypatch, wrong_worker=True)
    cases = evaluation.corpus()
    stages = []

    class Journal(evaluation.CheckpointJournal):
        def record(self, case, arm, outcome, stage):
            stages.append((stage, copy.deepcopy(outcome)))
            super().record(case, arm, outcome, stage)

    path = tmp_path / "partial.json"
    runner = evaluation.RemoteRunner(evaluation.CANONICAL_URL, "a" * 40,
                                     Journal(path, cases, "a" * 40, "bedrock"))
    outcome = runner.run(cases[0])
    assert outcome["status"] == "error" and runner.jobs == 1
    terminal = next(row for stage, row in stages if stage == "terminal_before_validation")
    assert terminal["result"]["commit"] == "b" * 40
    saved = json.loads(path.read_text())
    assert saved["cases"][0]["outcomes"]["strands"]["result"]["commit"] == "b" * 40
    assert sum(row["outcomes"]["strands"]["status"] == "not_executed" for row in saved["cases"]) == 29


def test_remote_ambiguous_post_never_retries_or_resets_global_allowance(monkeypatch, tmp_path):
    state = fake_http(monkeypatch, interrupted=True)
    cases = evaluation.corpus()
    journal = evaluation.CheckpointJournal(tmp_path / "partial.json", cases, "a" * 40, "bedrock")
    runner = evaluation.RemoteRunner(evaluation.CANONICAL_URL, "a" * 40, journal)
    assert runner.run(cases[0])["status"] == "error"
    assert runner.jobs == len(state["commands"]) == 1
    assert runner.run(cases[1])["status"] == "error"
    assert runner.jobs == len(state["commands"]) == 2
    assert state["commands"][0]["idempotency_key"] != state["commands"][1]["idempotency_key"]


def test_remote_unverified_session_fails_closed_and_wall_clock_never_submits(monkeypatch, tmp_path):
    state = fake_http(monkeypatch, wrong_boot=True)
    cases = evaluation.corpus()
    journal = evaluation.CheckpointJournal(tmp_path / "partial.json", cases, "a" * 40, "bedrock")
    runner = evaluation.RemoteRunner(evaluation.CANONICAL_URL, "a" * 40, journal)
    assert runner.run(cases[0])["status"] == "error"
    assert runner.run(cases[1])["status"] == "not_executed"
    assert runner.jobs == 0 and state["sessions"] == 1 and not state["commands"]
    runner.setup_error = False
    runner.deadline = 0
    assert runner.run(cases[2])["status"] == "not_executed" and runner.jobs == 0
    with pytest.raises(ValueError):
        evaluation.RemoteRunner("https://example.invalid", "a" * 40, journal)


def test_remote_wall_time_expiring_during_bootstrap_submits_no_job(monkeypatch, tmp_path):
    state = fake_http(monkeypatch)
    original_request = evaluation.RemoteRunner.request

    def request(self, path, payload=None):
        response = original_request(self, path, payload)
        if path == "/api/ops/bootstrap":
            self.deadline = 0
        return response

    monkeypatch.setattr(evaluation.RemoteRunner, "request", request)
    cases = evaluation.corpus()
    journal = evaluation.CheckpointJournal(tmp_path / "partial.json", cases, "a" * 40, "bedrock")
    runner = evaluation.RemoteRunner(evaluation.CANONICAL_URL, "a" * 40, journal)
    outcome = runner.run(cases[0])
    assert outcome["status"] == "not_executed"
    assert runner.jobs == journal.document["job_reservations"] == 0
    assert state["sessions"] == 1 and not state["commands"]


def test_invalid_result_never_receives_reference_bound_positive_credit(paired):
    row = copy.deepcopy(paired["cases"][0])
    outcome = row["outcomes"]["strands"]
    outcome.update(status="error", independent_validation=False)
    row["scores"]["strands"] = evaluation.score_outcome(row["expected"], outcome)
    # Reference identifiers can remain in retained failed raw evidence. They
    # cannot be reported as a successfully source-bound interaction review.
    assert row["scores"]["strands"]["selected_interaction_review"]["reference_ids_match"]
    counters = evaluation.aggregate([row], "strands")
    assert counters["selected_interaction_review"]["reference_bound_positive_cases"] == 0
    assert counters["selected_interaction_review"]["true_positive"] == 0


def test_empty_truncated_corpus_and_tampered_report_fail(tmp_path, paired):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"schema_version": 1, "version": evaluation.CORPUS_VERSION, "cases": []}))
    with pytest.raises(ValueError):
        evaluation.corpus(path)
    altered = copy.deepcopy(paired)
    altered["arms"]["strands"]["question_focus"]["recall"] = 1.0
    altered["arms"]["strands"]["question_focus"]["predicted"] += 1
    with pytest.raises(ValueError, match="integrity"):
        evaluation.write_report(altered, tmp_path / "report.json")
    evaluation.write_report(paired, tmp_path / "valid.json")
    assert json.loads((tmp_path / "valid.json").read_text())["report_sha256"] == paired["report_sha256"]
