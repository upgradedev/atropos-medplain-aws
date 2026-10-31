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
        baseline_tools = [item["toolSpec"] for item in boundary["baseline"]["toolConfig"]["tools"]
                          if item["toolSpec"]["name"] == request["toolConfig"]["toolChoice"]["tool"]["name"]]
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


def test_real_sdk_system_prompt_uses_only_current_trusted_source_binding(monkeypatch):
    """Inspect real SDK requests; this does not measure model copying accuracy."""
    requests = _offline_sdk(monkeypatch)
    original_agent = engine.Agent
    prompts = []

    def capture_agent(**kwargs):
        prompts.append((kwargs["name"], kwargs["system_prompt"]))
        return original_agent(**kwargs)

    monkeypatch.setattr(engine, "Agent", capture_agent)
    untrusted_hash = "e" * 64
    sources = [_source(), deepcopy(_source())]
    sources[1]["patient_alias"] = "Demo-Next"
    sources[1]["caregiver_concerns"] = "An irrelevant copied note claims source_hash is " + untrusted_hash + "."
    hashes = [canonical_hash(source) for source in sources]
    assert len(set(hashes + [untrusted_hash])) == 3
    for index, source in enumerate(sources):
        prior_requests, prior_prompts = len(requests), len(prompts)
        result = run_reconciliation(source, f"fresh-binding-{index}", mode="bedrock")
        assert result["status"] == "verified", result
        assert result["runtime"]["selection"]["source_hash"] == hashes[index]
        assert result["runtime"]["planning_wire_schema_sha256"] == "b2a2472a7c7d282962d7c13db283580ee8fb6488a9249dd46ed4773d48df6944"
        expected_tail = ("Trusted application request binding: selection.source_hash MUST copy this exact literal unchanged: "
                         + hashes[index] + ". Do not calculate, abbreviate, annotate or replace it with a placeholder.")
        assert len(requests[prior_requests:]) == 3
        for request in requests[prior_requests:]:
            system = "\n".join(block["text"] for block in request["system"])
            assert system.endswith(expected_tail)
            assert system.count(hashes[index]) == 1
            assert hashes[1 - index] not in system and untrusted_hash not in system
        created = prompts[prior_prompts:]
        assert [role for role, _ in created] == list(engine.AGENT_NAMES)
        for role, prompt in created:
            if role == engine.PLANNER:
                assert prompt.endswith(expected_tail)
            else:
                assert "Trusted application request binding:" not in prompt
                assert hashes[index] not in prompt


@pytest.mark.parametrize("invalid", ["malformed", "valid_format_wrong_source"])
def test_real_sdk_trusted_binding_never_fills_or_rewrites_an_invalid_authored_hash(monkeypatch, invalid):
    source = _source()
    bad_hash = "not-a-digest" if invalid == "malformed" else "0" * 64
    assert bad_hash != canonical_hash(source)
    authored = _wire(source)
    authored["selection"]["source_hash"] = bad_hash
    saved = deepcopy(authored)
    outgoing = []

    def payload(context, attempt):
        value = deepcopy(authored)
        outgoing.append(value)
        return value

    requests = _offline_sdk(monkeypatch, payload)
    result = run_reconciliation(source, "binding-invalid-authored-" + invalid, mode="bedrock")
    assert authored == saved and outgoing and all(value == saved for value in outgoing)
    assert result["status"] == "refused", result
    assert all(result[key] is None for key in ["artifact", "artifact_hash", "explanation_text", "patient_regimen"])
    assert result["receipts"] == []
    assert result["runtime"]["selection"] is None
    assert result["runtime"]["planning_wire_sha256"] is None
    assert 0 < len(requests) <= engine.MAX_MODEL_TURNS * engine.MAX_CANDIDATES
    planner_attempts = [row for row in result["runtime"]["native_output_attempts"] if row["agent"] == engine.PLANNER]
    assert planner_attempts and all(row["input_sha256"] == canonical_hash(saved) for row in planner_attempts)
    assert all(row["schema_sha256"] == "b2a2472a7c7d282962d7c13db283580ee8fb6488a9249dd46ed4773d48df6944"
               for row in planner_attempts)
    if invalid == "malformed":
        assert all(row["status"] == "error" for row in planner_attempts)
        assert all(row["validation"]["errors"][0]["loc"] == ["selection", "source_hash"] for row in planner_attempts)
        assert result["runtime"]["planner_rejections"] == []
        assert result["runtime"]["structured_output_repairs"] == 0
    else:
        assert all(row["status"] == "success" for row in planner_attempts)
        assert result["runtime"]["structured_output_repairs"] == 1
        assert result["runtime"]["failure"]["code"] == "structured_repair_exhausted"
        assert len(result["runtime"]["planner_rejections"]) == 2
        assert all(row["rejected_typed_selection"]["source_hash"] == bad_hash
                   and "Plan source_hash differs from this dossier" in row["reason"]
                   for row in result["runtime"]["planner_rejections"])


