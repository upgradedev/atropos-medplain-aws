"""Require fresh Nova tool calls using Bedrock's documented toolChoice control."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from threading import Lock
from typing import Any

from pydantic import BaseModel
from strands.models.model import Model


NOVA_TOOL_SCHEMA_PROFILE = "nova-v1-root-metadata-v1"
_NOVA_ROOT_METADATA = frozenset({"$schema", "title", "description"})
_NOVA_ROOT_FIELDS = frozenset({"type", "properties", "required"})
_CLIENT_INSTALL_LOCK = Lock()
_PROVIDER_CALL_LEDGER: ContextVar[_ProviderCallLedger | None] = ContextVar("medplain_provider_call_ledger", default=None)


class _ProviderCallLedger:
    """One SDK client-method reservation, shared by copied worker contexts."""

    def __init__(self, owner: object) -> None:
        self.owner = owner
        self.lock = Lock()
        self.attempted = False
        self.closed = False

    def reserve(self, owner: object) -> None:
        with self.lock:
            if self.owner is not owner or self.closed or self.attempted:
                raise RuntimeError("The single Converse client-call budget is unavailable; publication refused")
            # A local failure or cancellation must never refund this attempt.
            self.attempted = True

    def close(self) -> None:
        with self.lock:
            self.closed = True


class _SingleConverseClient:
    """Preserve SDK client metadata while denying inference outside its owned turn."""

    def __init__(self, client: Any, delegate: Model) -> None:
        self.client = client
        self.delegate = delegate

    def converse(self, *args: Any, **kwargs: Any) -> Any:
        ledger = _PROVIDER_CALL_LEDGER.get()
        if ledger is None:
            raise RuntimeError("An owned Converse client-call ledger is required; publication refused")
        ledger.reserve(self)
        return self.client.converse(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("converse", "invoke_model")):
            raise RuntimeError("Only Converse is permitted by this transport profile; publication refused")
        return getattr(self.client, name)


def nova_tool_specs(tool_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy tool specs and omit only Nova-unsupported root schema metadata.

    Nova 1 documents type/properties/required as its supported root fields.
    The SDK's Pydantic schema and all nested constraints stay unchanged. Any
    other root field is a refusal, never a silently discarded constraint.
    """
    projected = []
    for spec in tool_specs:
        input_schema = spec.get("inputSchema")
        schema = input_schema.get("json") if isinstance(input_schema, dict) else None
        if (not isinstance(schema, dict) or schema.get("type") != "object"
                or not isinstance(schema.get("properties"), dict)
                or set(schema) - _NOVA_ROOT_FIELDS - _NOVA_ROOT_METADATA):
            raise RuntimeError("Unsupported Nova tool schema root; publication refused")
        copied = deepcopy(spec)
        copied["inputSchema"]["json"] = {
            key: value for key, value in copied["inputSchema"]["json"].items()
            if key not in _NOVA_ROOT_METADATA
        }
        projected.append(copied)
    return projected


class RequiredToolModel(Model):
    """Keep mandatory clinical evidence in the actual Bedrock/Strands path.

    Nova supports ``toolChoice.tool`` to request one named tool. The current
    invocation must execute each required tool successfully before the model
    may request its Pydantic result tool. This wrapper never executes tools or
    manufactures model output; the SDK does both tool dispatch and validation.
    """

    def __init__(
        self,
        delegate: Model,
        missing_tools: Callable[[], list[str]],
        output_tool: str,
        *,
        nova_schema_compatibility: bool = False,
        selected_tool_only: bool = False,
        single_provider_call: bool = False,
    ) -> None:
        self.delegate = delegate
        self.missing_tools = missing_tools
        self.output_tool = output_tool
        self.nova_schema_compatibility = nova_schema_compatibility
        self.selected_tool_only = selected_tool_only
        self.single_provider_call = single_provider_call
        self._single_call_client: _SingleConverseClient | None = None

    @contextmanager
    def _provider_turn(self):
        # Installation is permanent and atomic; never swap the client per turn.
        with _CLIENT_INSTALL_LOCK:
            client = getattr(self.delegate, "client", None)
            if self._single_call_client is not None and client is not self._single_call_client:
                raise RuntimeError("The guarded SDK client changed; publication refused")
            if isinstance(client, _SingleConverseClient):
                if client.delegate is not self.delegate:
                    raise RuntimeError("The guarded SDK client has a different owner; publication refused")
            elif client is None:
                raise RuntimeError("A guarded SDK client is required; publication refused")
            else:
                client = _SingleConverseClient(client, self.delegate)
                self.delegate.client = client
            self._single_call_client = client
        ledger = _ProviderCallLedger(client)
        token = _PROVIDER_CALL_LEDGER.set(ledger)
        try:
            yield
        finally:
            ledger.close()
            _PROVIDER_CALL_LEDGER.reset(token)

    def update_config(self, **model_config: Any) -> None:
        self.delegate.update_config(**model_config)

    def get_config(self) -> Any:
        return self.delegate.get_config()

    async def structured_output(
        self,
        output_model: type[BaseModel],
        prompt: list[dict[str, Any]],
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        if self.single_provider_call:
            with self._provider_turn():
                async with aclosing(self.delegate.structured_output(output_model, prompt, system_prompt, **kwargs)) as events:
                    async for event in events:
                        yield event
            return
        async for event in self.delegate.structured_output(output_model, prompt, system_prompt, **kwargs):
            yield event

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tool_specs: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        missing = self.missing_tools()
        selected_tool = missing[0] if missing else self.output_tool
        if self.selected_tool_only:
            # Validate every original Nova schema before narrowing availability.
            # Historical messages and the SDK tool registry remain untouched.
            projected = nova_tool_specs(tool_specs or []) if self.nova_schema_compatibility else tool_specs or []
            provider_specs = [spec for spec in projected if spec["name"] == selected_tool]
            if len(provider_specs) != 1:
                raise RuntimeError("Exactly one required clinical tool must be available; publication refused")
        else:
            available = {spec["name"] for spec in tool_specs or []}
            if selected_tool not in available:
                raise RuntimeError("A required clinical tool is unavailable in the model request; publication refused")
            provider_specs = nova_tool_specs(tool_specs or []) if self.nova_schema_compatibility else tool_specs
        if self.single_provider_call:
            with self._provider_turn():
                async with aclosing(self.delegate.stream(
                    messages, tool_specs=provider_specs, system_prompt=system_prompt,
                    tool_choice={"tool": {"name": selected_tool}}, **kwargs,
                )) as events:
                    async for event in events:
                        yield event
            return
        async for event in self.delegate.stream(
            messages,
            tool_specs=provider_specs,
            system_prompt=system_prompt,
            tool_choice={"tool": {"name": selected_tool}},
            **kwargs,
        ):
            yield event
