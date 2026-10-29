"""Tool calls must retain the actual SDK invocation that executed them."""

from collections import defaultdict

from medplain import engine
from medplain.fixtures import get_packets

REQUIRED = {
    engine.PARSER: {"lab_reference_range_evaluator"},
    engine.VERIFIER: {"medication_dosage_frequency_parity_checker", "drug_interaction_checker"},
    engine.EXPLAINER: {"grounded_explanation_builder"},
}


def grouped_calls(result):
    runtime = result["runtime"]
    groups = defaultdict(set)
    assert len(runtime["nodes_executed"]) == runtime["hook_invocations"]
    for call in runtime["tools_executed"]:
        index = call["invocation_index"]
        assert type(index) is int and 1 <= index <= runtime["hook_invocations"]
        assert call["agent"] == runtime["nodes_executed"][index - 1]
        if call["success"]:
            groups[index].add(call["tool"])
    return groups


def test_actual_sdk_stress_has_fresh_required_tools_for_each_invocation():
    packet = next(packet for packet in get_packets() if packet.adversarial)
    result = engine.run_packet(packet, "trajectory-stress", stress=True)
    assert result["status"] == "verified", result
    assert result["runtime"]["nodes_executed"] == [
        engine.PARSER, engine.VERIFIER, engine.EXPLAINER, engine.VERIFIER, engine.EXPLAINER,
    ]
    groups = grouped_calls(result)
    for index, role in enumerate(result["runtime"]["nodes_executed"], start=1):
        assert groups[index] == REQUIRED[role]
    assert groups[2] == groups[4] == REQUIRED[engine.VERIFIER]


def test_actual_sdk_structured_repair_does_not_reuse_prior_tool_provenance(monkeypatch):
    factory = engine._model

    def one_wrong_identity(mode, context, role, tools, output_model):
        model = factory(mode, context, role, tools, output_model)
        payload = model._payload
        if role == engine.VERIFIER:
            model._payload = lambda: {**payload(), "packet_id": "wrong-id"} if model._invocation == 1 else payload()
        return model

    monkeypatch.setattr(engine, "_model", one_wrong_identity)
    result = engine.run_packet(get_packets()[0], "trajectory-repair")
    assert result["status"] == "verified", result
    assert result["runtime"]["structured_output_repairs"] == 1
    assert result["runtime"]["nodes_executed"] == [
        engine.PARSER, engine.VERIFIER, engine.VERIFIER, engine.EXPLAINER,
    ]
    groups = grouped_calls(result)
    assert groups[2] == groups[3] == REQUIRED[engine.VERIFIER]
    for index, role in enumerate(result["runtime"]["nodes_executed"], start=1):
        assert groups[index] == REQUIRED[role]