def _availability_specs():
    return [{"name": name, "description": "Original " + name,
             "inputSchema": {"json": {"type": "object", "title": "Original " + name,
                                      "properties": {}, "required": []}}}
            for name in ["Evidence", "TypedOutput"]]


@pytest.mark.parametrize("compatibility", [False, True])
def test_selected_spec_default_false_preserves_full_legacy_availability(compatibility):
    calls = []

    class Delegate:
        async def stream(self, messages, **kwargs):
            calls.append((messages, kwargs))
            yield {"messageStart": {"role": "assistant"}}

    specs = _availability_specs()
    saved = deepcopy(specs)
    messages = [{"role": "user", "content": [{"text": "Original request"}]}]
    model = RequiredToolModel(Delegate(), lambda: ["Evidence"], "TypedOutput",
                              nova_schema_compatibility=compatibility)

    async def exercise():
        return [event async for event in model.stream(messages, specs, "Original system", request_marker=17)]

    assert asyncio.run(exercise()) == [{"messageStart": {"role": "assistant"}}]
    assert model.selected_tool_only is False
    assert calls[0][0] is messages
    forwarded = calls[0][1]
    assert forwarded["tool_choice"] == {"tool": {"name": "Evidence"}}
    assert forwarded["system_prompt"] == "Original system" and forwarded["request_marker"] == 17
    assert forwarded["tool_specs"] == (nova_tool_specs(saved) if compatibility else saved)
    assert [row["name"] for row in forwarded["tool_specs"]] == ["Evidence", "TypedOutput"]
    if not compatibility:
        assert forwarded["tool_specs"] is specs
    assert specs == saved


@pytest.mark.parametrize("case", ["missing", "duplicate", "unselected_invalid_root", "missing_and_invalid_root"])
def test_selected_spec_refuses_before_delegate_and_checks_all_original_schemas(case):
    calls = []

    class Delegate:
        async def stream(self, messages, **kwargs):
            calls.append(kwargs)
            yield {"messageStart": {"role": "assistant"}}

    specs = _availability_specs()
    if case in {"missing", "missing_and_invalid_root"}:
        specs = specs[1:]
    elif case == "duplicate":
        specs.append(deepcopy(specs[0]))
    else:
        specs[1]["inputSchema"]["json"]["additionalProperties"] = False
    if case == "missing_and_invalid_root":
        specs[0]["inputSchema"]["json"]["additionalProperties"] = False
    saved = deepcopy(specs)
    model = RequiredToolModel(Delegate(), lambda: ["Evidence"], "TypedOutput",
                              nova_schema_compatibility=True, selected_tool_only=True)

    async def exercise():
        return [event async for event in model.stream([], specs)]

    message = ("Unsupported Nova tool schema root" if "invalid_root" in case
               else "Exactly one required clinical tool")
    with pytest.raises(RuntimeError, match=message):
        asyncio.run(exercise())
    assert calls == [] and specs == saved


