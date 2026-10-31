"""Actual pinned SDK wire and dispatch, with an offline Bedrock client.

These tests establish request compatibility and unchanged refusal behavior.
They do not establish that a real provider's ToolUse failure is repaired.
"""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

from botocore.exceptions import EventStreamError
import pytest
import strands.models.bedrock as bedrock_sdk
from strands.tools.structured_output.structured_output_tool import StructuredOutputTool

from medplain import reconciliation_engine as engine
from medplain.gate import canonical_hash, verify_receipts
from medplain.reconciliation import run_reconciliation
from medplain.reconciliation_evaluation import TARGETED_CODES, match_focus
from medplain.required_tool_model import NOVA_TOOL_SCHEMA_PROFILE, RequiredToolModel, nova_tool_specs


def _source():
    return {
        "profile": "medplain-reconciliation-v1", "patient_alias": "Demo-Nova", "language": "en",
        "caregiver_concerns": "", "documents": [
            {"document_id": "home", "kind": "home_list", "issued_at": "2026-10-20T09:00:00Z",
             "effective_at": None, "text": "Stop Amlodipine 5 mg by mouth QD."},
            {"document_id": "discharge", "kind": "discharge", "issued_at": "2026-10-30T09:00:00Z",
             "effective_at": None, "text": "Stop Amlodipine 5 mg by mouth QD."},
        ],
    }


def _wire(source):
    return {
        "selection": {"source_hash": canonical_hash(source), "questions": [],
                      "teachback": [{"code": "teachback", "statement_ids": ["home:L1", "discharge:L1"]}]},
        "nonask_review": [{"disposition": "resolved", "intent": "compare_instructions",
                           "statement_ids": ["home:L1", "discharge:L1"],
                           "reason": "Both original instructions explicitly stop the same medicine."}],
    }


def test_projection_removes_root_metadata_only_and_copies_every_nested_value():
    nested = {"type": "string", "description": "Keep this instruction", "title": "KeepNestedTitle",
              "enum": ["one", "two"], "minLength": 1, "maxLength": 160}
    original = [{"name": "CurrentTool", "description": "Keep the tool-level description",
                 "inputSchema": {"json": {"$schema": "https://json-schema.org/draft/2020-12/schema",
                    "title": "TopTitle", "description": "TopDescription", "type": "object",
                    "properties": {"reason": nested, "items": {"type": "array", "maxItems": 8,
                        "items": {"type": "object", "additionalProperties": False,
                                  "properties": {"code": {"enum": ["a", "b"]}}, "required": ["code"]}}},
                    "required": ["reason", "items"]}}}]
    saved = deepcopy(original)
    projected = nova_tool_specs(original)
    assert NOVA_TOOL_SCHEMA_PROFILE == "nova-v1-root-metadata-v1"
    assert list(projected[0]["inputSchema"]["json"]) == ["type", "properties", "required"]
    assert projected[0]["description"] == original[0]["description"]
    assert projected[0]["inputSchema"]["json"]["properties"] == saved[0]["inputSchema"]["json"]["properties"]
    assert projected[0]["inputSchema"]["json"]["required"] == ["reason", "items"]
    projected[0]["inputSchema"]["json"]["properties"]["reason"]["enum"].append("mutation")
    projected[0]["inputSchema"]["json"]["required"].clear()
    assert original == saved


@pytest.mark.parametrize("key,value", [
    ("additionalProperties", False), ("additionalProperties", True),
    ("minProperties", 1), ("maxProperties", 4), ("allOf", []), ("anyOf", []),
    ("$defs", {}), ("patternProperties", {}), ("unevaluatedProperties", False),
])
def test_projection_refuses_unknown_root_semantics_instead_of_dropping_them(key, value):
    specs = [{"name": "CurrentTool", "inputSchema": {"json": {
        "type": "object", "properties": {}, key: value}}}]
    saved = deepcopy(specs)
    with pytest.raises(RuntimeError, match="^Unsupported Nova tool schema root; publication refused$"):
        nova_tool_specs(specs)
    assert specs == saved


@pytest.mark.parametrize("schema", [None, [], {}, {"type": "array", "properties": {}},
                                     {"type": ["object", "null"], "properties": {}},
                                     {"type": "object", "properties": None}])
def test_projection_refuses_non_object_or_incomplete_roots(schema):
    with pytest.raises(RuntimeError, match="Unsupported Nova tool schema root"):
        nova_tool_specs([{"name": "CurrentTool", "inputSchema": {"json": schema}}])


