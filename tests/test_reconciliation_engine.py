"""Real SDK offline trajectories, repair bounds and independent gate failures."""

import copy
from types import SimpleNamespace

import pytest

from medplain.fixture_model import FixtureModel
from medplain.gate import canonical_hash, verify_receipts
from medplain import reconciliation_engine as engine
from medplain.reconciliation import (
    baseline_selection, build_artifact, examples, gate_errors, run_reconciliation, validate_reconciliation_result,
)


@pytest.mark.parametrize("example", examples(), ids=lambda row: row["example_id"])
def test_actual_sdk_indexes_plans_and_publishes_with_fresh_tools(example):
    result = run_reconciliation(example["dossier"], "sdk-" + example["example_id"], mode="fixture")
    assert result["status"] == "verified", result
    validate_reconciliation_result(example["dossier"], result, result["session_id"])
    assert result["runtime"]["nodes_executed"] == list(engine.AGENT_NAMES)
    assert result["runtime"]["sdk_version"] == "1.53.0"
    assert result["runtime"]["semantic_relevance_verified"] is False
    assert result["patient_regimen"] is None
    for index, role in enumerate(result["runtime"]["nodes_executed"], start=1):
        observed = [row for row in result["runtime"]["tools_executed"] if row["invocation_index"] == index]
        assert {row["tool"] for row in observed if row["success"]} == set(engine.REQUIRED_TOOLS[role])
        assert all(row["agent"] == role for row in observed)


def test_stress_refuses_changed_source_quote_then_real_graph_rebuilds():
    source = examples()[3]["dossier"]
    result = run_reconciliation(source, "stress-generation-2", mode="fixture", stress=True)
    assert result["status"] == "verified", result
    assert result["runtime"]["nodes_executed"] == [engine.INDEXER, engine.PLANNER, engine.PUBLISHER, engine.PLANNER, engine.PUBLISHER]
    assert [row["verdict"] for row in result["receipts"]] == ["refused", "verified"]
    assert result["receipts"][0]["artifact_hash"] != result["receipts"][1]["artifact_hash"]
    assert result["correction_count"] == 1
    assert verify_receipts(result["receipts"])
    assert any(row["status"] == "handoff" for row in result["trace"])
    assert "[mutated draft]" not in result["explanation_text"]
    for index, role in enumerate(result["runtime"]["nodes_executed"], start=1):
        names = {row["tool"] for row in result["runtime"]["tools_executed"] if row["invocation_index"] == index and row["success"]}
        assert names == set(engine.REQUIRED_TOOLS[role])
    validate_reconciliation_result(source, result, "stress-generation-2")


@pytest.mark.parametrize("index,field", [(3, "comparator"), (2, "taper"), (0, "dose")])
def test_stress_changes_clinical_meaning_coherently_and_gate_rejects_it(index, field):
    source = copy.deepcopy(examples()[index]["dossier"])
    saved_source = copy.deepcopy(source)
    selected = baseline_selection(source)
    artifact = build_artifact(source, selected)
    before = copy.deepcopy(artifact)
    engine.inject_stress(artifact)
    assert source == saved_source
    assert gate_errors(source, artifact, selected)
    if field == "comparator":
        before_row, after_row = before["source_statements"][1], artifact["source_statements"][1]
        assert before_row["clinical"]["numeric_literals"] == after_row["clinical"]["numeric_literals"]
        assert "< 100" in before_row["citation"]["quote"]
        assert "> 100" in after_row["citation"]["quote"]
        assert after_row["clinical"]["logic_tokens"] == [">", "AND", "="]
    else:
        key = "taper_phases" if field == "taper" else "medications"
        assert artifact["source_statements"][0]["clinical"][key][0]["dose"] in {"200", "100"}
        assert artifact["source_statements"][0]["citation"]["quote"] != before["source_statements"][0]["citation"]["quote"]


@pytest.mark.parametrize("mutation", ["quote", "phase", "hold_comparator", "stop_action", "floor_omission", "extra_advice"])
def test_wrong_builder_artifact_is_never_replaced_or_published(monkeypatch, mutation):
    index = 2 if mutation == "phase" else 3 if mutation == "hold_comparator" else 0
    source = examples()[index]["dossier"]
    original = build_artifact
    def wrong_builder(dossier, selection):
        artifact = original(dossier, selection)
        if mutation == "quote":
            artifact["source_statements"][0]["citation"]["quote"] += " invented"
        elif mutation == "phase":
            artifact["source_statements"][0]["clinical"]["taper_phases"][0]["dose"] = "200"
        elif mutation == "hold_comparator":
            artifact["source_statements"][1]["clinical"]["condition_text"] = "systolic blood pressure > 100 mmHg"
        elif mutation == "stop_action":
            artifact["source_statements"][3]["clinical"]["medications"][0]["action"] = "take"
        elif mutation == "floor_omission":
            artifact["structural_issues"] = []
        else:
            artifact["advice"] = "Take the newer dose."
        return artifact
    monkeypatch.setattr(engine, "build_artifact", wrong_builder)
    result = run_reconciliation(source, "wrong-builder-" + mutation, mode="fixture")
    assert result["status"] == "refused", result
    assert result["artifact"] is result["artifact_hash"] is result["explanation_text"] is None
    assert len(result["receipts"]) == 2
    assert all(row["verdict"] == "refused" for row in result["receipts"])
    validate_reconciliation_result(source, result, result["session_id"])


