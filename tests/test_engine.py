"""Integration tests use the real Strands Agents, tools, hooks and Graph offline."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from medplain import engine
from medplain.clinical import build_explanation
from medplain.engine import AGENT_NAMES, EXPLAINER, PARSER, VERIFIER, RequestContext, run_packet
from medplain.fixture_model import FixtureModel
from medplain.fixtures import get_packets
from medplain.gate import ReceiptChain, verify_receipts
from medplain.hooks import MAX_CANDIDATES, MAX_MODEL_TURNS_PER_NODE, ClinicalSafetyGateHook
from medplain.schemas import PatientExplanation


@pytest.mark.parametrize("packet", get_packets(), ids=lambda packet: packet.packet_id)
def test_real_strands_graph_executes_every_agent_and_tool(packet):
    result = run_packet(packet, f"test-{packet.packet_id}")
    assert result["status"] == "verified", result
    assert result["explanation"] == build_explanation(packet).model_dump(mode="json")
    assert result["runtime"]["nodes_executed"] == list(AGENT_NAMES)
    assert result["runtime"]["orchestrator"] == "strands.multiagent.Graph"
    assert result["runtime"]["sdk_version"] == "1.53.0"
    assert result["runtime"]["hook_invocations"] == 3
    assert {item["tool"] for item in result["runtime"]["tools_executed"]} == {
        "lab_reference_range_evaluator",
        "medication_dosage_frequency_parity_checker",
        "drug_interaction_checker",
        "grounded_explanation_builder",
    }
    assert all(item["success"] for item in result["runtime"]["tools_executed"])
    assert verify_receipts(result["receipts"])
    assert result["correction_count"] == 0
    assert result["runtime"]["session_state"]["session_id"] == f"test-{packet.packet_id}"


def test_adversarial_dose_is_withheld_then_corrected_by_actual_graph_cycle():
    packet = next(packet for packet in get_packets() if packet.adversarial)
    result = run_packet(packet, "stress-session", stress=True)
    assert result["status"] == "verified", result
    assert result["runtime"]["nodes_executed"] == [PARSER, VERIFIER, EXPLAINER, VERIFIER, EXPLAINER]
    assert result["correction_count"] == 1
    assert [receipt["verdict"] for receipt in result["receipts"]] == ["refused", "verified"]
    assert result["receipts"][0]["discrepancies"]
    assert any(" dose:" in error for error in result["receipts"][0]["discrepancies"])
    assert any(" text:" in error for error in result["receipts"][0]["discrepancies"])
    assert result["receipts"][1]["previous_hash"] == result["receipts"][0]["hash"]
    assert result["receipts"][0]["artifact_hash"] != result["receipts"][1]["artifact_hash"]
    assert result["explanation"] == build_explanation(packet).model_dump(mode="json")
    assert any(event["status"] == "handoff" for event in result["trace"])
    assert any(event["agent"] == "AdversarialStressInjector" for event in result["trace"])
    assert verify_receipts(result["receipts"])
    assert "candidate" not in result


def test_repeated_discrepancy_ends_refused_without_publishing(monkeypatch):
    packet = get_packets()[0]
    original = build_explanation(packet).model_dump(mode="json")
    broken = deepcopy(original)
    broken["medications"][0]["dose"] = "9999"
    monkeypatch.setattr(engine, "build_explanation", lambda _: PatientExplanation.model_validate(broken))
    result = run_packet(packet, "unfixable")
    assert result["status"] == "refused", result
    assert result["explanation"] is None
    assert result["explanation_text"] is None
    assert len(result["receipts"]) == MAX_CANDIDATES
    assert all(receipt["verdict"] == "refused" for receipt in result["receipts"])
    assert result["runtime"]["nodes_executed"] == [PARSER, VERIFIER, EXPLAINER, VERIFIER, EXPLAINER]
    assert verify_receipts(result["receipts"])


def test_inconsistent_source_fails_before_patient_artifact():
    packet = get_packets()[0]
    packet.medications[0].dose = "9999"
    result = run_packet(packet, "inconsistent")
    assert result["status"] == "refused"
    assert result["explanation"] is None
    assert result["receipts"] == []
    assert result["runtime"]["nodes_executed"] == [PARSER]


def test_requests_do_not_share_mutation_or_session_state():
    packet = get_packets()[0]
    first = run_packet(packet, "patient-a", stress=True)
    second = run_packet(packet, "patient-b")
    assert first["correction_count"] == 1
    assert second["correction_count"] == 0
    assert second["runtime"]["session_state"]["session_id"] == "patient-b"
    assert first["receipts"][0]["session_id"] == "patient-a"
    assert second["receipts"][0]["session_id"] == "patient-b"
    assert get_packets()[0].medications[0].dose == packet.medications[0].dose


def test_bedrock_failure_never_falls_back_to_fixture(monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("credential details must not reach the anonymous response")

    monkeypatch.setattr(engine, "BedrockModel", unavailable)
    result = run_packet(get_packets()[0], "unavailable", model_mode="bedrock")
    assert result["status"] == "refused"
    assert result["model_mode"] == "bedrock"
    assert result["explanation"] is None
    assert result["receipts"] == []
    assert "credential details" not in str(result)


def test_bedrock_configuration_is_explicit_without_network(monkeypatch):
    captured = {}
    monkeypatch.setattr(engine, "BedrockModel", lambda **kwargs: captured.update(kwargs) or "bedrock")
    monkeypatch.delenv("MEDPLAIN_BEDROCK_MODEL_ID", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    packet = get_packets()[0]
    context = RequestContext(packet, "config", False, ReceiptChain("config", packet))
    assert engine._model("bedrock", context, PARSER, [], PatientExplanation) == "bedrock"
    assert captured["model_id"] == "amazon.nova-micro-v1:0"
    assert captured["region_name"] == "us-east-1"
    assert captured["boto_client_config"].retries["total_max_attempts"] == 1
    assert captured["boto_client_config"].read_timeout == 45


def test_unknown_model_mode_is_rejected():
    with pytest.raises(ValueError, match="model_mode"):
        run_packet(get_packets()[0], "unknown", model_mode="fallback")


def test_noncompleted_graph_cannot_publish_even_a_verified_candidate(monkeypatch):
    packet = get_packets()[0]

    def failed_graph(context, model_mode):
        context.candidate = build_explanation(packet)
        context.verdict = SimpleNamespace(passed=True)
        return lambda *args, **kwargs: SimpleNamespace(status=SimpleNamespace(value="failed"), failed_nodes=0)

    monkeypatch.setattr(engine, "_make_graph", failed_graph)
    result = run_packet(packet, "graph-failed")
    assert result["status"] == "refused"
    assert result["explanation"] is None


def test_model_turn_budget_fails_closed():
    packet = get_packets()[0]
    context = RequestContext(packet, "budget", False, ReceiptChain("budget", packet))
    hook = ClinicalSafetyGateHook(context, PARSER, [])
    for _ in range(MAX_MODEL_TURNS_PER_NODE):
        hook.before_model_call(SimpleNamespace())
    with pytest.raises(RuntimeError, match="budget"):
        hook.before_model_call(SimpleNamespace())


def test_missing_required_tool_and_missing_structured_output_fail_closed():
    packet = get_packets()[0]
    context = RequestContext(packet, "tool-required", False, ReceiptChain("tool-required", packet))
    hook = ClinicalSafetyGateHook(context, PARSER, ["lab_reference_range_evaluator"])
    hook.required_tool_repairs = 1
    with pytest.raises(RuntimeError, match="deterministic tools"):
        hook.after_invocation(SimpleNamespace(result=SimpleNamespace(structured_output=None)))
    hook.successful_tools.add("lab_reference_range_evaluator")
    with pytest.raises(RuntimeError, match="structured output"):
        hook.after_invocation(SimpleNamespace(result=SimpleNamespace(structured_output=None)))


def test_parser_model_cannot_replace_deterministic_tool_evidence():
    packet = get_packets()[0]
    context = RequestContext(packet, "mismatch", False, ReceiptChain("mismatch", packet))
    hook = ClinicalSafetyGateHook(context, PARSER, [])
    output = engine.ParsedClinicalRecord(packet_id="wrong", source_verified=True, labs=[])
    with pytest.raises(RuntimeError, match="tool evidence"):
        hook.after_invocation(SimpleNamespace(result=SimpleNamespace(structured_output=output)))


def test_verifier_tools_include_authoritative_packet_id():
    packet = get_packets()[0]
    context = RequestContext(packet, "tool-identities", False, ReceiptChain("tool-identities", packet))
    tools = engine._tools(context)
    tools[PARSER][0]()
    for deterministic_tool in tools[VERIFIER]:
        assert deterministic_tool()["packet_id"] == packet.packet_id


def test_nested_verification_schema_requires_complete_clinical_evidence():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        engine.MedicationVerification(
            packet_id="packet", medication_discrepancies=[], interactions=[{"title": "Incomplete"}],
        )
    with pytest.raises(ValidationError):
        engine.ParsedClinicalRecord(packet_id="packet", source_verified=True, labs=[{"id": "lab"}])


def test_real_strands_hook_repairs_structured_verifier_output_once(monkeypatch):
    original_model = engine._model

    def first_bad_id(mode, context, role, tools, output_model):
        model = original_model(mode, context, role, tools, output_model)
        payload = model._payload
        if role == VERIFIER:
            model._payload = lambda: {**payload(), "packet_id": "wrong-id"} if model._invocation == 1 else payload()
        return model

    monkeypatch.setattr(engine, "_model", first_bad_id)
    result = run_packet(get_packets()[0], "structured-repair")
    assert result["status"] == "verified", result
    assert result["runtime"]["structured_output_repairs"] == 1
    assert result["runtime"]["nodes_executed"] == [PARSER, VERIFIER, VERIFIER, EXPLAINER]
    assert any(event["status"] == "repair" and "packet_id" in event["message"] for event in result["trace"])
    assert len([tool for tool in result["runtime"]["tools_executed"] if tool["agent"] == VERIFIER]) == 4


def test_repeated_structured_mismatch_fails_closed_after_one_repair(monkeypatch):
    original_model = engine._model

    def always_bad_id(mode, context, role, tools, output_model):
        model = original_model(mode, context, role, tools, output_model)
        payload = model._payload
        if role == VERIFIER:
            model._payload = lambda: {**payload(), "packet_id": "wrong-id"}
        return model

    monkeypatch.setattr(engine, "_model", always_bad_id)
    result = run_packet(get_packets()[0], "structured-refusal")
    assert result["status"] == "refused", result
    assert result["explanation"] is None
    assert result["runtime"]["structured_output_repairs"] == 1
    assert result["runtime"]["nodes_executed"] == [PARSER, VERIFIER, VERIFIER]
    assert result["receipts"] == []


def test_actual_stress_correction_requires_fresh_verifier_tools(monkeypatch):
    original_model = engine._model

    def skip_on_correction(mode, context, role, tools, output_model):
        model = original_model(mode, context, role, tools, output_model)
        if role != VERIFIER:
            return model
        original_stream = model.stream
        original_payload = model._payload

        def payload():
            if role not in context.expected_results:
                return {
                    "packet_id": context.packet.packet_id,
                    "medication_discrepancies": [],
                    "interactions": engine.check_interactions(context.packet),
                }
            return original_payload()

        async def stream(*args, **kwargs):
            # Simulate a live model returning a prior structured result on the
            # correction revisit without fresh tool calls in this invocation.
            if model._invocation == 2 and model._turn == 0:
                model._turn = 1
            async for event in original_stream(*args, **kwargs):
                yield event

        model._payload = payload
        model.stream = stream
        return model

    monkeypatch.setattr(engine, "_model", skip_on_correction)
    packet = next(packet for packet in get_packets() if packet.adversarial)
    result = run_packet(packet, "fresh-correction-tools", stress=True)
    assert result["status"] == "verified", result
    assert result["correction_count"] == 1
    assert result["runtime"]["tool_call_repairs"] == 1
    assert result["runtime"]["nodes_executed"] == [PARSER, VERIFIER, EXPLAINER, VERIFIER, VERIFIER, EXPLAINER]
    assert [receipt["verdict"] for receipt in result["receipts"]] == ["refused", "verified"]
    assert len([tool for tool in result["runtime"]["tools_executed"] if tool["agent"] == VERIFIER]) == 4


def test_missing_tool_repair_budget_is_separate_and_fails_closed():
    packet = get_packets()[0]
    context = RequestContext(packet, "missing-tools", False, ReceiptChain("missing-tools", packet))
    hook = ClinicalSafetyGateHook(context, VERIFIER, ["medication_dosage_frequency_parity_checker"])
    first = SimpleNamespace(result=SimpleNamespace(structured_output=None), resume=None)
    hook.after_invocation(first)
    assert "Missing fresh calls" in first.resume
    assert context.tool_call_repairs == 1
    assert hook.structured_output_repairs == 0
    with pytest.raises(RuntimeError, match="deterministic tools"):
        hook.after_invocation(SimpleNamespace(result=SimpleNamespace(structured_output=None), resume=None))


def test_fixture_adapter_is_bounded_and_pydantic_validated():
    expected = build_explanation(get_packets()[0]).model_dump(mode="json")
    model = FixtureModel([], PatientExplanation, lambda: expected)

    async def exercise():
        result = [event async for event in model.structured_output(PatientExplanation, [])]
        assert isinstance(result[-1]["output"], PatientExplanation)
        model.update_config(context_window_limit=12345)
        assert model.get_config()["context_window_limit"] == 12345
        model.begin_invocation()
        await anext(model.stream([], []))
        with pytest.raises(RuntimeError, match="registered"):
            await anext(model.stream([], []))
        with pytest.raises(RuntimeError, match="bounded"):
            await anext(model.stream([], []))

    asyncio.run(exercise())
