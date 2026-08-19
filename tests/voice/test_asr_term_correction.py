from app.graphrag.ontology import Term
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry
from app.voice.asr_term_correction import correct_asr_terms

_TERMS = [
    Term(
        tenant_id="t1",
        node_key="示例错误码E502",
        standard_name="示例错误码E502",
        aliases=["网关超时示例"],
        term_type="error_code",
    ),
]


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


async def test_replaces_fuzzy_match_when_llm_confirms():
    text = "我这边报了网关超时是例，麻烦看下"
    llm_text = '{"replacements": [{"span": "网关超时是例", "standard_name": "网关超时示例", "replace": true}]}'

    corrected = await correct_asr_terms(
        text,
        terms=_TERMS,
        llm_registry=_registry(FixedLLMProvider(llm_text)),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert corrected == "我这边报了网关超时示例，麻烦看下"


async def test_keeps_original_text_when_llm_fails():
    text = "我这边报了网关超时是例，麻烦看下"

    corrected = await correct_asr_terms(
        text,
        terms=_TERMS,
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert corrected == text


async def test_no_change_when_no_fuzzy_candidates_found():
    text = "今天天气怎么样"

    corrected = await correct_asr_terms(
        text,
        terms=_TERMS,
        llm_registry=_registry(FixedLLMProvider("不应该被调用")),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert corrected == text
