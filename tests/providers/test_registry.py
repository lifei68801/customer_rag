import pytest

from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry


class FakeProvider:
    def __init__(self, name: str) -> None:
        self._name = name

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=f"response-from-{self._name}")


async def test_run_routes_to_the_named_provider():
    registry = ProviderRegistry()
    registry.register(ProviderCapability.LLM, "qwen", FakeProvider("qwen"))
    registry.register(ProviderCapability.LLM, "deepseek", FakeProvider("deepseek"))

    result = await registry.run(
        ProviderCapability.LLM,
        ProviderRequest(messages=[{"role": "user", "content": "hi"}]),
        provider_name="deepseek",
    )

    assert result.text == "response-from-deepseek"


async def test_run_raises_for_unregistered_provider_name():
    registry = ProviderRegistry()
    registry.register(ProviderCapability.LLM, "qwen", FakeProvider("qwen"))

    with pytest.raises(KeyError):
        await registry.run(
            ProviderCapability.LLM,
            ProviderRequest(messages=[{"role": "user", "content": "hi"}]),
            provider_name="glm",
        )


class StreamingFakeProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text="不应该被用到")

    async def stream_complete(self, request: ProviderRequest):
        for chunk in ["你好", "，世界"]:
            yield chunk


def test_supports_streaming_true_for_provider_with_stream_complete():
    registry = ProviderRegistry()
    registry.register(ProviderCapability.LLM, "streaming", StreamingFakeProvider())

    assert registry.supports_streaming(ProviderCapability.LLM, "streaming") is True


def test_supports_streaming_false_for_provider_without_stream_complete():
    registry = ProviderRegistry()
    registry.register(ProviderCapability.LLM, "qwen", FakeProvider("qwen"))

    assert registry.supports_streaming(ProviderCapability.LLM, "qwen") is False


def test_supports_streaming_false_for_unregistered_provider_name():
    registry = ProviderRegistry()

    assert registry.supports_streaming(ProviderCapability.LLM, "missing") is False


async def test_stream_yields_chunks_from_the_named_provider():
    registry = ProviderRegistry()
    registry.register(ProviderCapability.LLM, "streaming", StreamingFakeProvider())

    chunks = [
        chunk
        async for chunk in registry.stream(
            ProviderCapability.LLM,
            ProviderRequest(messages=[{"role": "user", "content": "hi"}]),
            provider_name="streaming",
        )
    ]

    assert chunks == ["你好", "，世界"]


class ToolStreamingFakeProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text="不应该被用到")

    async def stream_complete_with_tools(self, request: ProviderRequest):
        from app.providers.base import ProviderStreamChunk

        for chunk in [ProviderStreamChunk(text="你好"), ProviderStreamChunk(text="，世界")]:
            yield chunk


def test_supports_tool_streaming_true_for_provider_with_stream_complete_with_tools():
    registry = ProviderRegistry()
    registry.register(ProviderCapability.LLM, "tool-streaming", ToolStreamingFakeProvider())

    assert registry.supports_tool_streaming(ProviderCapability.LLM, "tool-streaming") is True


def test_supports_tool_streaming_false_for_provider_without_it():
    registry = ProviderRegistry()
    registry.register(ProviderCapability.LLM, "streaming", StreamingFakeProvider())

    assert registry.supports_tool_streaming(ProviderCapability.LLM, "streaming") is False


def test_supports_tool_streaming_false_for_unregistered_provider_name():
    registry = ProviderRegistry()

    assert registry.supports_tool_streaming(ProviderCapability.LLM, "missing") is False


async def test_stream_with_tools_yields_chunks_from_the_named_provider():
    from app.providers.base import ProviderStreamChunk

    registry = ProviderRegistry()
    registry.register(ProviderCapability.LLM, "tool-streaming", ToolStreamingFakeProvider())

    chunks = [
        chunk
        async for chunk in registry.stream_with_tools(
            ProviderCapability.LLM,
            ProviderRequest(messages=[{"role": "user", "content": "hi"}]),
            provider_name="tool-streaming",
        )
    ]

    assert chunks == [ProviderStreamChunk(text="你好"), ProviderStreamChunk(text="，世界")]


async def test_stream_with_tools_raises_for_unregistered_provider_name():
    registry = ProviderRegistry()

    with pytest.raises(KeyError):
        async for _ in registry.stream_with_tools(
            ProviderCapability.LLM,
            ProviderRequest(messages=[{"role": "user", "content": "hi"}]),
            provider_name="missing",
        ):
            pass
