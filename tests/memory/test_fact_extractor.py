from app.memory.fact_extractor import extract_facts
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry


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


async def test_extracts_facts_from_valid_json_response():
    facts = await extract_facts(
        user_input="我们公司用的是企业版套餐",
        assistant_output="好的，已记录",
        llm_registry=_registry(FixedLLMProvider('{"facts": ["客户使用企业版套餐"]}')),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert facts == ["客户使用企业版套餐"]


async def test_falls_back_to_empty_list_when_llm_fails():
    facts = await extract_facts(
        user_input="我们公司用的是企业版套餐",
        assistant_output="好的，已记录",
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert facts == []
