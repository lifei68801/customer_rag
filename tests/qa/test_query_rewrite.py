import asyncio

from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry
from app.qa.query_rewrite import rewrite_query


class FixedLLMProvider:
    def __init__(self, text: str) -> None:
        self._text = text
        self.last_request: ProviderRequest | None = None

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        self.last_request = request
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


async def test_passes_conversation_context_to_the_rewrite_llm_call():
    # 客服口语化提问常有指代（"这个报错"），改写需要看到近期对话轮次
    # 才能补全指代，不能只看孤立的一句话。
    provider = FixedLLMProvider("网关超时错误码E502")
    conversation_context = [
        {"role": "user", "content": "我遇到了E502错误"},
        {"role": "assistant", "content": "E502是网关超时错误。"},
    ]

    result = await rewrite_query(
        "这个报错怎么解决",
        llm_registry=_registry(provider),
        llm_provider_name="llm",
        timeout_sec=1.0,
        conversation_context=conversation_context,
    )

    assert result == "网关超时错误码E502"
    assert provider.last_request is not None
    assert conversation_context[0] in provider.last_request.messages
    assert conversation_context[1] in provider.last_request.messages


async def test_conversation_context_is_optional():
    result = await rewrite_query(
        "登录不了怎么办",
        llm_registry=_registry(FixedLLMProvider("登录失败")),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert result == "登录失败"
