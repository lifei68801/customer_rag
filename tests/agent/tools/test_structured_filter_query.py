from pathlib import Path

import yaml

from app.agent.tool_registry import ToolContext
from app.agent.tools.structured_filter_query.tool import TOOL
from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import ExtraFieldSpec, TermTypeCategory
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry


class ScriptedLLMProvider:
    def __init__(self, responses: list[ProviderResult]) -> None:
        self._responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        self.requests.append(request)
        return self._responses.pop(0)


def _manifest_path() -> Path:
    import app.agent.tools.structured_filter_query as pkg
    return Path(pkg.__file__).parent / "manifest.yaml"


def _base_context(*, terms=None, term_type_schema=None, graph_client=None,
                   confirmed_relation_types=None, allowed_combinations=None,
                   llm_registry=None) -> ToolContext:
    return ToolContext(
        tenant_id="t1", question="",
        embedding_registry=None, embedding_provider_name="",
        vector_store=None, bm25_index=None,
        llm_registry=llm_registry, llm_provider_name="fake-llm",
        rerank_provider=None, query_rewrite_enabled=False,
        terms=terms or [], graph_client=graph_client,
        confirmed_relation_types=confirmed_relation_types or set(),
        term_type_schema=term_type_schema or {},
        allowed_combinations=allowed_combinations or [],
    )


def test_manifest_schema_only_exposes_query_intent():
    raw = yaml.safe_load(_manifest_path().read_text(encoding="utf-8"))
    assert set(raw["parameters_schema"]["properties"]) == {"query_intent"}
    for forbidden in ("anchor", "constraints", "hops", "matched_count"):
        assert forbidden not in raw["description"]


def test_manifest_description_trigger_cue_and_query_intent_match_content_exactly():
    """精确逐字符比对（不是子串检查）——manifest.yaml 里如果用 YAML 折叠
    标量（`>`）跨行写多行中文，换行处会被折叠成一个空格、结尾还会带一个
    多余的换行符，这跟原始 Python 字符串字面量拼接完全不是一回事（中文
    本来就不需要词间空格）。这条测试直接钉死三个字段的解析结果，任何
    回归（比如改回折叠标量）都会在这里失败，不依赖人工重新逐字核对。"""
    raw = yaml.safe_load(_manifest_path().read_text(encoding="utf-8"))
    assert raw["description"] == (
        "在知识图谱里查询实体数量/满足条件的实体列表——用自然语言描述"
        "你想查什么就行，不需要给出结构化参数，后续步骤会引导你把它"
        "转成实际能执行的查询。"
    )
    assert raw["trigger_cue"] == (
        "看到「多少个」「数量」等计数意图时，应该用 structured_filter_query_tool 给出确定数字，"
        "不能仅凭检索到的文档片段猜测，也不能因为一次调用没查到就直接放弃。"
    )
    assert raw["parameters_schema"]["properties"]["query_intent"]["description"] == (
        "用自然语言描述这次想查询/筛选的内容：想找什么类型的实体、"
        "有什么筛选条件、涉及哪些已知的名字。写得越具体、越自包含"
        "（把'它''这个'之类的指代词换成前面已经了解到的具体名字）"
        "越好——这句话会被用来检索本体里相关的术语和关系作为参考，"
        "帮你把接下来的实际查询参数填对。"
    )


async def test_resolve_arguments_triggers_recall_augmented_call():
    llm_registry = ProviderRegistry()
    provider = ScriptedLLMProvider([ProviderResult(text='{"anchor": {"term_type": "订单号"}}')])
    llm_registry.register(ProviderCapability.LLM, "fake-llm", provider)
    context = _base_context(llm_registry=llm_registry)

    resolved = await TOOL.resolve_arguments({"query_intent": "查一下订单号有多少个"}, context=context)

    assert resolved == {"anchor": {"term_type": "订单号"}}
    assert provider.requests[0].tools is None
    # 钉住"xx类目/公司下有多少个yy"复合查询策略段落——计划2 Task 2 曾经在
    # 从旧 tool description 搬迁到 STRUCTURED_FILTER_QUERY_USAGE_GUIDE 时
    # 意外丢过这段内容，这里从 planner.py 搬到 _USAGE_GUIDE 时同一类风险
    # 再次存在，用断言长期把关，不依赖人工每次检查。
    prompt_content = provider.requests[0].messages[0]["content"]
    assert "constraints.kind=relation" in prompt_content


async def test_execute_delegates_to_run_structured_filter_query():
    class _FakeGraphClient:
        async def execute_structured_filter_query(self, args, *, resolved, tenant_id, term_type_schema):
            return {"rows": [], "total_count": 0}

    context = _base_context(
        graph_client=_FakeGraphClient(),
        term_type_schema={"SKU": TermTypeCategory(
            value="SKU", extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
        )},
    )

    result, records = await TOOL.execute(
        {"anchor": {"term_type": "SKU"},
         "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}]},
        context=context,
    )

    assert result == {"matched_count": 0, "anchors": []}
    assert records == []


async def test_execute_resolves_name_anchor():
    terms = [Term(
        tenant_id="t1", node_key="示例错误码E502", standard_name="示例错误码E502",
        aliases=["网关超时示例"], term_type="error_code",
    )]

    class _FakeGraphClient:
        async def execute_structured_filter_query(self, args, *, resolved, tenant_id, term_type_schema):
            assert resolved.node_key == "示例错误码E502"
            return {"rows": [{
                "standard_name": "示例错误码E502", "node_key": "示例错误码E502",
                "term_type": "error_code", "all_properties": {},
            }], "total_count": 1}

    context = _base_context(
        terms=terms, graph_client=_FakeGraphClient(),
        term_type_schema={"error_code": TermTypeCategory(value="error_code", extra_fields=[])},
    )

    result, records = await TOOL.execute({"anchor": {"name": "网关超时示例"}}, context=context)

    assert result["matched_count"] == 1
    assert result["anchors"][0]["standard_name"] == "示例错误码E502"
    assert records == []
