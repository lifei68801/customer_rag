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