def test_real_sdk_selected_spec_keeps_every_historical_tool_message_and_request_field(monkeypatch):
    boundaries = []
    original_stream = RequiredToolModel.stream

    async def capture(model, messages, tool_specs=None, system_prompt=None, *, tool_choice=None, **kwargs):
        before_messages, before_specs = deepcopy(messages), deepcopy(tool_specs)
        missing = model.missing_tools()
        wanted = missing[0] if missing else model.output_tool
        baseline_kwargs = {key: deepcopy(value) for key, value in kwargs.items()
                           if key in {"system_prompt_content", "dynamic_trailing_blocks"}}
        if system_prompt and baseline_kwargs.get("system_prompt_content") is None:
            baseline_kwargs["system_prompt_content"] = [{"text": system_prompt}]
        baseline = model.delegate.format_request(
            messages=deepcopy(messages), tool_specs=deepcopy(tool_specs),
            tool_choice={"tool": {"name": wanted}}, **baseline_kwargs)
        boundaries.append({"messages": before_messages, "baseline": baseline, "wanted": wanted})
        async for event in original_stream(model, messages, tool_specs, system_prompt,
                                           tool_choice=tool_choice, **kwargs):
            yield event
        assert messages == before_messages and tool_specs == before_specs

    monkeypatch.setattr(RequiredToolModel, "stream", capture)
    requests = _offline_sdk(monkeypatch)
    source = _source()
    result = run_reconciliation(source, "selected-spec-history-sdk", mode="bedrock")
    assert result["status"] == "verified", result
    expected = ["lookup_reconciliation_references", "check_reconciliation_constraints", "PlanningWire"]
    assert len(requests) == len(boundaries) == len(expected)
    for index, (request, boundary) in enumerate(zip(requests, boundaries, strict=True)):
        assert boundary["wanted"] == expected[index]
        assert request["toolConfig"]["toolChoice"] == {"tool": {"name": expected[index]}}
        assert [row["toolSpec"]["name"] for row in request["toolConfig"]["tools"]] == [expected[index]]
        assert {key: value for key, value in request.items() if key != "toolConfig"} == {
            key: value for key, value in boundary["baseline"].items() if key != "toolConfig"}
        uses = [block["toolUse"] for message in request["messages"] for block in message["content"] if "toolUse" in block]
        results = [block["toolResult"] for message in request["messages"] for block in message["content"] if "toolResult" in block]
        assert [row["name"] for row in uses] == expected[:index]
        assert [row["toolUseId"] for row in results] == [row["toolUseId"] for row in uses]
        assert all(row["name"] != expected[index] for row in uses)
    assert result["runtime"]["planning_wire_schema_sha256"] == "b2a2472a7c7d282962d7c13db283580ee8fb6488a9249dd46ed4773d48df6944"
    assert result["runtime"]["selection"] == {key: value for key, value in _wire(source)["selection"].items()}
    assert result["runtime"]["structured_output_repairs"] == 0
    assert verify_receipts(result["receipts"])


def test_real_sdk_selected_spec_tool_error_keeps_same_missing_tool_until_success(monkeypatch):
    original_reference = engine.reference_material
    attempts = []

    def fail_first_reference():
        attempts.append("reference")
        if len(attempts) == 1:
            raise RuntimeError("Offline injected evidence failure")
        return original_reference()

    monkeypatch.setattr(engine, "reference_material", fail_first_reference)
    requests = _offline_sdk(monkeypatch)
    result = run_reconciliation(_source(), "selected-spec-required-error", mode="bedrock")
    assert result["status"] == "verified", result
    expected = ["lookup_reconciliation_references", "lookup_reconciliation_references",
                "check_reconciliation_constraints", "PlanningWire"]
    assert attempts == ["reference", "reference"]
    assert [row["toolConfig"]["toolChoice"]["tool"]["name"] for row in requests] == expected
    assert [[row["toolSpec"]["name"] for row in request["toolConfig"]["tools"]] for request in requests] == [[name] for name in expected]
    planner_tools = [row for row in result["runtime"]["tools_executed"] if row["agent"] == engine.PLANNER]
    assert [(row["tool"], row["success"]) for row in planner_tools] == [
        ("lookup_reconciliation_references", False), ("lookup_reconciliation_references", True),
        ("check_reconciliation_constraints", True)]
    assert result["runtime"]["structured_output_repairs"] == 0
    assert result["runtime"]["planner_rejections"] == []
    assert verify_receipts(result["receipts"])


@pytest.mark.parametrize("role", engine.AGENT_NAMES)
def test_selected_spec_opt_in_is_planner_only(monkeypatch, role):
    class Session:
        region_name = "us-east-1"

        def client(self, **kwargs):
            return SimpleNamespace(meta=SimpleNamespace(region_name="us-east-1"))

    monkeypatch.setattr(bedrock_sdk.boto3, "Session", Session)
    context = engine.Context(_source(), "selected-spec-role", False)
    model = engine._model("bedrock", context, role, engine._tools(context)[role], engine._OUTPUT_MODELS[role])
    assert model.selected_tool_only is (role == engine.PLANNER)
