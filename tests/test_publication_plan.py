"""Fresh publication handles cannot bypass the unchanged full clinical gate."""

import copy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from medplain import engine
from medplain.clinical import build_explanation
from medplain.fixtures import get_packets
from medplain.gate import ReceiptChain, canonical_hash
from medplain.hooks import ClinicalSafetyGateHook
from medplain.publication import resolve_publication
from medplain.schemas import PatientExplanation, PatientPublicationPlan


class State:
    def __init__(self):
        self.rows = {}

    def set(self, name, value):
        self.rows[name] = copy.deepcopy(value)

    def get(self):
        return copy.deepcopy(self.rows)


def prepared(stress=False, packet=None, execution="case:1"):
    packet = packet or get_packets()[0]
    context = engine.RequestContext(packet, execution, stress, ReceiptChain(execution, packet))
    context.parsed = packet
    context.expected_results[engine.VERIFIER] = {"verified": True}
    context.hook_invocations = 2
    agent = SimpleNamespace(state=State(), model=None)
    hook = ClinicalSafetyGateHook(context, engine.EXPLAINER, ["grounded_explanation_builder"])
    hook.before_invocation(SimpleNamespace(agent=agent))
    builder = engine._tools(context)[engine.EXPLAINER][0]
    return context, agent, hook, builder


def successful_builder(hook, builder):
    plan = builder()
    hook.after_tool_call(SimpleNamespace(tool_use={"name": "grounded_explanation_builder"},
                                         result={"status": "success"}))
    return PatientPublicationPlan.model_validate(plan)


def invoke(hook, agent, plan):
    return hook.after_invocation(SimpleNamespace(agent=agent, result=SimpleNamespace(structured_output=plan)))


@pytest.mark.parametrize("field,value", [
    ("artifact_ref", "draft-" + "0" * 24), ("packet_id", "other-packet"),
])
def test_wrong_plan_never_resolves_current_tool_artifact(field, value):
    context, agent, hook, builder = prepared()
    plan = successful_builder(hook, builder).model_copy(update={field: value})
    with pytest.raises(RuntimeError, match="fresh builder reference"):
        invoke(hook, agent, plan)
    assert context.attempts == 0 and context.candidate is None and context.chain.receipts == []


def test_plan_forbids_clinical_fields_free_advice_and_unknown_presentation():
    context, _, hook, builder = prepared()
    plan = successful_builder(hook, builder).model_dump()
    for field, value in [("dose", "999"), ("explanation", {"advice": "Take more"}),
                         ("advice", "unsupported"), ("presentation", "unchecked-view")]:
        with pytest.raises(ValidationError):
            PatientPublicationPlan.model_validate({**plan, field: value})
    assert context.candidate is None


def test_old_clinical_candidate_type_is_refused_instead_of_replaced():
    context, agent, hook, builder = prepared()
    successful_builder(hook, builder)
    old_candidate = build_explanation(context.packet)
    old_candidate.medications[0].dose = "999"
    with pytest.raises(RuntimeError, match="publication plan"):
        invoke(hook, agent, old_candidate)
    assert context.candidate is None and context.attempts == 0


def test_publication_reference_is_bound_to_request_generation_packet_and_invocation():
    first, _, hook, builder = prepared()
    plan = successful_builder(hook, builder)
    for changed in [
        {"execution_id": "case:2"}, {"request_nonce": "different-request"},
        {"invocation_index": first.hook_invocations + 1}, {"packet": get_packets()[1]},
    ]:
        arguments = {
            "snapshot": first.publication_snapshot, "snapshot_hash": first.publication_snapshot_hash,
            "saved_binding": first.publication_binding, "plan": plan, "packet": first.packet,
            "execution_id": first.session_id, "request_nonce": first.request_nonce,
            "invocation_index": first.hook_invocations, "artifact_nonce": first.publication_artifact_nonce,
            **changed,
        }
        with pytest.raises(ValueError, match="source, request and invocation"):
            resolve_publication(**arguments)


def test_invocation_reset_and_later_successful_builder_reject_previous_reference():
    context, agent, hook, builder = prepared()
    previous = successful_builder(hook, builder)
    hook.before_invocation(SimpleNamespace(agent=agent))
    assert context.publication_snapshot is None and context.publication_binding is None
    current = successful_builder(hook, builder)
    assert current.artifact_ref != previous.artifact_ref
    with pytest.raises(RuntimeError):
        invoke(hook, agent, previous)
    assert context.attempts == 0
    invoke(hook, agent, current)
    assert context.verdict.passed and context.validated_publication_plan == current.model_dump()


