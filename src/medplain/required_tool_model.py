"""Require fresh Nova tool calls using Bedrock's documented toolChoice control."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from copy import deepcopy
from typing import Any

from pydantic import BaseModel
from strands.models.model import Model


NOVA_TOOL_SCHEMA_PROFILE = "nova-v1-root-metadata-v1"
_NOVA_ROOT_METADATA = frozenset({"$schema", "title", "description"})
_NOVA_ROOT_FIELDS = frozenset({"type", "properties", "required"})


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
    ) -> None:
        self.delegate = delegate
        self.missing_tools = missing_tools
        self.output_tool = output_tool
        self.nova_schema_compatibility = nova_schema_compatibility

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
        available = {spec["name"] for spec in tool_specs or []}
        if selected_tool not in available:
            raise RuntimeError("A required clinical tool is unavailable in the model request; publication refused")
        provider_specs = nova_tool_specs(tool_specs or []) if self.nova_schema_compatibility else tool_specs
        async for event in self.delegate.stream(
            messages,
            tool_specs=provider_specs,
            system_prompt=system_prompt,
            tool_choice={"tool": {"name": selected_tool}},
            **kwargs,
        ):
            yield event