def test_bad_semantic_citation_gets_one_bounded_repair_without_exposing_output(monkeypatch):
    original = engine._model
    def model(mode, context, role, tools, output_model):
        if role == engine.PLANNER:
            return FixtureModel([item.tool_name for item in tools], output_model,
                                lambda: {"source_hash": canonical_hash(context.dossier), "questions": [
                                    {"code": "caregiver_question", "statement_ids": ["foreign:L1"]}],
                                         "teachback": [{"code": "teachback", "statement_ids": ["home:L1"]}]})
        return original(mode, context, role, tools, output_model)
    monkeypatch.setattr(engine, "_model", model)
    source = examples()[0]["dossier"]
    result = run_reconciliation(source, "bad-proposal", mode="fixture")
    assert result["status"] == "refused"
    assert result["artifact"] is None
    assert result["runtime"]["nodes_executed"] == [engine.INDEXER, engine.PLANNER, engine.PLANNER]
    assert result["runtime"]["structured_output_repairs"] == 1
    assert result["receipts"] == []
    assert "foreign:L1" not in str(result)
    validate_reconciliation_result(source, result, "bad-proposal")


def test_a_valid_nonbaseline_semantic_plan_is_used_not_silently_replaced(monkeypatch):
    source = examples()[0]["dossier"]
    selected = {"source_hash": canonical_hash(source), "questions": [
        {"code": "caregiver_question", "statement_ids": ["caregiver-concerns:L1", "discharge:L1", "home:L1"]}],
        "teachback": [{"code": "teachback", "statement_ids": ["discharge:L2"]}]}
    original = engine._model
    def model(mode, context, role, tools, output_model):
        if role == engine.PLANNER:
            return FixtureModel([item.tool_name for item in tools], output_model, lambda: copy.deepcopy(selected))
        return original(mode, context, role, tools, output_model)
    monkeypatch.setattr(engine, "_model", model)
    result = run_reconciliation(source, "model-owned-context", mode="fixture")
    assert result["status"] == "verified", result
    assert result["runtime"]["selection"] == selected
    assert result["runtime"]["selection"] != baseline_selection(source)
    assert result["artifact"]["proposed_questions"][0]["code"] == "caregiver_question"
    validate_reconciliation_result(source, result)


def _prepared_context():
    source = examples()[0]["dossier"]
    context = engine.Context(source, "case:generation-1", False)
    context.selection = baseline_selection(source)
    context.nodes = [engine.INDEXER, engine.PLANNER, engine.PUBLISHER]
    tool = engine._tools(context)[engine.PUBLISHER][0]
    return context, tool


@pytest.mark.parametrize("mutation", ["session", "source", "nonce", "invocation", "snapshot", "selection", "ref", "presentation"])
def test_publication_reference_is_bound_to_exact_fresh_request(mutation):
    context, tool = _prepared_context()
    plan = tool()
    assert engine.resolve_publication(context, plan)["patient_regimen"] is None
    if mutation == "session":
        context.session_id = "case:generation-2"
    elif mutation == "source":
        context.dossier = copy.deepcopy(context.dossier)
        context.dossier["caregiver_concerns"] = "changed note"
    elif mutation == "nonce":
        context.nonce = "0" * 32
    elif mutation == "invocation":
        context.nodes.append(engine.PUBLISHER)
    elif mutation == "snapshot":
        context.snapshot += " "
        # Byte changes that are semantically identical JSON are harmless; make
        # the structured content differ so the actual content hash must fail.
        context.snapshot = context.snapshot.replace("Lisinopril", "DifferentMedicine", 1)
    elif mutation == "selection":
        context.selection["teachback"][0]["statement_ids"] = ["home:L2"]
    elif mutation == "ref":
        plan["artifact_ref"] = "clarify-" + "0" * 32
    else:
        plan["presentation"] = "prescription"
    with pytest.raises(ValueError):
        engine.resolve_publication(context, plan)


def test_latest_tool_reference_and_immutable_snapshot_semantics():
    context, tool = _prepared_context()
    old = tool()
    artifact = engine.resolve_publication(context, old)
    artifact["source_statements"].clear()
    assert engine.resolve_publication(context, old)["source_statements"]
    new = tool()
    assert old != new
    with pytest.raises(ValueError):
        engine.resolve_publication(context, old)
    context.selection = None
    with pytest.raises(ValueError):
        tool()
    with pytest.raises(ValueError):
        engine.resolve_publication(context, new)


def test_model_turn_budget_and_required_tool_repair_are_bounded():
    context = engine.Context(examples()[0]["dossier"], "bounded", False)
    context.nodes = [engine.PLANNER]
    context.invocation_outcomes = ["running"]
    hook = engine.ReconciliationHook(context, engine.PLANNER)
    for _ in range(engine.MAX_MODEL_TURNS):
        hook.before_model_call(None)
    with pytest.raises(RuntimeError):
        hook.before_model_call(None)
    event = SimpleNamespace(resume=None)
    hook._repair(event, "A fresh tool is missing.")
    assert event.resume
    with pytest.raises(RuntimeError):
        hook._repair(event, "Still missing.")


def test_runtime_has_no_silent_provider_fallback(monkeypatch):
    def unavailable(*args):
        raise RuntimeError("provider diagnostic must not appear publicly")
    monkeypatch.setattr(engine, "_make_graph", unavailable)
    source = examples()[0]["dossier"]
    result = run_reconciliation(source, "provider-failed", mode="bedrock")
    assert result["status"] == "refused"
    assert result["model_mode"] == "bedrock"
    assert result["artifact"] is None
    assert "provider diagnostic" not in str(result)
    validate_reconciliation_result(source, result, "provider-failed")
