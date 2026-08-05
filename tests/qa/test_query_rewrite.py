import asyncio

from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry
from app.qa.query_rewrite import rewrite_query


class FixedLLMProvider:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._text)


class FailingLLMProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        raise RuntimeError("provider unavailable")


class SlowLLMProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        await asyncio.sleep(10)
        return ProviderResult(text="不应该被用到")


def _registry(provider) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(ProviderCapability.LLM, "llm", provider)
    return registry


async def test_uses_rewritten_query_when_llm_succeeds():
    result = await rewrite_query(
        "登录不了怎么办",
        llm_registry=_registry(FixedLLMProvider("登录失败 认证模块 错误码")),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert result == "登录失败 认证模块 错误码"


async def test_falls_back_to_original_when_llm_raises():
    result = await rewrite_query(
        "登录不了怎么办",
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert result == "登录不了怎么办"


async def test_falls_back_to_original_when_llm_times_out():
    result = await rewrite_query(
        "登录不了怎么办",
        llm_registry=_registry(SlowLLMProvider()),
        llm_provider_name="llm",
        timeout_sec=0.05,
    )

    assert result == "登录不了怎么办"


async def test_falls_back_to_original_when_llm_returns_empty():
    result = await rewrite_query(
        "登录不了怎么办",
        llm_registry=_registry(FixedLLMProvider("   ")),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert result == "登录不了怎么办"
