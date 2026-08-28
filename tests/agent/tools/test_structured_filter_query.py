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
                   llm_registry=None, question="") -> ToolContext:
    return ToolContext(
        tenant_id="t1", question=question,
        embedding_registry=None, embedding_provider_name="",
        vector_store=None, bm25_index=None,
        llm_registry=llm_registry, llm_provider_name="fake-llm",
        rerank_provider=None, query_rewrite_enabled=False,
        terms=terms or [], graph_client=graph_client,
        confirmed_relation_types=confirmed_relation_types or set(),
        term_type_schema=term_type_schema or {},
        allowed_combinations=allowed_combinations or [],
    )


def test_manifest_schema_exposes_query_intent_and_is_verbatim():
    raw = yaml.safe_load(_manifest_path().read_text(encoding="utf-8"))
    assert set(raw["parameters_schema"]["properties"]) == {"query_intent", "is_verbatim"}
    assert set(raw["parameters_schema"]["required"]) == {"query_intent", "is_verbatim"}
    # 深层机制（anchor/constraints/hops/matched_count）仍然不能出现在这份
    # 常驻 schema 里——渐进式披露的核心约束，见
    # docs/superpowers/specs/2026-08-25-progressive-disclosure-recall-augmented-params-design.md
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
        "默认原样填入用户当前问题的原文。只有当用户问题依赖前文指代"
        "（「它」「这个」「上面提到的」）或存在明显省略、脱离上下文无法独立"
        "执行时，才允许做最小改写——仅补全缺失的指代对象本身，不改写、"
        "不概括、不重新组织其余内容。补全后的句子必须完整保留用户当前"
        "问题里所有显式出现的措辞（尤其是「多少个/数量/一共/共有」这类"
        "计数用词、具体实体名、数值条件），禁止为了「更清楚」而概括或"
        "简化它们。如果当前问题本身已经完整、不依赖任何指代，直接原样"
        "返回，不要改写。"
    )
    assert raw["parameters_schema"]["properties"]["is_verbatim"]["description"] == (
        "true 表示 query_intent 就是用户当前问题的原文，未做任何改写；"
        "false 表示做了指代补全式的最小改写。默认应该是 true——只有当前"
        "问题确实依赖前文指代、脱离上下文无法独立执行时，才允许 false。"
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


async def test_resolve_arguments_includes_original_question_alongside_planner_intent():
    # 2026-08-27 真实案例回归：Planner（第一层 LLM，生成 tool_calls 参数）
    # 会把用户"coke-cola公司有多少个订单"这类问题改写成 query_intent="查询
    # Coca-Cola 这个产品的信息"，把计数意图丢掉——两次分别在 query_intent 的
    # schema 描述、trigger_cue 里加"必须保留计数措辞"的指令都没能让 Planner
    # 稳定照做（见 debug_trace.log 现场记录）。与其赌 Planner 的措辞，深层
    # 参数生成 prompt 必须始终原样带上 context.question（用户原始问题，
    # Planner 改写前的原文），让这一层 LLM 有机会从原文里自己发现计数意图，
    # 不完全依赖 query_intent 这个转述是否忠实。
    llm_registry = ProviderRegistry()
    provider = ScriptedLLMProvider([ProviderResult(text='{"anchor": {"term_type": "订单号"}}')])
    llm_registry.register(ProviderCapability.LLM, "fake-llm", provider)
    context = _base_context(llm_registry=llm_registry, question="coke-cola公司有多少个订单")

    await TOOL.resolve_arguments(
        {"query_intent": "查询 Coca-Cola 这个产品的信息"}, context=context,
    )

    prompt_content = provider.requests[0].messages[0]["content"]
    assert "coke-cola公司有多少个订单" in prompt_content
    assert "用户原始问题" in prompt_content


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