def test_missing_fresh_tool_call_cannot_publish_old_plan():
    context, agent, hook, builder = prepared()
    previous = successful_builder(hook, builder)
    hook.before_invocation(SimpleNamespace(agent=agent))
    hook.required_tool_repairs = 1
    with pytest.raises(RuntimeError, match="deterministic tools"):
        invoke(hook, agent, previous)
    assert context.attempts == 0 and context.candidate is None


def test_repeated_builder_mints_distinct_last_reference_and_failure_clears_it(monkeypatch):
    context, agent, hook, builder = prepared()
    first = successful_builder(hook, builder)
    second = successful_builder(hook, builder)
    assert first.artifact_ref != second.artifact_ref
    with pytest.raises(RuntimeError):
        invoke(hook, agent, first)

    def failed_builder(_):
        raise RuntimeError("intentional builder failure")

    monkeypatch.setattr(engine, "build_explanation", failed_builder)
    with pytest.raises(RuntimeError, match="intentional builder failure"):
        builder()
    assert context.publication_snapshot is None
    with pytest.raises(RuntimeError):
        invoke(hook, agent, second)
    assert context.candidate is None


def test_snapshot_tamper_and_resolved_mutation_cannot_change_private_snapshot():
    context, agent, hook, builder = prepared()
    plan = successful_builder(hook, builder)
    original_snapshot = context.publication_snapshot
    snapshot = PatientExplanation.model_validate_json(original_snapshot)
    snapshot.medications[0].dose = "999"
    context.publication_snapshot = snapshot.model_dump_json()
    with pytest.raises(ValueError, match="immutable"):
        invoke(hook, agent, plan)
    context.publication_snapshot = original_snapshot
    invoke(hook, agent, plan)
    context.candidate.medications[0].dose = "999"
    assert PatientExplanation.model_validate_json(context.publication_snapshot).medications[0].dose != "999"


def test_stress_receipt_binds_post_injection_artifact_then_fresh_plan_corrects():
    context, agent, hook, builder = prepared(stress=True)
    first = successful_builder(hook, builder)
    private_hash = context.publication_snapshot_hash
    original = context.publication_snapshot
    invoke(hook, agent, first)
    assert not context.verdict.passed and context.attempts == 1
    assert context.chain.receipts[0]["artifact_hash"] != private_hash
    assert context.publication_snapshot == original
    hook.before_invocation(SimpleNamespace(agent=agent))
    second = successful_builder(hook, builder)
    assert second.artifact_ref != first.artifact_ref
    invoke(hook, agent, second)
    assert context.verdict.passed and context.attempts == 2
    assert [receipt["verdict"] for receipt in context.chain.receipts] == ["refused", "verified"]
    assert context.chain.receipts[-1]["artifact_hash"] == context.publication_snapshot_hash
    assert context.validated_publication_plan == second.model_dump()


@pytest.mark.parametrize("mutation", ["wrong_ref", "clinical_fields"])
def test_actual_sdk_wrong_plan_and_forbidden_old_candidate_fields_refuse(monkeypatch, mutation):
    original = engine._model

    def altered_plan(mode, context, role, tools, output_model):
        model = original(mode, context, role, tools, output_model)
        if role == engine.EXPLAINER:
            payload = model._payload
            if mutation == "wrong_ref":
                model._payload = lambda: {**payload(), "artifact_ref": "draft-" + "0" * 24}
            else:
                model._payload = lambda: {**payload(), "patient_text": "Take an unsupported dose"}
        return model

    monkeypatch.setattr(engine, "_model", altered_plan)
    result = engine.run_packet(get_packets()[0], "bad-plan")
    assert result["status"] == "refused", result
    assert result["explanation"] is None and result["receipts"] == []
    assert result["runtime"]["publication_plan"] is None


def test_actual_sdk_success_retains_validated_plan_and_full_patient_artifact():
    result = engine.run_packet(get_packets()[0], "plan-success")
    assert result["status"] == "verified", result
    assert result["explanation"] == build_explanation(get_packets()[0]).model_dump()
    plan = result["runtime"]["publication_plan"]
    assert set(plan) == {"packet_id", "artifact_ref", "presentation"}
    assert result["runtime"]["publication_plan_sha256"] == canonical_hash(plan)
