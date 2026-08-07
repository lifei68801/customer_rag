from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry
from app.safety.semantic_review import semantic_safety_review


class FixedLLMProvider:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._text)


class FailingLLMProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        raise RuntimeError("provider unavailable")


def _registry(provider) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(ProviderCapability.LLM, "llm", provider)
    return registry


async def test_llm_flags_unsafe_content():
    result = await semantic_safety_review(
        "这是一段包含违规建议的内容",
        llm_registry=_registry(
            FixedLLMProvider('{"is_safe": false, "reason": "包含违规建议"}')
        ),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert result.is_safe is False
    assert result.reviewed is True


async def test_llm_confirms_safe_content():
    result = await semantic_safety_review(
        "重启路由器即可解决网络问题",
        llm_registry=_registry(FixedLLMProvider('{"is_safe": true, "reason": ""}')),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert result.is_safe is True
    assert result.reviewed is True


async def test_llm_failure_falls_back_to_unreviewed_but_not_blocked():
    result = await semantic_safety_review(
        "重启路由器即可解决网络问题",
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert result.is_safe is True
    assert result.reviewed is False


def test_system_prompt_mentions_internal_data_leakage():
    from app.safety.semantic_review import _SYSTEM_PROMPT

    assert "内部数据" in _SYSTEM_PROMPT or "内部信息" in _SYSTEM_PROMPT
