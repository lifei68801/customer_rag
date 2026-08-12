import asyncio

from app.graphrag.llm_extractor import extract_candidate_relations
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry


class FixedLLMProvider:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._text)


class SpyLLMProvider:
    """记录收到的完整请求，用来断言 prompt 内容和多片段拼接格式。"""

    def __init__(self, text: str) -> None:
        self._text = text
        self.received_requests: list[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        self.received_requests.append(request)
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
        ["文档片段..."],
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
        ["文档片段..."],
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert relations == []


async def test_falls_back_to_empty_list_when_response_is_malformed_json():
    relations = await extract_candidate_relations(
        ["文档片段..."],
        llm_registry=_registry(FixedLLMProvider("这不是JSON")),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert relations == []


async def test_single_segment_is_sent_without_segment_markers():
    provider = SpyLLMProvider('{"relations": []}')

    await extract_candidate_relations(
        ["单独一个片段的文本"],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    user_message = provider.received_requests[0].messages[1]
    assert user_message["content"] == "单独一个片段的文本"


async def test_multiple_segments_are_joined_with_segment_markers():
    provider = SpyLLMProvider('{"relations": []}')

    await extract_candidate_relations(
        ["第一个片段", "第二个片段"],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    user_message = provider.received_requests[0].messages[1]
    assert "[片段1]\n第一个片段" in user_message["content"]
    assert "[片段2]\n第二个片段" in user_message["content"]


async def test_returns_empty_list_for_empty_segments_without_calling_llm():
    provider = SpyLLMProvider('{"relations": []}')

    relations = await extract_candidate_relations(
        [],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert relations == []
    assert provider.received_requests == []


async def test_system_prompt_lists_all_ten_relation_types_and_forbids_cross_segment_relations():
    provider = SpyLLMProvider('{"relations": []}')

    await extract_candidate_relations(
        ["片段"],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    system_message = provider.received_requests[0].messages[0]["content"]
    for relation_type in [
        "RELATED_TO", "PART_OF", "IS_A", "REQUIRES", "ALTERNATIVE_TO",
        "CAUSES", "ADDRESSED_BY", "LOCATED_IN", "APPLIES_TO", "PRECEDES",
    ]:
        assert relation_type in system_message
    assert "BELONGS_TO_MODULE" not in system_message
    assert "不要把不同片段里的实体强行关联起来" in system_message


async def test_system_prompt_requires_concrete_entities_and_excludes_generic_category_words():
    provider = SpyLLMProvider('{"relations": []}')

    await extract_candidate_relations(
        ["片段"],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    system_message = provider.received_requests[0].messages[0]["content"]
    assert "专有名词指在这门具体生意里有实际业务含义" in system_message
    for excluded_word in ["设备", "问题", "服务", "顾客", "流程"]:
        assert excluded_word in system_message
    assert "没有具体指代对象的泛称" in system_message


async def test_system_prompt_explains_taxonomy_examples_are_not_excluded():
    """回归测试：新加的实体范围约束不能和下面 10 种 relation_type 的例子
    自相矛盾——模型必须能同时看到"入住登记"这类词被列为合法专有名词的
    例子，和它在 PRECEDES 类型例子里被使用，不能一边说"不要抽类别/动作/
    状态词"一边又指望模型抽出这些类型。
    """
    provider = SpyLLMProvider('{"relations": []}')

    await extract_candidate_relations(
        ["片段"],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    system_message = provider.received_requests[0].messages[0]["content"]
    assert "入住登记" in system_message
    assert "IS_A" in system_message
    assert "PART_OF" in system_message
    assert "PRECEDES" in system_message
    assert "ADDRESSED_BY" in system_message
