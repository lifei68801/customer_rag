import asyncio

from app.graphrag.llm_extractor import extract_candidate_relations
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


async def test_extracts_relations_from_valid_json_response():
    text = (
        '{"relations": ['
        '{"subject": "错误码E502", "object": "登录模块", "relation_type": "RELATED_TO"}'
        "]}"
    )
    relations = await extract_candidate_relations(
        "文档片段...",
        llm_registry=_registry(FixedLLMProvider(text)),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert relations == [
        {
            "subject": "错误码E502",
            "object": "登录模块",
            "relation_type": "RELATED_TO",
        }
    ]


async def test_falls_back_to_empty_list_when_llm_fails():
    relations = await extract_candidate_relations(
        "文档片段...",
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert relations == []


async def test_falls_back_to_empty_list_when_response_is_malformed_json():
    relations = await extract_candidate_relations(
        "文档片段...",
        llm_registry=_registry(FixedLLMProvider("这不是JSON")),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert relations == []
