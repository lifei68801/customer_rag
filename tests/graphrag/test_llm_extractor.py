import asyncio

from app.graphrag.llm_extractor import extract_candidate_relations
from app.graphrag.ontology_constraints import AllowedCombination
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


_DEFAULT_RELATION_TYPES = ["RELATED_TO", "ADDRESSED_BY"]
_DEFAULT_TERM_TYPES = ["error_code", "solution"]
_DEFAULT_ALLOWED_COMBINATIONS = [
    AllowedCombination(
        subject_term_type="error_code",
        relation_type="ADDRESSED_BY",
        object_term_type="solution",
    ),
]


async def test_extracts_relations_from_valid_json_response():
    text = (
        '{"relations": ['
        '{"subject": "错误码E502", "subject_type": "error_code", '
        '"object": "登录模块", "object_type": "solution", '
        '"relation_type": "RELATED_TO", '
        '"evidence": "出现错误码E502时请检查登录模块状态"}'
        "]}"
    )
    relations = await extract_candidate_relations(
        ["文档片段..."],
        llm_registry=_registry(FixedLLMProvider(text)),
        llm_provider_name="llm",
        relation_types=_DEFAULT_RELATION_TYPES,
        term_types=_DEFAULT_TERM_TYPES,
        allowed_combinations=_DEFAULT_ALLOWED_COMBINATIONS,
        timeout_sec=1.0,
    )

    assert relations == [
        {
            "subject": "错误码E502",
            "object": "登录模块",
            "relation_type": "RELATED_TO",
            "subject_type": "error_code",
            "object_type": "solution",
            "evidence": "出现错误码E502时请检查登录模块状态",
        }
    ]


async def test_falls_back_to_empty_list_when_llm_fails():
    relations = await extract_candidate_relations(
        ["文档片段..."],
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
        relation_types=_DEFAULT_RELATION_TYPES,
        term_types=_DEFAULT_TERM_TYPES,
        allowed_combinations=_DEFAULT_ALLOWED_COMBINATIONS,
        timeout_sec=1.0,
    )

    assert relations == []


async def test_falls_back_to_empty_list_when_response_is_malformed_json():
    relations = await extract_candidate_relations(
        ["文档片段..."],
        llm_registry=_registry(FixedLLMProvider("这不是JSON")),
        llm_provider_name="llm",
        relation_types=_DEFAULT_RELATION_TYPES,
        term_types=_DEFAULT_TERM_TYPES,
        allowed_combinations=_DEFAULT_ALLOWED_COMBINATIONS,
        timeout_sec=1.0,
    )

    assert relations == []


async def test_single_segment_is_sent_without_segment_markers():
    provider = SpyLLMProvider('{"relations": []}')

    await extract_candidate_relations(
        ["单独一个片段的文本"],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
        relation_types=_DEFAULT_RELATION_TYPES,
        term_types=_DEFAULT_TERM_TYPES,
        allowed_combinations=_DEFAULT_ALLOWED_COMBINATIONS,
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
        relation_types=_DEFAULT_RELATION_TYPES,
        term_types=_DEFAULT_TERM_TYPES,
        allowed_combinations=_DEFAULT_ALLOWED_COMBINATIONS,
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
        relation_types=_DEFAULT_RELATION_TYPES,
        term_types=_DEFAULT_TERM_TYPES,
        allowed_combinations=_DEFAULT_ALLOWED_COMBINATIONS,
        timeout_sec=1.0,
    )

    assert relations == []
    assert provider.received_requests == []


async def test_system_prompt_lists_tenant_relation_and_term_types_and_forbids_cross_segment_relations():
    provider = SpyLLMProvider('{"relations": []}')

    await extract_candidate_relations(
        ["片段"],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
        relation_types=["RELATED_TO", "ADDRESSED_BY", "IS_A"],
        term_types=["error_code", "solution", "module"],
        allowed_combinations=_DEFAULT_ALLOWED_COMBINATIONS,
        timeout_sec=1.0,
    )

    system_message = provider.received_requests[0].messages[0]["content"]
    for relation_type in ["RELATED_TO", "ADDRESSED_BY", "IS_A"]:
        assert relation_type in system_message
    for term_type in ["error_code", "solution", "module"]:
        assert term_type in system_message
    assert "不要把不同片段里的实体强行关联起来" in system_message


async def test_system_prompt_lists_allowed_combinations_as_subject_relation_object_triples():
    provider = SpyLLMProvider('{"relations": []}')

    await extract_candidate_relations(
        ["片段"],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
        relation_types=_DEFAULT_RELATION_TYPES,
        term_types=_DEFAULT_TERM_TYPES,
        allowed_combinations=[
            AllowedCombination(
                subject_term_type="error_code",
                relation_type="ADDRESSED_BY",
                object_term_type="solution",
            ),
        ],
        timeout_sec=1.0,
    )

    system_message = provider.received_requests[0].messages[0]["content"]
    assert "error_code ADDRESSED_BY solution" in system_message


