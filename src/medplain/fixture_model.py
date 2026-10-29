"""Offline model adapter for exercising the real Strands event loop.

This adapter is a deterministic test double for inference, never for Strands.
It emits SDK streaming tool-use events, so tools, structured-output validation,
hooks and Graph routing execute exactly as they do with the Bedrock adapter.
The UI and receipt response label this mode as a synthetic replay.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from typing import Any

from pydantic import BaseModel
from strands.models.model import Model


class FixtureModel(Model):
    """Replay a bounded tool plan and then a Pydantic structured output."""

    def __init__(
        self,
        tool_names: list[str],
        output_model: type[BaseModel],
        payload: Callable[[], dict[str, Any]],
    ) -> None:
        self._tool_names = list(tool_names)
        self._output_model = output_model
        self._payload = payload
        self._config = {"model_id": "medplain-synthetic-replay", "context_window_limit": 32768}
        self._turn = 0
        self._invocation = 0

    def begin_invocation(self) -> None:
        self._turn = 0
        self._invocation += 1

    def update_config(self, **model_config: Any) -> None:
        self._config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return dict(self._config)

    async def structured_output(
        self,
        output_model: type[BaseModel],
        prompt: list[dict[str, Any]],
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        yield {"output": output_model.model_validate(self._payload())}

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tool_specs: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        self._turn += 1
        if self._turn == 1:
            calls = [(name, {}) for name in self._tool_names]
        elif self._turn == 2:
            calls = [(self._output_model.__name__, self._payload())]
        else:
            raise RuntimeError("Synthetic replay exceeded its bounded tool plan")

        available = {spec["name"] for spec in tool_specs or []}
        if any(name not in available for name, _ in calls):
            raise RuntimeError("Required Strands tool was not registered")

        yield {"messageStart": {"role": "assistant"}}
        for index, (name, arguments) in enumerate(calls):
            call_id = f"fixture-{self._invocation}-{self._turn}-{index}"
            yield {
                "contentBlockStart": {
                    "contentBlockIndex": index,
                    "start": {"toolUse": {"toolUseId": call_id, "name": name}},
                }
            }
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": index,
                    "delta": {"toolUse": {"input": json.dumps(arguments)}},
                }
            }
            yield {"contentBlockStop": {"contentBlockIndex": index}}
        yield {"messageStop": {"stopReason": "tool_use"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                "metrics": {"latencyMs": 0},
            }
        }
