"""The live model adapter must request fresh mandatory tools before output."""

import asyncio

import pytest

from medplain.required_tool_model import RequiredToolModel


class RecordingDelegate:
    def __init__(self):
        self.choices = []
        self.config = {}

    def update_config(self, **kwargs):
        self.config.update(kwargs)

    def get_config(self):
        return self.config

    async def stream(self, messages, **kwargs):
        self.choices.append(kwargs["tool_choice"])
        yield {"messageStart": {"role": "assistant"}}

    async def structured_output(self, *args, **kwargs):
        yield {"output": "delegated"}


def test_force_each_missing_tool_then_pydantic_output_and_reset_for_revisit():
    delegate = RecordingDelegate()
    successful = set()
    tools = ["parity", "interactions"]
    model = RequiredToolModel(delegate, lambda: [tool for tool in tools if tool not in successful], "TypedOutput")
    specs = [{"name": name} for name in [*tools, "TypedOutput"]]

    async def exercise():
        for tool in tools:
            async for _ in model.stream([], specs, tool_choice={"auto": {}}):
                pass
            assert delegate.choices[-1] == {"tool": {"name": tool}}
            successful.add(tool)
        async for _ in model.stream([], specs):
            pass
        assert delegate.choices[-1] == {"tool": {"name": "TypedOutput"}}
        successful.clear()
        async for _ in model.stream([], specs):
            pass
        assert delegate.choices[-1] == {"tool": {"name": "parity"}}
        with pytest.raises(RuntimeError, match="unavailable"):
            await anext(model.stream([], [{"name": "TypedOutput"}]))
        model.update_config(context_window_limit=123)
        assert model.get_config() == {"context_window_limit": 123}
        assert [event async for event in model.structured_output(None, [])] == [{"output": "delegated"}]

    asyncio.run(exercise())