async def test_system_prompt_explains_no_allowed_combinations_means_nothing_extracted():
    provider = SpyLLMProvider('{"relations": []}')

    await extract_candidate_relations(
        ["片段"],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
        relation_types=[],
        term_types=[],
        allowed_combinations=[],
        timeout_sec=1.0,
    )

    system_message = provider.received_requests[0].messages[0]["content"]
    assert "该租户尚未配置任何允许组合" in system_message


async def test_falls_back_to_empty_string_evidence_when_llm_omits_it():
    text = (
        '{"relations": ['
        '{"subject": "错误码E502", "object": "登录模块", "relation_type": "RELATED_TO"}'
        "]}"
    )
    relations = await extract_candidate_relations(
        ["文档片段..."],
        llm_registry=_registry(FixedLLMProvider(text)),
        llm_provider_name="llm",
        relation_types=_DEFAULT_RELATION_TYPES,
        term_types=_DEFAULT_TERM_TYPES,
        allowed_combinations=_DEFAULT_ALLOWED_COMBINATIONS,
        timeout_sec=1.0,
    )

    assert relations == [
        {
            "subject": "错误码E502",
            "object": "登录模块",
            "relation_type": "RELATED_TO",
            "subject_type": "",
            "object_type": "",
            "evidence": "",
        }
    ]


async def test_falls_back_to_empty_string_evidence_when_llm_returns_null():
    text = (
        '{"relations": ['
        '{"subject": "错误码E502", "object": "登录模块", "relation_type": "RELATED_TO", '
        '"evidence": null}'
        "]}"
    )
    relations = await extract_candidate_relations(
        ["文档片段..."],
        llm_registry=_registry(FixedLLMProvider(text)),
        llm_provider_name="llm",
        relation_types=_DEFAULT_RELATION_TYPES,
        term_types=_DEFAULT_TERM_TYPES,
        allowed_combinations=_DEFAULT_ALLOWED_COMBINATIONS,
        timeout_sec=1.0,
    )

    assert relations == [
        {
            "subject": "错误码E502",
            "object": "登录模块",
            "relation_type": "RELATED_TO",
            "subject_type": "",
            "object_type": "",
            "evidence": "",
        }
    ]


async def test_system_prompt_requires_evidence_quote_in_output_schema():
    provider = SpyLLMProvider('{"relations": []}')

    await extract_candidate_relations(
        ["片段"],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
        relation_types=_DEFAULT_RELATION_TYPES,
        term_types=_DEFAULT_TERM_TYPES,
        allowed_combinations=_DEFAULT_ALLOWED_COMBINATIONS,
        timeout_sec=1.0,
    )

    system_message = provider.received_requests[0].messages[0]["content"]
    assert '"evidence":"..."' in system_message
    assert "原文摘录" in system_message


async def test_extract_candidate_relations_parses_type_fields():
    fake_registry = _registry(
        FixedLLMProvider(
            '{"relations": ['
            '{"subject": "错误码E509", "subject_type": "error_code", '
            '"object": "重启路由器", "object_type": "solution", '
            '"relation_type": "ADDRESSED_BY", "evidence": "……"}'
            "]}"
        )
    )
    relations = await extract_candidate_relations(
        ["文档片段"],
        llm_registry=fake_registry,
        llm_provider_name="llm",
        relation_types=["ADDRESSED_BY"],
        term_types=["error_code", "solution"],
        allowed_combinations=[
            AllowedCombination(
                subject_term_type="error_code",
                relation_type="ADDRESSED_BY",
                object_term_type="solution",
            ),
        ],
        timeout_sec=1.0,
    )
    assert relations == [
        {
            "subject": "错误码E509",
            "subject_type": "error_code",
            "object": "重启路由器",
            "object_type": "solution",
            "relation_type": "ADDRESSED_BY",
            "evidence": "……",
        }
    ]


async def test_parses_relation_when_llm_omits_type_fields():
    """subject_type/object_type 缺失时不丢弃候选——下游确认范围校验会用
    空字符串匹配不到任何 allowed_combinations，自然降级转人工审核。"""
    text = (
        '{"relations": ['
        '{"subject": "错误码E502", "object": "登录模块", "relation_type": "RELATED_TO", '
        '"evidence": "证据句"}'
        "]}"
    )
    relations = await extract_candidate_relations(
        ["文档片段..."],
        llm_registry=_registry(FixedLLMProvider(text)),
        llm_provider_name="llm",
        relation_types=_DEFAULT_RELATION_TYPES,
        term_types=_DEFAULT_TERM_TYPES,
        allowed_combinations=_DEFAULT_ALLOWED_COMBINATIONS,
        timeout_sec=1.0,
    )

    assert relations == [
        {
            "subject": "错误码E502",
            "object": "登录模块",
            "relation_type": "RELATED_TO",
            "subject_type": "",
            "object_type": "",
            "evidence": "证据句",
        }
    ]