def test_legacy_wrapper_keeps_original_tool_specs_and_choice_unchanged_by_default():
    calls = []

    class Delegate:
        async def stream(self, messages, **kwargs):
            calls.append(kwargs)
            yield {"messageStart": {"role": "assistant"}}

    specs = [{"name": "Original", "inputSchema": {"json": {
        "type": "object", "title": "ExistingTitle", "properties": {}, "additionalProperties": False}}}]
    model = RequiredToolModel(Delegate(), lambda: [], "Original")

    async def exercise():
        return [event async for event in model.stream([], specs)]

    assert asyncio.run(exercise()) == [{"messageStart": {"role": "assistant"}}]
    assert calls[0]["tool_specs"] is specs
    assert calls[0]["tool_choice"] == {"tool": {"name": "Original"}}
    assert model.nova_schema_compatibility is False


def _offline_sdk(monkeypatch, payload=None, *, provider_error=False, wire_requests=None):
    """Run the real Bedrock adapter and real Graph, replacing only AWS I/O.

    Indexer/publisher use existing SDK fixture models; the planner uses the
    actual BedrockModel request formatter, streaming and structured dispatch.
    """
    requests = wire_requests if wire_requests is not None else []
    holder = {}
    counts = {}
    original_model = engine._model

    class Client:
        meta = SimpleNamespace(region_name="us-east-1")

        def converse_stream(self, **request):
            requests.append(deepcopy(request))
            for tool in request["toolConfig"]["tools"]:
                schema = tool["toolSpec"]["inputSchema"]["json"]
                assert schema["type"] == "object"
                assert set(schema) <= {"type", "properties", "required"}
            name = request["toolConfig"]["toolChoice"]["tool"]["name"]
            context = holder["context"]
            invocation = len(context.nodes)
            arguments = {}
            if name == "PlanningWire":
                counts[invocation] = counts.get(invocation, 0) + 1
                arguments = (payload(context, counts[invocation]) if payload else _wire(context.dossier))

            def events():
                yield {"messageStart": {"role": "assistant"}}
                if name == "PlanningWire" and provider_error:
                    raise EventStreamError({"Error": {"Code": "modelStreamErrorException",
                        "Message": "Offline injected provider ToolUse failure"}}, "ConverseStream")
                yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {
                    "toolUseId": f"offline-{len(requests)}", "name": name}}}}
                yield {"contentBlockDelta": {"contentBlockIndex": 0,
                    "delta": {"toolUse": {"input": json.dumps(arguments)}}}}
                yield {"contentBlockStop": {"contentBlockIndex": 0}}
                yield {"messageStop": {"stopReason": "tool_use"}}
                yield {"metadata": {"usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                                     "metrics": {"latencyMs": 0}}}

            return {"stream": events()}

    class Session:
        region_name = "us-east-1"

        def client(self, **kwargs):
            assert kwargs["service_name"] == "bedrock-runtime"
            config = kwargs["config"]
            assert config.connect_timeout == 5 and config.read_timeout == 45
            assert config.retries == {"total_max_attempts": 1}
            return Client()

    def model(mode, context, role, tools, output_model):
        if role != engine.PLANNER:
            return original_model("fixture", context, role, tools, output_model)
        holder["context"] = context
        return original_model("bedrock", context, role, tools, output_model)

    monkeypatch.setattr(bedrock_sdk.boto3, "Session", Session)
    monkeypatch.setattr(engine, "_model", model)
    return requests


def test_real_sdk_wire_projects_only_planner_roots_and_keeps_native_schema(monkeypatch):
    boundaries = []
    original_stream = RequiredToolModel.stream

    async def capture_incoming(model, messages, tool_specs=None, system_prompt=None, *, tool_choice=None, **kwargs):
        # ToolRegistry normalizes descriptions before this wrapper boundary.
        # Compare provider output to that exact input stage, not a fresh raw
        # StructuredOutputTool created before SDK registry normalization.
        before = deepcopy(tool_specs)
        baseline = model.delegate.format_request(messages=deepcopy(messages), tool_specs=deepcopy(tool_specs),
                                                 tool_choice=tool_choice)
        boundaries.append({"incoming": tool_specs, "snapshot": before, "baseline": baseline})
        async for event in original_stream(model, messages, tool_specs, system_prompt,
                                           tool_choice=tool_choice, **kwargs):
            yield event
        assert tool_specs == before

    monkeypatch.setattr(RequiredToolModel, "stream", capture_incoming)
    requests = _offline_sdk(monkeypatch)
    result = run_reconciliation(_source(), "nova-projection-sdk-dispatch", mode="bedrock")
    assert result["status"] == "verified", result
    assert result["runtime"]["nodes_executed"] == list(engine.AGENT_NAMES)
    assert result["runtime"]["invocation_outcomes"] == ["completed"] * 3
    assert result["runtime"]["structured_output_repairs"] == 0
    assert [r["toolConfig"]["toolChoice"]["tool"]["name"] for r in requests] == [
        "lookup_reconciliation_references", "check_reconciliation_constraints", "PlanningWire"]
    native = StructuredOutputTool(engine.PlanningWire).tool_spec
    native_schema = native["inputSchema"]["json"]
    assert {"title", "description"} <= set(native_schema)
    assert engine.planning_wire_schema_sha256() == "b2a2472a7c7d282962d7c13db283580ee8fb6488a9249dd46ed4773d48df6944"
    assert len(boundaries) == len(requests)
    for request, boundary in zip(requests, boundaries, strict=True):
        assert boundary["incoming"] == boundary["snapshot"]
        assert request["modelId"] == "amazon.nova-pro-v1:0"
        assert request["inferenceConfig"] == {"maxTokens": 4096, "temperature": 0.0}
        assert request["additionalModelRequestFields"] == {"inferenceConfig": {"topK": 1}}
        actual_tools = [item["toolSpec"] for item in request["toolConfig"]["tools"]]
        baseline_tools = [item["toolSpec"] for item in boundary["baseline"]["toolConfig"]["tools"]]
        assert [item["name"] for item in actual_tools] == [item["name"] for item in baseline_tools]
        for actual, baseline in zip(actual_tools, baseline_tools, strict=True):
            assert actual["description"] == baseline["description"]
            projected, incoming = actual["inputSchema"]["json"], baseline["inputSchema"]["json"]
            assert projected["type"] == incoming["type"] == "object"
            assert list(projected) == [key for key in incoming if key not in {"$schema", "title", "description"}]
            assert projected["properties"] == incoming["properties"]
            assert projected["required"] == incoming["required"]
            if actual["name"] == "PlanningWire":
                assert incoming["properties"]["nonask_review"]["description"] == "Property nonask_review"
                assert canonical_hash(projected) != canonical_hash(incoming)
    assert result["artifact"]["patient_regimen"] is None
    assert verify_receipts(result["receipts"])


@pytest.mark.parametrize("role", engine.AGENT_NAMES)
def test_compatibility_opt_in_is_planner_only(monkeypatch, role):
    class Session:
        region_name = "us-east-1"

        def client(self, **kwargs):
            return SimpleNamespace(meta=SimpleNamespace(region_name="us-east-1"))

    monkeypatch.setattr(bedrock_sdk.boto3, "Session", Session)
    context = engine.Context(_source(), "nova-profile-scope", False)
    model = engine._model("bedrock", context, role, engine._tools(context)[role], engine._OUTPUT_MODELS[role])
    assert model.nova_schema_compatibility is (role == engine.PLANNER)


def test_real_sdk_still_rejects_extra_output_key_then_requires_new_authored_payload(monkeypatch):
    authored = []

    def payload(context, attempt):
        wire = _wire(context.dossier)
        if attempt == 1:
            wire["unsupported_extra"] = "must be rejected, not dropped"
        authored.append(deepcopy(wire))
        return wire

    requests = _offline_sdk(monkeypatch, payload)
    result = run_reconciliation(_source(), "nova-extra-key-repair", mode="bedrock")
    assert result["status"] == "verified", result
    assert [r["toolConfig"]["toolChoice"]["tool"]["name"] for r in requests] == [
        "lookup_reconciliation_references", "check_reconciliation_constraints", "PlanningWire", "PlanningWire"]
    native = [a for a in result["runtime"]["native_output_attempts"] if a["agent"] == engine.PLANNER]
    assert [a["status"] for a in native] == ["error", "success"]
    assert "unsupported_extra" in authored[0] and "unsupported_extra" not in authored[1]
    assert result["runtime"]["structured_output_repairs"] == 0


@pytest.mark.parametrize("invalid", ["missing_teachback", "unsupported_extra"])
def test_real_sdk_persistent_malformed_output_stays_refused_within_existing_budget(monkeypatch, invalid):
    authored = []

    def payload(context, attempt):
        wire = _wire(context.dossier)
        if invalid == "missing_teachback":
            del wire["selection"]["teachback"]
        else:
            wire["unsupported_extra"] = "must remain invalid on every attempt"
        authored.append(deepcopy(wire))
        return wire

    requests = _offline_sdk(monkeypatch, payload)
    result = run_reconciliation(_source(), "nova-persistent-native-refusal-" + invalid, mode="bedrock")
    assert result["status"] == "refused"
    assert all(result[key] is None for key in ["artifact", "artifact_hash", "explanation_text", "patient_regimen"])
    assert result["receipts"] == []
    assert 0 < len(requests) <= engine.MAX_MODEL_TURNS * engine.MAX_CANDIDATES
    assert result["runtime"]["structured_output_repairs"] <= 1
    planner_attempts = [row for row in result["runtime"]["native_output_attempts"] if row["agent"] == engine.PLANNER]
    assert planner_attempts and all(row["status"] == "error" for row in planner_attempts)
    assert len(planner_attempts) == len(authored)
    assert result["runtime"]["selection"] is None
    assert result["runtime"]["planning_wire_sha256"] is None
    if invalid == "missing_teachback":
        assert all("teachback" not in wire["selection"] for wire in authored)
    else:
        assert all(wire["unsupported_extra"] == "must remain invalid on every attempt" for wire in authored)


def test_real_sdk_projection_does_not_make_invalid_source_citations_acceptable(monkeypatch):
    def payload(context, attempt):
        wire = _wire(context.dossier)
        wire["selection"]["teachback"][0]["statement_ids"] = ["foreign:L1"]
        return wire

    requests = _offline_sdk(monkeypatch, payload)
    result = run_reconciliation(_source(), "nova-private-source-refusal", mode="bedrock")
    assert result["status"] == "refused"
    assert result["artifact"] is result["explanation_text"] is result["patient_regimen"] is None
    assert result["runtime"]["failure"]["code"] == "structured_repair_exhausted"
    assert result["runtime"]["structured_output_repairs"] == 1
    assert len(result["runtime"]["planner_rejections"]) == 2
    assert [r["toolConfig"]["toolChoice"]["tool"]["name"] for r in requests] == [
        "lookup_reconciliation_references", "check_reconciliation_constraints", "PlanningWire"] * 2


def test_real_sdk_provider_stream_error_stays_withheld_without_retry_or_fallback(monkeypatch):
    requests = _offline_sdk(monkeypatch, provider_error=True)
    result = run_reconciliation(_source(), "nova-provider-error-refusal", mode="bedrock")
    assert result["status"] == "refused"
    assert result["artifact"] is result["explanation_text"] is result["patient_regimen"] is None
    assert result["runtime"]["failure"]["code"] == "external_error"
    assert result["runtime"]["structured_output_repairs"] == 0
    assert result["runtime"]["planner_rejections"] == []
    assert len(requests) == 3
    assert not any(a["agent"] == engine.PLANNER for a in result["runtime"]["native_output_attempts"])


def test_real_sdk_projection_preserves_clinical_stress_refusal_and_fresh_correction(monkeypatch):
    requests = _offline_sdk(monkeypatch)
    result = run_reconciliation(_source(), "nova-stress-gate-unchanged", mode="bedrock", stress=True)
    assert result["status"] == "verified", result
    assert [receipt["verdict"] for receipt in result["receipts"]] == ["refused", "verified"]
    assert verify_receipts(result["receipts"])
    assert result["artifact"]["patient_regimen"] is None
    assert [r["toolConfig"]["toolChoice"]["tool"]["name"] for r in requests] == [
        "lookup_reconciliation_references", "check_reconciliation_constraints", "PlanningWire"] * 2


def _scope_source():
    """Fresh synthetic policy-control source, not a benchmark dossier."""
    return {
        "profile": "medplain-reconciliation-v1", "patient_alias": "Demo-Scoped", "language": "en",
        "caregiver_concerns": "", "documents": [
            {"document_id": "instructions", "kind": "discharge", "issued_at": "2026-10-30T08:00:00Z",
             "effective_at": None,
             "text": "Taper Prednisolone: 12 mg by mouth QD on days 1-3; then 6 mg by mouth QD on days 4-6; then stop."},
            {"document_id": "support", "kind": "caregiver_note", "issued_at": "2026-10-30T08:30:00Z",
             "effective_at": None,
             "text": "Which morning should I treat as the start of this sequence?"},
        ],
    }


def _scope_wire(source, code, ids):
    return {
        "selection": {"source_hash": canonical_hash(source),
                      "questions": [{"code": code, "statement_ids": list(ids),
                                     "reason": "Clarify the caregiver's uncertainty about the schedule anchor."}],
                      "teachback": [{"code": "teachback", "statement_ids": ["instructions:L1"]}]},
        "nonask_review": [{"disposition": "covered", "intent": "confirm_taper",
                           "statement_ids": ["instructions:L1"],
                           "reason": "The existing single-source check covers the stated taper sequence."}],
    }


@pytest.mark.parametrize("choice", ["generic", "complete_scope", "floor_repeat"])
def test_sdk_authored_intent_and_scope_reach_unchanged_gate_and_frozen_scorer(monkeypatch, choice):
    """Injected SDK output tests contract preservation, not LLM improvement."""
    source = _scope_source()
    code = "caregiver_question" if choice == "generic" else "confirm_taper"
    ids = ["instructions:L1"] if choice == "floor_repeat" else ["instructions:L1", "support:L1"]
    authored = _scope_wire(source, code, ids)
    saved = deepcopy(authored)
    requests = _offline_sdk(monkeypatch, lambda context, attempt: deepcopy(authored))
    result = run_reconciliation(source, "intent-scope-contract-" + choice, mode="bedrock")
    assert result["status"] == "verified", result
    assert authored == saved
    expected_question = {"code": code, "statement_ids": ids}
    assert result["runtime"]["selection"]["questions"] == [expected_question]
    actual = result["artifact"]["proposed_questions"]
    assert [{"code": row["code"], "statement_ids": row["statement_ids"]} for row in actual] == [expected_question]
    assert result["artifact"]["structural_issues"][0]["question_code"] == "confirm_taper"
    assert result["artifact"]["structural_issues"][0]["statement_ids"] == ["instructions:L1"]
    assert result["runtime"]["structured_output_repairs"] == 0
    assert result["runtime"]["planner_rejections"] == []
    assert len(requests) == 3
    assert verify_receipts(result["receipts"])
    assert result["artifact"]["patient_regimen"] is None
    # Same targeted-code filter and matcher as the unchanged score_outcome.
    focused = [row for row in actual if row["code"] in TARGETED_CODES]
    target = [{"codes": ["confirm_taper"],
               "required_statement_ids": ["instructions:L1", "support:L1"],
               "allowed_statement_ids": ["instructions:L1", "support:L1"]}]
    focus = match_focus(focused, target)
    assert focus == {
        "true_positive": int(choice == "complete_scope"),
        "false_positive": int(choice == "floor_repeat"),
        "false_negative": int(choice != "complete_scope"),
        "predicted": int(choice != "generic"), "expected": 1,
        "exact": choice == "complete_scope",
    }
    if choice == "generic":
        assert len(actual) == 1 and actual[0]["code"] == "caregiver_question"
    assert result["runtime"]["planning_wire_schema_sha256"] == "b2a2472a7c7d282962d7c13db283580ee8fb6488a9249dd46ed4773d48df6944"


def test_sdk_duplicate_authored_questions_remain_refused_under_existing_validator(monkeypatch):
    source = _scope_source()
    authored = _scope_wire(source, "confirm_taper", ["instructions:L1"])
    authored["selection"]["questions"].append(deepcopy(authored["selection"]["questions"][0]))
    saved = deepcopy(authored)
    _offline_sdk(monkeypatch, lambda context, attempt: deepcopy(authored))
    result = run_reconciliation(source, "intent-scope-duplicate-refusal", mode="bedrock")
    assert authored == saved
    assert result["status"] == "refused", result
    assert all(result[key] is None for key in ["artifact", "artifact_hash", "explanation_text", "patient_regimen"])
    assert result["receipts"] == []
    assert result["runtime"]["structured_output_repairs"] == 1
    assert result["runtime"]["failure"]["code"] == "structured_repair_exhausted"
    assert len(result["runtime"]["planner_rejections"]) == 2
    assert all("Duplicate question" in row["reason"] for row in result["runtime"]["planner_rejections"])
