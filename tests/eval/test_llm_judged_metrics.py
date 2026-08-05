from app.eval.llm_judged_metrics import score_answer_relevancy, score_faithfulness
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


async def test_score_faithfulness_parses_llm_score():
    score = await score_faithfulness(
        answer="重启路由器即可解决",
        context="网络断开时请先重启路由器",
        llm_registry=_registry(FixedLLMProvider('{"score": 0.9}')),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert score == 0.9


async def test_score_faithfulness_returns_none_when_llm_fails():
    score = await score_faithfulness(
        answer="重启路由器即可解决",
        context="网络断开时请先重启路由器",
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert score is None


async def test_score_answer_relevancy_parses_llm_score():
    score = await score_answer_relevancy(
        question="网络连不上怎么办？",
        answer="重启路由器即可解决",
        llm_registry=_registry(FixedLLMProvider('{"score": 0.8}')),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert score == 0.8


async def test_score_answer_relevancy_returns_none_when_llm_fails():
    score = await score_answer_relevancy(
        question="网络连不上怎么办？",
        answer="重启路由器即可解决",
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert score is None
