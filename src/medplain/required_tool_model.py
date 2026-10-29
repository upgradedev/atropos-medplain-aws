"""Require fresh Nova tool calls using Bedrock's documented toolChoice control."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any

from pydantic import BaseModel
from strands.models.model import Model


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
    ) -> None:
        self.delegate = delegate
        self.missing_tools = missing_tools
        self.output_tool = output_tool

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
        async for event in self.delegate.stream(
            messages,
            tool_specs=tool_specs,
            system_prompt=system_prompt,
            tool_choice={"tool": {"name": selected_tool}},
            **kwargs,
        ):
            yield event
