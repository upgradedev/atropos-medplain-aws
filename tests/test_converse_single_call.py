"""Pinned SDK transport and call-seam controls, with invented responses and no AWS I/O."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
import json
from threading import Barrier, Event, Lock
from types import SimpleNamespace

from botocore.config import Config
from botocore.exceptions import ClientError
import pytest
import strands.models.bedrock as sdk

from medplain import reconciliation_engine as engine
from medplain import required_tool_model as adapter
from medplain.gate import canonical_hash
from medplain.reconciliation import run_reconciliation, validate_reconciliation_result


def _response(name="FinalTool", payload=None, stop="tool_use"):
    return {"output": {"message": {"role": "assistant", "content": [
        {"toolUse": {"toolUseId": "unchanged-tool-id", "name": name, "input": payload if payload is not None else {}}}]}},
        "stopReason": stop, "usage": {"inputTokens": 3, "outputTokens": 4, "totalTokens": 7},
        "metrics": {"latencyMs": 5}}


class _Client:
    meta = SimpleNamespace(region_name="us-east-1")
    exceptions = SimpleNamespace(marker="unchanged-exceptions")

    def __init__(self, answer=None):
        self.answer = answer or (lambda request: _response(request["toolConfig"]["toolChoice"]["tool"]["name"]))
        self.calls = []
        self.stream_calls = []
        self.lock = Lock()

    def converse(self, **request):
        with self.lock:
            self.calls.append(deepcopy(request))
        return self.answer(request)

    def converse_stream(self, **request):
        self.stream_calls.append(deepcopy(request))
        response = self.answer(request)
        return {"stream": list(sdk.BedrockModel.convert_non_streaming_to_streaming(None, response))}


def _delegate(client, streaming=False):
    session = SimpleNamespace(region_name="us-east-1", client=lambda **kwargs: client)
    return sdk.BedrockModel(boto_session=session, model_id="amazon.nova-pro-v1:0", streaming=streaming,
                           temperature=0.0, max_tokens=4096,
                           additional_request_fields={"inferenceConfig": {"topK": 1}},
                           boto_client_config=Config(connect_timeout=5, read_timeout=45,
                                                     retries={"total_max_attempts": 1}))


def _wrapper(client, *, guarded=True, streaming=False, missing=None):
    delegate = _delegate(client, streaming)
    return adapter.RequiredToolModel(delegate, missing or (lambda: []), "FinalTool",
                                     nova_schema_compatibility=True, selected_tool_only=True,
                                     single_provider_call=guarded)


def _specs():
    return [{"name": name, "description": "Unchanged tool description", "inputSchema": {"json": {
        "type": "object", "title": "Root metadata", "properties": {
            "value": {"type": "string", "minLength": 1, "description": "Keep nested description"}},
        "required": ["value"]}}} for name in ["ReadTool", "CheckTool", "FinalTool"]]


async def _collect(model, messages=None, specs=None, **kwargs):
    return [event async for event in model.stream(messages if messages is not None else [
        {"role": "user", "content": [{"text": "Invented protocol test"}]}], specs or _specs(), **kwargs)]


def test_sdk_converse_conversion_preserves_complete_tool_input_stop_reason_and_metadata():
    payload = {"nested": ["α", 2, False], "reason": "Invented input, not medical advice"}
    response = _response(payload=payload)
    saved = deepcopy(response)
    client = _Client(lambda request: deepcopy(response))
    model = _wrapper(client)
    assert model.delegate.client is client
    generator = model.stream([], _specs())
    assert model.delegate.client is client  # Creating the generator must not install anything.
    asyncio.run(generator.aclose())
    assert model.delegate.client is client
    events = asyncio.run(_collect(model))
    expected = list(model.delegate.convert_non_streaming_to_streaming(saved))
    assert events == expected
    assert json.loads(next(item["contentBlockDelta"]["delta"]["toolUse"]["input"]
                           for item in events if "toolUse" in item.get("contentBlockDelta", {}).get("delta", {}))) == payload
    assert response == saved and len(client.calls) == 1 and client.stream_calls == []
    assert model.delegate.client.meta is client.meta
    assert model.delegate.client.exceptions is client.exceptions
    assert adapter._PROVIDER_CALL_LEDGER.get() is None


def test_plain_nonstream_response_uses_only_the_unmodified_sdk_event_converter():
    response = _response(stop="end_turn")
    response["output"]["message"]["content"] = [{"text": "Unchanged plain response α"}]
    saved = deepcopy(response)
    client = _Client(lambda request: deepcopy(response))
    model = _wrapper(client)
    events = asyncio.run(_collect(model))
    assert events == list(model.delegate.convert_non_streaming_to_streaming(saved))
    assert not any("toolUse" in item.get("contentBlockStart", {}).get("start", {}) for item in events)
    assert response == saved and len(client.calls) == 1 and client.stream_calls == []


@pytest.mark.parametrize("missing", ["input", "output"])
def test_incomplete_provider_response_is_not_filled_retried_or_replaced(missing):
    response = _response()
    if missing == "input":
        del response["output"]["message"]["content"][0]["toolUse"]["input"]
    else:
        del response["output"]
    saved = deepcopy(response)
    client = _Client(lambda request: deepcopy(response))
    with pytest.raises(KeyError):
        asyncio.run(_collect(_wrapper(client)))
    assert response == saved and len(client.calls) == 1 and client.stream_calls == []
    assert adapter._PROVIDER_CALL_LEDGER.get() is None


@pytest.mark.parametrize("stop", ["end_turn", "max_tokens"])
def test_sdk_converter_does_not_relabel_non_tool_stop_reasons(stop):
    client = _Client(lambda request: _response(stop=stop))
    events = asyncio.run(_collect(_wrapper(client)))
    assert next(item["messageStop"]["stopReason"] for item in events if "messageStop" in item) == stop
    assert len(client.calls) == 1 and client.stream_calls == []


def test_three_turns_keep_exact_formatted_requests_history_schema_and_inference_fields():
    nonstream, stream = _Client(), _Client()
    missing = ["ReadTool", "CheckTool"]
    guarded = _wrapper(nonstream, missing=lambda: list(missing))
    legacy = _wrapper(stream, guarded=False, streaming=True, missing=lambda: list(missing))
    specs = _specs()
    messages = [{"role": "user", "content": [{"text": "Complete original prompt"}]}]
    saved_specs = deepcopy(specs)

    async def exercise():
        for name in ["ReadTool", "CheckTool", "FinalTool"]:
            saved = deepcopy(messages)
            await _collect(guarded, messages, specs, system_prompt="Keep exact system text", dynamic_trailing_blocks=0)
            await _collect(legacy, messages, specs, system_prompt="Keep exact system text", dynamic_trailing_blocks=0)
            assert messages == saved
            assert nonstream.calls[-1] == stream.stream_calls[-1]
            assert nonstream.calls[-1]["toolConfig"]["toolChoice"] == {"tool": {"name": name}}
            assert [item["toolSpec"]["name"] for item in nonstream.calls[-1]["toolConfig"]["tools"]] == [name]
            if missing:
                missing.pop(0)
            messages.extend([
                {"role": "assistant", "content": [{"toolUse": {"toolUseId": name, "name": name, "input": {}}}]},
                {"role": "user", "content": [{"toolResult": {"toolUseId": name, "status": "success",
                    "content": [{"text": "Earlier exact tool result"}]}}]}])
    asyncio.run(exercise())
    assert len(nonstream.calls) == len(stream.stream_calls) == 3
    assert nonstream.stream_calls == stream.calls == []
    assert specs == saved_specs
    assert nonstream.calls[0]["inferenceConfig"] == {"maxTokens": 4096, "temperature": 0.0}
    assert nonstream.calls[0]["additionalModelRequestFields"] == {"inferenceConfig": {"topK": 1}}
    assert adapter._PROVIDER_CALL_LEDGER.get() is None


def test_sdk_compatibility_resubmission_is_blocked_before_second_client_io_without_history_change():
    def fail(request):
        raise ClientError({"Error": {"Code": "ValidationException",
                                     "Message": sdk._TOOL_RESULT_TURN_VALIDATION_MESSAGE}}, "Converse")
    client = _Client(fail)
    model = _wrapper(client)
    messages = [{"role": "assistant", "content": [{"toolUse": {
        "toolUseId": "previous", "name": "EarlierTool", "input": {}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "previous", "status": "success",
            "content": [{"text": "Original result"}]}}, {"text": "Original adjacent conversation"}]}]
    saved = deepcopy(messages)
    with pytest.raises(RuntimeError, match="single Converse client-call budget"):
        asyncio.run(_collect(model, messages))
    assert len(client.calls) == 1 and client.stream_calls == []
    assert messages == saved
    assert model.delegate._tool_result_turn_separation_model_id is None
    assert adapter._PROVIDER_CALL_LEDGER.get() is None


@pytest.mark.parametrize("failure", ["model_error", "validation", "timeout", "runtime"])
def test_provider_errors_leave_one_attempt_no_fallback_and_reset_the_context(failure):
    def fail(request):
        if failure == "timeout":
            raise TimeoutError("Ambiguous invented timeout")
        if failure == "runtime":
            raise RuntimeError("Invented local failure")
        raise ClientError({"Error": {"Code": "ModelErrorException" if failure == "model_error" else "ValidationException",
                                     "Message": "Invented noncompatibility failure"}}, "Converse")
    client = _Client(fail)
    model = _wrapper(client)
    with pytest.raises((ClientError, RuntimeError, TimeoutError)):
        asyncio.run(_collect(model))
    assert len(client.calls) == 1 and client.stream_calls == []
    assert adapter._PROVIDER_CALL_LEDGER.get() is None
    client.answer = lambda request: _response()
    asyncio.run(_collect(model))
    assert len(client.calls) == 2  # A separate explicit turn gets a new ledger, never a refund.


def test_default_false_does_not_inspect_or_modify_a_delegate_client():
    class Delegate:
        @property
        def client(self):
            raise AssertionError("Legacy client property must not be inspected")

        async def stream(self, *args, **kwargs):
            yield {"messageStop": {"stopReason": "end_turn"}}
    model = adapter.RequiredToolModel(Delegate(), lambda: [], "FinalTool")
    assert model.single_provider_call is False
    assert asyncio.run(_collect(model)) == [{"messageStop": {"stopReason": "end_turn"}}]


@pytest.mark.parametrize("operation", ["converse_stream", "invoke_model", "invoke_model_with_response_stream"])
def test_prohibited_generation_operations_are_denied_even_before_any_converse_call(operation):
    client = _Client()
    model = _wrapper(client)
    with model._provider_turn():
        with pytest.raises(RuntimeError, match="Only Converse"):
            getattr(model.delegate.client, operation)()
    assert client.calls == client.stream_calls == []


@pytest.mark.parametrize("ledger_kind", ["missing", "foreign", "closed"])
def test_absent_foreign_or_closed_ledger_cannot_forward_a_client_call(ledger_kind):
    client = _Client()
    model = _wrapper(client)
    with model._provider_turn():
        pass
    if ledger_kind == "missing":
        with pytest.raises(RuntimeError, match="ledger is required"):
            model.delegate.client.converse()
    else:
        ledger = adapter._ProviderCallLedger(model.delegate.client if ledger_kind == "closed" else object())
        if ledger_kind == "closed":
            ledger.close()
        token = adapter._PROVIDER_CALL_LEDGER.set(ledger)
        try:
            with pytest.raises(RuntimeError, match="budget is unavailable"):
                model.delegate.client.converse()
        finally:
            adapter._PROVIDER_CALL_LEDGER.reset(token)
    assert client.calls == client.stream_calls == []


def test_two_threads_with_one_copied_ledger_forward_only_one_attempt():
    client = _Client(lambda request: _response())
    model = _wrapper(client)
    barrier = Barrier(2)
    def attempt(context):
        barrier.wait(timeout=2)
        try:
            return context.run(model.delegate.client.converse)
        except RuntimeError:
            return "blocked"
    with model._provider_turn():
        context = copy_context()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [context.copy(), context.copy()]))
    assert results.count("blocked") == 1
    assert len(client.calls) == 1 and client.stream_calls == []


def test_concurrent_sdk_streams_install_one_proxy_and_keep_independent_ledgers():
    barrier = Barrier(2)
    def answer(request):
        barrier.wait(timeout=3)
        return _response()
    client = _Client(answer)
    model = _wrapper(client)
    async def exercise():
        return await asyncio.gather(_collect(model), _collect(model))
    results = asyncio.run(exercise())
    assert len(results) == 2 and all(results)
    assert len(client.calls) == 2 and client.stream_calls == []
    assert isinstance(model.delegate.client, adapter._SingleConverseClient)
    assert model.delegate.client.client is client
    assert adapter._PROVIDER_CALL_LEDGER.get() is None


def test_generator_close_closes_ledger_and_restores_outer_context():
    client = _Client()
    model = _wrapper(client)
    outer = adapter._ProviderCallLedger(object())
    async def exercise():
        token = adapter._PROVIDER_CALL_LEDGER.set(outer)
        try:
            generator = model.stream([{"role": "user", "content": [{"text": "Invented"}]}], _specs())
            await anext(generator)
            ledger = adapter._PROVIDER_CALL_LEDGER.get()
            assert ledger is not outer and ledger.attempted is True
            await generator.aclose()
            assert ledger.closed is True and adapter._PROVIDER_CALL_LEDGER.get() is outer
        finally:
            adapter._PROVIDER_CALL_LEDGER.reset(token)
    asyncio.run(exercise())
    assert len(client.calls) == 1


def test_proxy_install_is_idempotent_and_refuses_foreign_owners_or_replaced_clients():
    client = _Client()
    model = _wrapper(client)
    with model._provider_turn():
        first = model.delegate.client
    second = adapter.RequiredToolModel(model.delegate, lambda: [], "FinalTool", single_provider_call=True)
    with second._provider_turn():
        assert second.delegate.client is first and first.client is client
    foreign = _wrapper(_Client())
    foreign.delegate.client = first
    with pytest.raises(RuntimeError, match="different owner"):
        with foreign._provider_turn():
            pass
    model.delegate.client = _Client()
    with pytest.raises(RuntimeError, match="client changed"):
        with model._provider_turn():
            pass
    assert client.calls == client.stream_calls == []


def test_opted_in_structured_output_entry_cannot_bypass_the_same_call_guard():
    client = _Client(lambda request: _response())
    class Delegate:
        def __init__(self):
            self.client = client

        async def structured_output(self, *args, **kwargs):
            self.client.converse()
            yield {"invented": "unchanged first event"}
            self.client.converse()
    model = adapter.RequiredToolModel(Delegate(), lambda: [], "FinalTool", single_provider_call=True)
    async def exercise():
        return [event async for event in model.structured_output(dict, [])]
    with pytest.raises(RuntimeError, match="single Converse client-call budget"):
        asyncio.run(exercise())
    assert len(client.calls) == 1 and adapter._PROVIDER_CALL_LEDGER.get() is None


@pytest.mark.parametrize("reserve_before_cancel", [True, False])
def test_cancelled_sdk_worker_cannot_start_or_repeat_io_with_its_copied_closed_ledger(reserve_before_cancel):
    entered, release, finished = Event(), Event(), Event()
    saved = {}
    client = _Client()
    model = _wrapper(client)
    original_stream = model.delegate._stream
    if reserve_before_cancel:
        def answer(request):
            saved["ledger"] = adapter._PROVIDER_CALL_LEDGER.get()
            entered.set()
            try:
                assert release.wait(3)
                with pytest.raises(RuntimeError, match="budget is unavailable"):
                    model.delegate.client.converse()
                return _response()
            finally:
                finished.set()
        client.answer = answer
    else:
        def delayed(*args, **kwargs):
            saved["ledger"] = adapter._PROVIDER_CALL_LEDGER.get()
            entered.set()
            try:
                assert release.wait(3)
                return original_stream(*args, **kwargs)
            finally:
                finished.set()
        model.delegate._stream = delayed
    async def exercise():
        task = asyncio.create_task(_collect(model))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert saved["ledger"].closed is True
            assert adapter._PROVIDER_CALL_LEDGER.get() is None
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 2)
            await asyncio.sleep(0)
    asyncio.run(exercise())
    assert len(client.calls) == (1 if reserve_before_cancel else 0)
    assert client.stream_calls == []


def _source():
    return {"profile": "medplain-reconciliation-v1", "patient_alias": "Demo-Converse", "language": "en",
            "caregiver_concerns": "", "documents": [
                {"document_id": "home", "kind": "home_list", "issued_at": "2026-10-20T09:00:00Z",
                 "effective_at": None, "text": "Stop Amlodipine 5 mg by mouth QD."},
                {"document_id": "discharge", "kind": "discharge", "issued_at": "2026-10-30T09:00:00Z",
                 "effective_at": None, "text": "Stop Amlodipine 5 mg by mouth QD."}]}


def _wire(source):
    return {"selection": {"source_hash": canonical_hash(source), "questions": [],
                          "teachback": [{"code": "teachback", "statement_ids": ["home:L1", "discharge:L1"]}]},
            "nonask_review": [{"disposition": "resolved", "intent": "compare_instructions",
                               "statement_ids": ["home:L1", "discharge:L1"],
                               "reason": "Both original instructions explicitly stop the same medicine."}]}


@pytest.mark.parametrize("malformation", [None, "missing", "extra", "wrong_source", "truncated",
                                         "transport_error", "wrong_transport"])
def test_actual_nonstream_sdk_graph_keeps_native_private_withholding_and_finite_budgets(monkeypatch, malformation):
    source = _source()
    def answer(request):
        if malformation == "transport_error":
            raise ClientError({"Error": {"Code": "ModelErrorException", "Message": "Invented generation failure"}}, "Converse")
        name = request["toolConfig"]["toolChoice"]["tool"]["name"]
        payload = _wire(source) if name == "PlanningWire" else {}
        if name == "PlanningWire":
            if malformation == "missing":
                del payload["selection"]["source_hash"]
            elif malformation == "extra":
                payload["invented_extra_field"] = "must refuse"
            elif malformation == "wrong_source":
                payload["selection"]["source_hash"] = "0" * 64
        return _response(name, payload, "max_tokens" if malformation == "truncated" and name == "PlanningWire" else "tool_use")
    client = _Client(answer)
    monkeypatch.setattr(sdk.boto3, "Session", lambda: SimpleNamespace(region_name="us-east-1", client=lambda **kwargs: client))
    original = engine._model
    def model(mode, context, role, tools, output):
        result = original("bedrock" if role == engine.PLANNER else "fixture", context, role, tools, output)
        if role == engine.PLANNER and malformation == "wrong_transport":
            result.delegate.update_config(streaming=True)
        return result
    monkeypatch.setattr(engine, "_model", model)
    result = run_reconciliation(source, "nonstream-sdk-graph", mode="bedrock")
    assert client.stream_calls == []
    assert 0 <= len(client.calls) <= engine.MAX_MODEL_TURNS * engine.MAX_CANDIDATES
    if malformation == "wrong_transport":
        assert client.calls == []
    elif malformation == "transport_error":
        assert len(client.calls) == 1
    else:
        assert client.calls
    assert result["patient_regimen"] is None
    assert engine.planning_wire_schema_sha256() == "b2a2472a7c7d282962d7c13db283580ee8fb6488a9249dd46ed4773d48df6944"
    if malformation is None:
        assert result["status"] == "verified", result
        assert len(client.calls) == 3
        assert result["runtime"]["selection"]["questions"] == []
        assert result["runtime"]["selection"]["teachback"] == _wire(source)["selection"]["teachback"]
    else:
        assert result["status"] == "refused", result
        assert result["artifact"] is result["explanation_text"] is result["artifact_hash"] is None
        assert result["receipts"] == []
    validate_reconciliation_result(source, result)
    assert adapter._PROVIDER_CALL_LEDGER.get() is None


def test_engine_optin_is_only_planner_and_preserves_boto_retry_and_other_role_transport(monkeypatch):
    made = []
    def delegate(**kwargs):
        made.append(kwargs)
        return SimpleNamespace()
    monkeypatch.setattr(engine, "BedrockModel", delegate)
    context = engine.Context(_source(), "transport-role-config", False)
    tools = engine._tools(context)
    wrappers = [engine._model("bedrock", context, role, tools[role], engine._OUTPUT_MODELS[role])
                for role in engine.AGENT_NAMES]
    assert [model.single_provider_call for model in wrappers] == [False, True, False]
    assert "streaming" not in made[0] and made[1]["streaming"] is False and "streaming" not in made[2]
    assert all(row["temperature"] == 0.0 and row["max_tokens"] == 4096 for row in made)
    assert all(row["boto_client_config"].retries == {"total_max_attempts": 1} for row in made)
