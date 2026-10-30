"""Real SDK offline trajectories, repair bounds and independent gate failures."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from medplain.fixture_model import FixtureModel
from medplain.gate import canonical_hash, verify_receipts
from medplain import reconciliation_engine as engine
from medplain.reconciliation import (
    baseline_selection, build_artifact, examples, gate_errors, run_reconciliation, source_statements,
    validate_reconciliation_result,
)
from medplain.reconciliation_models import ReconciliationSelection
from medplain.ops_domain import OpsService


def _resolved_dossier():
    """Fresh synthetic context with explicit understanding, not a gold case ID."""
    return {
        "profile": "medplain-reconciliation-v1", "patient_alias": "Demo-Resolved", "language": "en",
        "caregiver_concerns": "", "documents": [
            {"document_id": "home", "kind": "home_list", "issued_at": "2026-10-01T09:00:00Z",
             "effective_at": None, "text": "Stop Amlodipine 5 mg QD.\nStop Furosemide 20 mg QD."},
            {"document_id": "discharge", "kind": "discharge", "issued_at": "2026-10-30T09:00:00Z",
             "effective_at": None, "text": "Stop Amlodipine 5 mg QD.\nStop Furosemide 20 mg QD."},
            {"document_id": "caregiver", "kind": "caregiver_note", "issued_at": "2026-10-30T09:30:00Z",
             "effective_at": None,
             "text": "I understand both medicines remain stopped. I do not have an identity, bottle, or restart question."},
        ],
    }


def _forced_native_planner(monkeypatch, payload):
    """Exercise the unchanged live tool-choice wrapper inside the actual offline SDK."""
    original = engine._model
    calls = []

    class NamedToolDelegate(FixtureModel):
        def __init__(self, context, output_model):
            super().__init__([], output_model, lambda: {})
            self.context = context
            self.output_attempts = 0
            self.invocation_index = None

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
            invocation = len(self.context.nodes)
            if self.invocation_index != invocation:
                self.invocation_index, self.output_attempts = invocation, 0
            name = kwargs["tool_choice"]["tool"]["name"]
            assert name in {spec["name"] for spec in tool_specs}
            arguments = {}
            if name == self._output_model.__name__:
                self.output_attempts += 1
                arguments = payload(self.context, self.output_attempts)
            calls.append({"invocation_index": invocation, "name": name})
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockStart": {"contentBlockIndex": 0,
                                         "start": {"toolUse": {"toolUseId": f"named-{len(calls)}", "name": name}}}}
            yield {"contentBlockDelta": {"contentBlockIndex": 0,
                                         "delta": {"toolUse": {"input": json.dumps(arguments)}}}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            yield {"metadata": {"usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                                 "metrics": {"latencyMs": 0}}}

    def model(mode, context, role, tools, output_model):
        if role != engine.PLANNER:
            return original(mode, context, role, tools, output_model)
        delegate = NamedToolDelegate(context, output_model)
        return engine.RequiredToolModel(delegate, lambda: [name for name in
            ("lookup_reconciliation_references", "check_reconciliation_constraints")
            if name not in context.completed[role]], output_model.__name__)

    monkeypatch.setattr(engine, "_model", model)
    return calls


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


def test_empty_concerns_still_exposes_original_caregiver_document_context():
    source = copy.deepcopy(examples()[0]["dossier"])
    source["caregiver_concerns"] = ""
    note = "\r\nThe orange card and the printed instructions seem different.\r\n"
    source["documents"].append({"document_id": "context-note", "kind": "caregiver_note",
                                "issued_at": "2026-10-29T13:00:00Z", "effective_at": None, "text": note})
    original = copy.deepcopy(source)
    context = engine.Context(source, "context-partition", False)
    context.index_verified = True
    tools = engine._tools(context)[engine.PLANNER]
    read_context = next(item for item in tools if item.tool_name == "check_reconciliation_constraints")
    evidence = read_context()
    assert evidence["caregiver_concerns"] == ""
    assert [row["statement_id"] for row in evidence["caregiver_context"]] == ["context-note:L2"]
    assert evidence["caregiver_context"][0]["citation"]["quote"] == "The orange card and the printed instructions seem different."
    assert evidence["source_documents"][-1]["text"].encode("utf-8") == note.encode("utf-8")
    partitioned = evidence["clinical_source_statements"] + evidence["caregiver_context"]
    by_id = {row["statement_id"]: row for row in partitioned}
    assert len(by_id) == len(partitioned)
    assert [by_id[sid] for sid in evidence["source_statement_order"]] == source_statements(source)
    assert source == original
    assert "already_rendered_checks" in evidence and "structural_floor" not in evidence
    evidence["source_documents"][0]["text"] = "A consumer changed its returned tool payload."
    assert context.dossier["documents"][0]["text"] == original["documents"][0]["text"]
    assert source == original


def test_virtual_concerns_and_caregiver_documents_both_keep_citation_identity():
    source = copy.deepcopy(examples()[0]["dossier"])
    source["caregiver_concerns"] = "First context line.\nSecond context line."
    source["documents"].append({"document_id": "reported-context", "kind": "caregiver_note",
                                "issued_at": "2026-10-29T13:00:00Z", "effective_at": None,
                                "text": "A separate caregiver document."})
    context = engine.Context(source, "two-context-origins", False)
    context.index_verified = True
    tool = next(item for item in engine._tools(context)[engine.PLANNER]
                if item.tool_name == "check_reconciliation_constraints")
    result = tool()
    assert [row["statement_id"] for row in result["caregiver_context"]] == [
        "reported-context:L1", "caregiver-concerns:L1", "caregiver-concerns:L2"]
    assert [row["citation"]["quote"] for row in result["caregiver_context"]] == [
        "A separate caregiver document.", "First context line.", "Second context line."]


@pytest.mark.parametrize("language", ["en", "el"])
def test_planner_sees_exact_rendered_floor_and_complete_question_meanings(language):
    source = copy.deepcopy(examples()[3]["dossier"])
    source["language"] = language
    context = engine.Context(source, "exact-question-context", False)
    context.index_verified = True
    tool = next(item for item in engine._tools(context)[engine.PLANNER]
                if item.tool_name == "check_reconciliation_constraints")
    evidence = tool()
    artifact = build_artifact(source, baseline_selection(source))
    floor = evidence["already_rendered_checks"]
    assert floor
    assert [{key: value for key, value in row.items() if key != "question_meaning_en"} for row in floor] == artifact["structural_issues"]
    assert set(evidence["question_catalog"]) == set(engine.QUESTION_CODES)
    assert evidence["question_catalog"]["confirm_identity_form"]["meaning_en"] == (
        "Do these names, bottles or formulations refer to the same medicine? Ask the pharmacist to confirm identity, strength and release form.")
    for row in floor:
        catalog = evidence["question_catalog"][row["question_code"]]
        assert row["question_text"] == catalog["rendered_text"]
        assert row["question_meaning_en"] == catalog["meaning_en"]
    assert "recommended_plan" not in evidence and "expected_plan" not in evidence


def test_model_roles_share_configuration_helper_and_planner_override_is_isolated(monkeypatch):
    monkeypatch.delenv("MEDPLAIN_BEDROCK_MODEL_ID", raising=False)
    monkeypatch.delenv("MEDPLAIN_RECONCILIATION_PLANNER_MODEL_ID", raising=False)
    assert engine.configured_model_ids() == {
        engine.INDEXER: "amazon.nova-micro-v1:0", engine.PLANNER: "amazon.nova-pro-v1:0",
        engine.PUBLISHER: "amazon.nova-micro-v1:0"}
    monkeypatch.setenv("MEDPLAIN_BEDROCK_MODEL_ID", "existing-base-setting")
    monkeypatch.setenv("MEDPLAIN_RECONCILIATION_PLANNER_MODEL_ID", "amazon.nova-micro-v1:0")
    recorded = []
    monkeypatch.setattr(engine, "BedrockModel", lambda **kwargs: recorded.append(kwargs) or SimpleNamespace())
    context = engine.Context(examples()[0]["dossier"], "role-model-configuration", False)
    tools = engine._tools(context)
    for role in engine.AGENT_NAMES:
        engine._model("bedrock", context, role, tools[role], engine._OUTPUT_MODELS[role])
    assert [row["model_id"] for row in recorded] == ["existing-base-setting", "amazon.nova-micro-v1:0", "existing-base-setting"]
    assert all(row["max_tokens"] == 4096 and row["temperature"] == 0.0 for row in recorded)
    # The actual adapter must use the shared helper, rather than duplicating env logic.
    monkeypatch.setattr(engine, "configured_model_ids", lambda: {role: "helper-selected-model" for role in engine.AGENT_NAMES})
    engine._model("bedrock", context, engine.PLANNER, tools[engine.PLANNER], ReconciliationSelection)
    assert recorded[-1]["model_id"] == "helper-selected-model"


@pytest.mark.parametrize("invalid", ["", "unknown-model", "us.amazon.nova-pro-v1:0", "arn:aws:bedrock:us-east-1:123456789012:inference-profile/private"])
def test_unknown_planner_model_fails_closed_without_echo_or_fallback(monkeypatch, invalid):
    monkeypatch.setenv("MEDPLAIN_RECONCILIATION_PLANNER_MODEL_ID", invalid)
    with pytest.raises(ValueError, match="^Unsupported reconciliation planner model ID$"):
        engine.configured_model_ids()


def test_fixture_discloses_configured_models_separately_from_actual_replay(monkeypatch):
    monkeypatch.delenv("MEDPLAIN_BEDROCK_MODEL_ID", raising=False)
    monkeypatch.delenv("MEDPLAIN_RECONCILIATION_PLANNER_MODEL_ID", raising=False)
    result = run_reconciliation(examples()[0]["dossier"], "configured-is-not-inferred", mode="fixture")
    assert result["status"] == "verified", result
    assert result["runtime"]["configured_model_ids"] == engine.configured_model_ids()
    assert result["runtime"]["model_adapter_ids"] == {role: "medplain-synthetic-replay" for role in engine.AGENT_NAMES}
    assert "not evidence every node completed inference" in result["runtime"]["model_identity_scope"]
    assert result["model_mode"] == "fixture"


def test_live_adapter_forces_references_then_case_context_without_changing_frozen_descriptor(monkeypatch):
    class RecordingDelegate:
        def __init__(self):
            self.choices = []

        async def stream(self, messages, **kwargs):
            self.choices.append(kwargs["tool_choice"])
            yield {"messageStart": {"role": "assistant"}}

    delegate = RecordingDelegate()
    monkeypatch.setattr(engine, "BedrockModel", lambda **kwargs: delegate)
    context = engine.Context(examples()[0]["dossier"], "fresh-order", False)
    context.completed[engine.PLANNER] = set()
    tools = engine._tools(context)[engine.PLANNER]
    assert engine.REQUIRED_TOOLS[engine.PLANNER] == ("check_reconciliation_constraints", "lookup_reconciliation_references")
    model = engine._model("bedrock", context, engine.PLANNER, tools, ReconciliationSelection)
    specs = [{"name": name} for name in ["lookup_reconciliation_references", "check_reconciliation_constraints", "ReconciliationSelection"]]

    async def exercise():
        for name in ["lookup_reconciliation_references", "check_reconciliation_constraints"]:
            async for _ in model.stream([], specs):
                pass
            assert delegate.choices[-1] == {"tool": {"name": name}}
            context.completed[engine.PLANNER].add(name)
        async for _ in model.stream([], specs):
            pass
        assert delegate.choices[-1] == {"tool": {"name": "ReconciliationSelection"}}
        context.completed[engine.PLANNER].clear()
        async for _ in model.stream([], specs):
            pass
        assert delegate.choices[-1] == {"tool": {"name": "lookup_reconciliation_references"}}

    asyncio.run(exercise())


@pytest.mark.parametrize("question", [
    {"code": "compare_instructions", "statement_ids": ["discharge:L1", "home:L1"]},
    {"code": "caregiver_question", "statement_ids": ["caregiver-concerns:L1"]},
])
def test_model_questions_are_not_filtered_or_repaired_into_better_citation_groups(monkeypatch, question):
    source = copy.deepcopy(examples()[0]["dossier"])
    selected = {"source_hash": canonical_hash(source), "questions": [question],
                "teachback": [{"code": "teachback", "statement_ids": ["home:L1"]}]}
    original = engine._model

    def model(mode, context, role, tools, output_model):
        if role == engine.PLANNER:
            return FixtureModel([item.tool_name for item in tools], output_model, lambda: copy.deepcopy(selected))
        return original(mode, context, role, tools, output_model)

    monkeypatch.setattr(engine, "_model", model)
    result = run_reconciliation(source, "preserve-model-plan", mode="fixture")
    assert result["status"] == "verified", result
    # These valid proposals may be semantically unhelpful. Preserve them so the
    # unchanged independent ruler measures that failure instead of hiding it.
    assert result["runtime"]["selection"] == selected
    assert result["artifact"]["proposed_questions"][0]["statement_ids"] == question["statement_ids"]
    assert result["artifact"]["proposed_questions"][0]["code"] == question["code"]
    validate_reconciliation_result(source, result)


@pytest.mark.parametrize("first_malformed", [False, True])
def test_actual_sdk_named_output_accepts_empty_questions_with_teachback_and_fresh_evidence(monkeypatch, first_malformed):
    source = _resolved_dossier()
    selected = {"source_hash": canonical_hash(source), "questions": [],
                "teachback": [{"code": "teachback", "statement_ids": ["home:L1", "discharge:L1", "caregiver:L1"]}]}

    def payload(context, attempt):
        value = copy.deepcopy(selected)
        if first_malformed and attempt == 1:
            value["questions"] = None
        return value

    calls = _forced_native_planner(monkeypatch, payload)
    result = run_reconciliation(source, "native-empty-questions", mode="fixture")
    assert result["status"] == "verified", result
    assert result["runtime"]["selection"] == selected
    assert result["artifact"]["proposed_questions"] == []
    assert result["artifact"]["teachback"][0]["statement_ids"] == selected["teachback"][0]["statement_ids"]
    assert result["runtime"]["nodes_executed"] == list(engine.AGENT_NAMES)
    assert result["runtime"]["structured_output_repairs"] == 0
    assert result["runtime"]["planner_rejections"] == []
    names = [row["name"] for row in calls]
    expected = ["lookup_reconciliation_references", "check_reconciliation_constraints", "ReconciliationSelection"]
    assert names == expected + (["ReconciliationSelection"] if first_malformed else [])
    attempts = [row for row in result["runtime"]["native_output_attempts"] if row["agent"] == engine.PLANNER]
    assert [row["status"] for row in attempts] == (["error", "success"] if first_malformed else ["success"])
    if first_malformed:
        assert attempts[0]["validation"]["kind"] == "pydantic"
        assert attempts[0]["validation"]["errors"][0]["loc"] == ["questions"]
    assert attempts[-1]["input_sha256"] == canonical_hash(selected)
    planner_tools = [row for row in result["runtime"]["tools_executed"] if row["agent"] == engine.PLANNER]
    assert [row["tool"] for row in planner_tools] == expected[:2]
    assert all(row["success"] for row in planner_tools)
    assert result["runtime"]["failure"] is None
    validate_reconciliation_result(source, result)


def test_actual_sdk_native_output_failure_keeps_existing_turn_cap_and_withholds_artifact(monkeypatch):
    source = _resolved_dossier()
    calls = _forced_native_planner(monkeypatch, lambda context, attempt: {
        "source_hash": canonical_hash(source), "questions": None,
        "teachback": [{"code": "teachback", "statement_ids": ["home:L1"]}]})
    result = run_reconciliation(source, "native-invalid-remains-refused", mode="fixture")
    assert result["status"] == "refused", result
    assert result["artifact"] is result["artifact_hash"] is result["explanation_text"] is None
    assert result["runtime"]["selection"] is None
    assert result["runtime"]["failure"] == {"code": "model_turn_budget", "agent": engine.PLANNER}
    assert result["runtime"]["structured_output_repairs"] == 0
    assert len(calls) == engine.MAX_MODEL_TURNS == 5
    attempts = [row for row in result["runtime"]["native_output_attempts"] if row["agent"] == engine.PLANNER]
    assert len(attempts) == 3 and all(row["status"] == "error" for row in attempts)
    assert result["receipts"] == []
    validate_reconciliation_result(source, result)


@pytest.mark.parametrize("include_caregiver", [False, True])
def test_negative_caregiver_context_is_never_auto_cited_or_semantically_filtered(monkeypatch, include_caregiver):
    source = _resolved_dossier()
    ids = ["home:L1", "discharge:L2"] + (["caregiver:L1"] if include_caregiver else [])
    selected = {"source_hash": canonical_hash(source), "questions": [{"code": "confirm_identity_form", "statement_ids": ids}],
                "teachback": [{"code": "teachback", "statement_ids": ["home:L1"]}]}
    original = engine._model

    def model(mode, context, role, tools, output_model):
        if role == engine.PLANNER:
            return FixtureModel([item.tool_name for item in tools], output_model, lambda: copy.deepcopy(selected))
        return original(mode, context, role, tools, output_model)

    monkeypatch.setattr(engine, "_model", model)
    result = run_reconciliation(source, "negative-context-preserves-model-decision", mode="fixture")
    if include_caregiver:
        # A semantically bad yet gate-valid proposal stays visible to the independent ruler.
        assert result["status"] == "verified", result
        assert result["runtime"]["selection"] == selected
        assert result["artifact"]["proposed_questions"][0]["statement_ids"] == ids
    else:
        assert result["status"] == "refused", result
        assert result["artifact"] is None
        assert result["runtime"]["structured_output_repairs"] == 1
        assert len(result["runtime"]["planner_rejections"]) == 2
        assert all(row["rejected_typed_selection"] == selected for row in result["runtime"]["planner_rejections"])
        assert all("requires cited caregiver context" in row["reason"] for row in result["runtime"]["planner_rejections"])
    validate_reconciliation_result(source, result)


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
    assert len(result["runtime"]["planner_rejections"]) == 2
    assert result["runtime"]["planner_rejections"][-1]["rejected_typed_selection"]["questions"][0]["statement_ids"] == ["foreign:L1"]
    assert result["runtime"]["repair_diagnostics"][-1]["exhausted"] is True
    assert result["runtime"]["repair_diagnostics"][-1]["reason"] == "questions[0]: Question citations are missing, duplicated or outside this source"
    assert result["runtime"]["failure"] == {"code": "structured_repair_exhausted", "agent": engine.PLANNER,
                                              "contract_code": "planner_contract"}
    assert "foreign:L1" not in json.dumps(result["trace"])
    validate_reconciliation_result(source, result, "bad-proposal")


def test_actual_validator_reason_requires_fresh_model_repair_and_stays_private(monkeypatch):
    original_model = engine._model
    source = copy.deepcopy(examples()[0]["dossier"])
    valid = {"source_hash": canonical_hash(source), "questions": [
        {"code": "caregiver_question", "statement_ids": ["caregiver-concerns:L1", "home:L1"]}],
        "teachback": [{"code": "teachback", "statement_ids": ["discharge:L1"]}]}
    bad = copy.deepcopy(valid)
    bad["questions"][0] = {"code": "confirm_taper", "statement_ids": ["home:L1"]}
    seen = []

    class CapturingModel(FixtureModel):
        async def stream(self, messages, *args, **kwargs):
            seen.append(json.dumps(messages))
            async for event in super().stream(messages, *args, **kwargs):
                yield event

    def model(mode, context, role, tools, output_model):
        if role == engine.PLANNER:
            holder = {}
            replay = CapturingModel([item.tool_name for item in tools], output_model,
                                    lambda: copy.deepcopy(bad if holder["model"]._invocation == 1 else valid))
            holder["model"] = replay
            return replay
        return original_model(mode, context, role, tools, output_model)

    monkeypatch.setattr(engine, "_model", model)
    result = run_reconciliation(source, "fresh-model-repair", mode="fixture")
    assert result["status"] == "verified", result
    assert result["runtime"]["selection"] == valid
    assert result["runtime"]["structured_output_repairs"] == 1
    assert result["runtime"]["nodes_executed"] == [engine.INDEXER, engine.PLANNER, engine.PLANNER, engine.PUBLISHER]
    assert result["runtime"]["invocation_outcomes"] == ["completed", "repair", "completed", "completed"]
    assert result["runtime"]["failure"] is None
    reason = "questions[0]: A taper question requires cited taper phases"
    assert any(reason in message for message in seen)
    rejection = result["runtime"]["planner_rejections"][0]
    assert rejection["reason"] == reason and rejection["rejected_typed_selection"] == bad
    assert rejection["selection_sha256"] == canonical_hash(bad)
    for index in [2, 3]:
        records = [row for row in result["runtime"]["tools_executed"] if row["invocation_index"] == index]
        assert {row["tool"] for row in records if row["success"]} == set(engine.REQUIRED_TOOLS[engine.PLANNER])
    validate_reconciliation_result(source, result)
    share = {"execution_id": result["session_id"], "source_profile": "medplain-reconciliation-v1",
             "source": source, "result": result, "intake": None,
             "case": {"status": "released", "source_hash": canonical_hash(source), "artifact_hash": result["artifact_hash"]}}
    public = OpsService(SimpleNamespace(get=lambda *_: copy.deepcopy(share))).patient("a" * 32)
    assert "runtime" not in public["result"]
    assert "planner_rejections" not in json.dumps(public)
    assert "rejected_typed_selection" not in json.dumps(public)


def test_private_diagnostics_bound_untrusted_ids_and_keep_unknown_messages_type_only():
    source = copy.deepcopy(examples()[0]["dossier"])
    context = engine.Context(source, "bounded-diagnostics", False)
    context.nodes = [engine.INDEXER, engine.PLANNER]
    payload = {"source_hash": canonical_hash(source), "questions": [
        {"code": "caregiver_question", "statement_ids": ["long-" + "x" * 50000] * 8} for _ in range(12)],
        "teachback": [{"code": "teachback", "statement_ids": ["wide-" + "λ" * 50000] * 8} for _ in range(6)]}
    for _ in range(engine.MAX_PRIVATE_REJECTIONS + 2):
        reason = engine._record_planner_rejection(context, payload, ValueError("provider-secret-must-not-appear"))
        assert "provider-secret" not in reason
    assert len(context.planner_rejections) == engine.MAX_PRIVATE_REJECTIONS
    for record in context.planner_rejections:
        assert len(json.dumps(record, ensure_ascii=False).encode("utf-8")) <= engine.MAX_PRIVATE_REJECTION_BYTES
        assert record["selection_truncated"] is True
        assert record["selection_counts"] == {"questions": 12, "teachback": 6}
        assert record["validator"] == {"kind": "exception_type", "exception_type": "ValueError"}
    assert "provider-secret" not in json.dumps(context.planner_rejections)


def test_pydantic_diagnostics_omit_raw_input_and_context():
    try:
        ReconciliationSelection.model_validate({"source_hash": "private-input-should-not-be-copied", "questions": [], "teachback": []})
    except ValueError as exc:
        details = engine._validator_details(exc)
    assert details["kind"] == "pydantic"
    assert details["errors"]
    assert all(set(row) == {"loc", "type", "msg"} for row in details["errors"])
    assert "private-input-should-not-be-copied" not in json.dumps(details)


def test_native_output_diagnostics_are_redacted_bounded_observations_without_mutations():
    source = examples()[0]["dossier"]
    context = engine.Context(source, "private-native-attempts", False)
    context.nodes = [engine.INDEXER, engine.PLANNER]
    context.completed[engine.PLANNER] = {"lookup_reconciliation_references", "check_reconciliation_constraints"}
    hook = engine.ReconciliationHook(context, engine.PLANNER)
    payload = {"source_hash": canonical_hash(source), "questions": None,
               "teachback": [{"code": "teachback", "statement_ids": ["private-source-value-never-copied"]}],
               "private-secret-field": "private-value-never-copied"}
    output = {"name": "ReconciliationSelection", "input": payload}
    result = {"status": "error", "content": [{"text": "provider-secret-must-not-appear"}]}
    saved_output, saved_result = copy.deepcopy(output), copy.deepcopy(result)
    for _ in range(engine.MAX_NATIVE_OUTPUT_ATTEMPTS + 3):
        hook.after_tool_call(SimpleNamespace(tool_use=output, result=result))
    assert output == saved_output and result == saved_result
    assert context.completed[engine.PLANNER] == {"lookup_reconciliation_references", "check_reconciliation_constraints"}
    assert context.tools == [] and context.repairs == hook.repairs == hook.turns == 0
    assert context.selection is None and context.failure is None
    assert len(context.native_output_attempts) == engine.MAX_NATIVE_OUTPUT_ATTEMPTS
    assert context.native_output_attempts_dropped == 3
    for record in context.native_output_attempts:
        assert record["input_sha256"] == canonical_hash(payload)
        assert record["validation"]["kind"] == "pydantic"
        assert all(set(row) == {"loc", "type", "msg"} for row in record["validation"]["errors"])
        assert len(json.dumps(record, ensure_ascii=False).encode("utf-8")) <= engine.MAX_NATIVE_OUTPUT_ATTEMPT_BYTES
    serialized = json.dumps(context.native_output_attempts)
    assert "private-secret-field" not in serialized and "private-value-never-copied" not in serialized
    assert "private-source-value-never-copied" not in serialized and "provider-secret" not in serialized
    assert "unknown_field" in serialized
    # A provider failure after schema-valid input still cannot leak provider text.
    valid = {"source_hash": canonical_hash(source), "questions": [],
             "teachback": [{"code": "teachback", "statement_ids": ["home:L1"]}]}
    hook.after_tool_call(SimpleNamespace(tool_use={"name": "ReconciliationSelection", "input": valid}, result=result))
    assert context.native_output_attempts[-1]["validation"] == {"kind": "sdk_tool_error", "details": "No provider error text retained"}
    assert "provider-secret" not in json.dumps(context.native_output_attempts)


def test_tool_eligibility_exposes_only_existing_preconditions_not_a_plan():
    source = copy.deepcopy(examples()[0]["dossier"])
    context = engine.Context(source, "eligibility-not-relevance", False)
    context.index_verified = True
    tool = next(item for item in engine._tools(context)[engine.PLANNER] if item.tool_name == "check_reconciliation_constraints")
    evidence = tool()["code_eligibility"]
    assert "Eligibility is not relevance" in evidence["scope"]
    assert evidence["codes"]["confirm_taper"]["requires_any_witness"] == []
    assert evidence["codes"]["confirm_identity_form"]["requires_any_witness"] == ["caregiver-concerns:L1"]
    assert evidence["codes"]["compare_instructions"]["minimum_distinct_citations"] == 2
    assert evidence["codes"]["teachback"]["section"] == "teachback_only"
    assert "questions" not in evidence


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
    assert result["runtime"]["failure"] == {"code": "external_error", "exception_type": "RuntimeError"}
    validate_reconciliation_result(source, result, "provider-failed")
