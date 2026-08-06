from app.memory.delay_intent import detect_delay_intent
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry


class FixedLLMProvider:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._text)


class FailingLLMProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        raise RuntimeError("provider 挂了")


def _registry(provider) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(ProviderCapability.LLM, "llm", provider)
    return registry


async def test_detects_delay_intent_when_llm_says_true():
    result = await detect_delay_intent(
        "我先按您说的重启路由器试试，不行再联系",
        llm_registry=_registry(FixedLLMProvider('{"is_delay": true}')),
        llm_provider_name="llm",
    )
    assert result is True


async def test_does_not_detect_delay_intent_for_normal_question():
    result = await detect_delay_intent(
        "网络连不上怎么办",
        llm_registry=_registry(FixedLLMProvider('{"is_delay": false}')),
        llm_provider_name="llm",
    )
    assert result is False


async def test_falls_back_to_rule_when_llm_fails():
    result = await detect_delay_intent(
        "我先试试，稍后再试",
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
    )
    assert result is True


async def test_rule_fallback_does_not_flag_normal_question():
    result = await detect_delay_intent(
        "网络连不上怎么办",
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
    )
    assert result is False
