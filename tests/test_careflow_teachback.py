"""Actual SDK fixture assessments are provisional and cannot publish instructions."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from medplain import careflow_teachback as teachback
from medplain.fixture_model import FixtureModel
from medplain.gate import canonical_hash
from medplain.reconciliation import deterministic_baseline, examples


@pytest.fixture
def released():
    source = copy.deepcopy(examples()[0]["dossier"])
    # A valid release-shaped source artifact for an isolated SDK test; service
    # release authorization remains a separate domain boundary.
    result = deterministic_baseline(source)
    return source, result, teachback.make_teachback_prompt(result)


def assess(released, response="Yes, I understand", execution="case:teachback:attempt1", **kwargs):
    source, result, prompt = released
    return teachback.run_teachback_assessment(source, result, prompt, response, execution,
                                            mode="fixture", evidence_as_of="2026-10-30", **kwargs)


def test_actual_sdk_uses_real_response_fresh_tools_retrieved_evidence_and_provisional_native_plan(released):
    source, artifact, prompt = released
    response = "Yes, I understand"
    result = assess(released, response)
    assert result["status"] == "provisional", result
    assert result["requires_reviewer"] is True and result["patient_regimen"] is None
    assert result["assessment"]["covered_statement_ids"] == []
    assert result["assessment"]["missing_statement_ids"] == prompt["statement_ids"]
    assert result["follow_up"]["code"] == "repeat_in_own_words"
    assert result["response_sha256"] == hashlib.sha256(response.encode("utf-8")).hexdigest()
    assert result["runtime"]["model_mode"] == "fixture"
    assert result["runtime"]["model_adapter_id"] == "medplain-synthetic-replay"
    assert {row["tool"] for row in result["runtime"]["tools_executed"] if row["success"]} == set(teachback.REQUIRED_TOOLS)
    assert result["evidence"]["entries"]
    assert result["runtime"]["native_output_attempts"][-1]["validated_input_sha256"] == canonical_hash(result["runtime"]["assessment_plan"])
    assert result["receipt"]["verdict"] == "provisional_reviewer_required"
    assert result["receipt"]["response_sha256"] == result["response_sha256"]
    teachback.validate_teachback_assessment(source, artifact, prompt, response, result, "case:teachback:attempt1")


@pytest.mark.parametrize("response", ["I take 999 mcg TID", "The new plan says 999 mL QHS"])
def test_reported_dose_or_frequency_mismatch_is_preserved_as_provisional_misunderstanding(released, response):
    result = assess(released, response)
    assert result["status"] == "provisional", result
    assert result["assessment"]["misunderstood_statement_ids"] == released[2]["statement_ids"]
    assert result["assessment"]["covered_statement_ids"] == []
    assert result["follow_up"]["code"] == "review_misunderstanding"
    original = {row["statement_id"]: row["citation"]["quote"] for row in released[1]["artifact"]["source_statements"]}
    assert all(row["quote"] == original[row["statement_id"]] for row in result["follow_up"]["source_quotes"])
    assert "999" not in json.dumps(result["follow_up"])


def test_misunderstanding_and_recheck_attempts_keep_distinct_immutable_bindings(released):
    first = assess(released, "I take 999 mg BID", "case:teachback:first")
    saved = copy.deepcopy(first)
    second = assess(released, "Please help me explain the schedule", "case:teachback:recheck")
    assert first == saved
    assert first["assessment"]["misunderstood_statement_ids"]
    assert second["assessment"]["missing_statement_ids"]
    assert first["receipt"]["hash"] != second["receipt"]["hash"]
    assert first["execution_id"] != second["execution_id"]
    assert all(value["requires_reviewer"] is True for value in [first, second])


@pytest.mark.parametrize("mutation", ["source", "prompt", "response", "execution", "evidence", "quote", "native", "fresh", "advice", "regimen"])
def test_independent_validator_rejects_forged_boundaries_or_unsupported_publication(released, mutation):
    source, artifact, prompt = released
    response = "Yes, I understand"
    result = assess(released, response)
    if mutation == "source":
        result["source_hash"] = "0" * 64
    elif mutation == "prompt":
        result["prompt_sha256"] = "0" * 64
    elif mutation == "response":
        result["response_sha256"] = "0" * 64
    elif mutation == "execution":
        result["execution_id"] = "another:attempt"
    elif mutation == "evidence":
        result["evidence"]["entries"][0]["uri"] = "https://example.invalid/fake"
    elif mutation == "quote":
        result["follow_up"]["source_quotes"][0]["quote"] += " Take 999 mg."
    elif mutation == "native":
        result["runtime"]["native_output_attempts"][-1]["validated_input_sha256"] = "0" * 64
    elif mutation == "fresh":
        result["runtime"]["tools_executed"] = []
    elif mutation == "advice":
        result["new_medical_advice"] = "Take 999 mg."
    else:
        result["patient_regimen"] = ["new treatment"]
    with pytest.raises(ValueError):
        teachback.validate_teachback_assessment(source, artifact, prompt, response, result, "case:teachback:attempt1")


@pytest.mark.parametrize("failure", ["unknown_source", "unknown_evidence", "covered_acknowledgement", "covered_changed_frequency", "covered_changed_unit", "stale_context"])
def test_model_contract_failures_get_one_fresh_bounded_repair_then_no_publication(monkeypatch, released, failure):
    original = teachback._fixture_payload
    response = "Yes, I understand everything now"
    if failure == "covered_changed_frequency":
        response = "I will use the listed medicine TID"
    elif failure == "covered_changed_unit":
        response = "I think the amount is 999 mcg"

    def payload(context):
        plan = original(context)
        if failure == "unknown_source":
            plan["missing_statement_ids"] = ["foreign:L1"]
        elif failure == "unknown_evidence":
            plan["evidence_ids"] = ["unapproved-reference"]
        elif failure == "stale_context":
            plan["context_ref"] = "teachback-" + "0" * 32
        else:
            plan.update(covered_statement_ids=list(context.prompt["statement_ids"]), missing_statement_ids=[],
                        misunderstood_statement_ids=[], follow_up_code="review_response")
        return plan

    monkeypatch.setattr(teachback, "_fixture_payload", payload)
    result = assess(released, response)
    assert result["status"] == "refused", result
    assert result["assessment"] is result["follow_up"] is result["receipt"] is None
    assert result["runtime"]["structured_repairs"] == 1
    assert result["runtime"]["invocations"] == 2
    assert len(result["runtime"]["repair_diagnostics"]) == 2
    for invocation in [1, 2]:
        assert {row["tool"] for row in result["runtime"]["tools_executed"]
                if row["invocation_index"] == invocation and row["success"]} == set(teachback.REQUIRED_TOOLS)


def test_model_must_reread_fresh_evidence_and_cannot_inherit_old_attempt_context(monkeypatch, released):
    def model(mode, context, tools):
        def payload():
            return {"context_ref": teachback._context_ref(context.binding, context.invocation),
                    "covered_statement_ids": [], "missing_statement_ids": list(context.prompt["statement_ids"]),
                    "misunderstood_statement_ids": [], "evidence_ids": ["ahrq-teachback:ask-own-words"],
                    "follow_up_code": "repeat_in_own_words"}
        return FixtureModel(["read_teachback_context"], teachback.AssessmentPlan, payload)
    monkeypatch.setattr(teachback, "_model", model)
    result = assess(released)
    assert result["status"] == "refused"
    assert result["assessment"] is result["receipt"] is None
    assert result["runtime"]["structured_repairs"] == 1
    assert all(row["tool"] != "retrieve_teachback_evidence" for row in result["runtime"]["tools_executed"])


def test_even_model_proposed_complete_coverage_remains_provisional_for_reviewer(monkeypatch, released):
    original = teachback._fixture_payload
    def payload(context):
        value = original(context)
        value.update(covered_statement_ids=list(context.prompt["statement_ids"]), missing_statement_ids=[],
                     misunderstood_statement_ids=[], follow_up_code="review_response")
        return value
    monkeypatch.setattr(teachback, "_fixture_payload", payload)
    result = assess(released, "I will ask the reviewer about the schedule and any questions I still have")
    assert result["status"] == "provisional" and result["requires_reviewer"] is True
    assert result["assessment"]["covered_statement_ids"]
    assert result["patient_regimen"] is None
    assert result["follow_up"]["code"] == "review_response"
    assert "understood" not in result and "closed" not in result


def test_prompt_or_stale_release_and_empty_response_are_rejected_before_inference(monkeypatch, released):
    monkeypatch.setattr(teachback, "_model", lambda *args: (_ for _ in ()).throw(AssertionError("Must not infer")))
    source, result, prompt = released
    for response in ["", " ", "x" * 2001]:
        with pytest.raises(ValueError):
            teachback.run_teachback_assessment(source, result, prompt, response, "attempt")
    stale = copy.deepcopy(prompt)
    stale["text"] += " extra advice"
    with pytest.raises(ValueError):
        teachback.run_teachback_assessment(source, result, stale, "I need help", "attempt")
    corrupt = copy.deepcopy(result)
    corrupt["artifact_hash"] = "0" * 64
    with pytest.raises(ValueError):
        teachback.run_teachback_assessment(source, corrupt, prompt, "I need help", "attempt")


def test_expired_or_unrelated_evidence_never_authorizes_assessment(monkeypatch, released):
    result = teachback.run_teachback_assessment(*released, "Please help me explain it", "expired:attempt",
                                               mode="fixture", evidence_as_of="2026-11-30")
    assert result["status"] == "refused" and result["assessment"] is None
    assert result["evidence"]["status"] == "no_support"


def test_deadline_and_turn_budgets_stop_before_another_model_or_tool_call(released):
    source, result, prompt = released
    prompt, rows, binding = teachback._inputs(source, result, prompt, "I need some help", "bounded:attempt")
    context = teachback._Context(prompt, rows, "I need some help", binding, "2026-10-30")
    context.deadline = -1
    hook = teachback._AssessmentHook(context)
    with pytest.raises(RuntimeError, match="deadline"):
        hook.before_model(SimpleNamespace())
    assert context.total_turns == 0
    with pytest.raises(RuntimeError, match="deadline"):
        teachback._tools(context)[0]()
    context.deadline = float("inf")
    context.total_turns = teachback.MAX_TOTAL_MODEL_TURNS
    with pytest.raises(RuntimeError, match="turn_budget"):
        hook.before_model(SimpleNamespace())
    assert context.total_turns == teachback.MAX_TOTAL_MODEL_TURNS


def test_bedrock_adapter_is_explicit_bounded_and_not_invoked(monkeypatch, released):
    captured = []
    monkeypatch.setattr(teachback, "BedrockModel", lambda **kwargs: captured.append(kwargs) or SimpleNamespace())
    monkeypatch.delenv("MEDPLAIN_CAREFLOW_MODEL_ID", raising=False)
    source, result, prompt = released
    prompt, rows, binding = teachback._inputs(source, result, prompt, "I need help", "bedrock:config")
    context = teachback._Context(prompt, rows, "I need help", binding, "2026-10-30")
    model = teachback._model("bedrock", context, teachback._tools(context))
    assert isinstance(model, teachback.RequiredToolModel)
    assert captured[0]["model_id"] == "amazon.nova-pro-v1:0"
    assert captured[0]["max_tokens"] == 2048 and captured[0]["temperature"] == 0.0
    assert captured[0]["boto_client_config"].read_timeout == 45
    assert captured[0]["boto_client_config"].connect_timeout == 5
    assert captured[0]["boto_client_config"].retries["total_max_attempts"] == 1
    monkeypatch.setenv("MEDPLAIN_CAREFLOW_MODEL_ID", "unapproved-model")
    with pytest.raises(ValueError):
        teachback._model("bedrock", context, teachback._tools(context))
