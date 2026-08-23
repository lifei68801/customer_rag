from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.graphrag.ontology import Term, resolve_term
from app.graphrag.ontology_categories import TermTypeCategory
from app.graphrag.structured_filter_query import run_structured_filter_query
from app.graphrag.term_guard import GraphClientProtocol
from app.providers.embedding import EmbeddingRegistry
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider
from app.retrieval.bm25 import BM25Index
from app.retrieval.hybrid_search import hybrid_search
from app.retrieval.vector_store import VectorRecord, VectorStore

# OpenAI function-calling 格式的工具 schema。刻意不在 properties 里暴露 tenant_id——
# 隔离维度只能由系统层（tool_call_node）从 AgentState 注入，不能是 LLM 可控参数
# （见 docs/AGENT_PLANNER_DESIGN.md §4.2）。

VECTOR_SEARCH_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "vector_search_tool",
        "description": (
            "在企业知识库中做混合检索（向量+关键词），返回相关文档片段。"
            "当需要补充事实性资料来回答用户问题时调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索查询语句，可以是用户问题本身或其改写/子问题",
                },
            },
            "required": ["query"],
        },
    },
}

GRAPH_QUERY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "graph_query_tool",
        "description": (
            "查询知识图谱中某个专有名词/实体的标准名称及其关联关系。"
            "当用户提到的实体名称不确定是否为标准写法、或需要了解其关联实体时调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entity_name": {
                    "type": "string",
                    "description": "待查询的实体名称或别名",
                },
                "entity_type": {
                    "type": "string",
                    "description": (
                        "该实体的类型（如果能从对话上下文推断出来，比如用户明确说的是"
                        "\"这个产品\"还是\"这个类目\"）。不确定就不传，系统会在名字本身"
                        "不重复时正常解析；如果同名实体有多个类型，不传可能导致查询失败，"
                        "此时应重新以更明确的表述询问用户后再调用。"
                    ),
                },
            },
            "required": ["entity_name"],
        },
    },
}

STRUCTURED_FILTER_QUERY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "structured_filter_query_tool",
        "description": (
            "按结构化条件（数值区间、精确匹配、关系约束）在知识图谱里筛选满足条件的实体，"
            "适用于「有没有xx以上的」「比xx大的有哪些」「xx类目下有没有yy」这类不知道具体"
            "实体名、需要按条件查找的问题。与 graph_query_tool 不同：graph_query_tool 用于"
            "已知实体名、查它的关联信息；本工具用于按条件反查一批满足条件的实体。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "anchor_term_type": {
                    "type": "string",
                    "description": "要筛选的实体类型（如 SKU、Product、Category），结果就是这个类型的实体列表",
                },
                "constraints": {
                    "type": "array",
                    "description": "过滤条件列表，条件之间是 AND 关系，至少提供一个",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": ["attribute", "relation"],
                                "description": "attribute：直接比较锚点实体自己的字段；relation：经过关系跳到目标实体再比较",
                            },
                            "field": {
                                "type": "string",
                                "description": "kind=attribute 时必填：要比较的字段名（standard_name 或该实体类型已声明的属性字段名）",
                            },
                            "operator": {
                                "type": "string",
                                "enum": ["gt", "gte", "lt", "lte", "eq", "ne", "starts_with",
                                         "all_lte", "all_gte", "any_lte", "any_gte"],
                                "description": "比较运算符，实际可用范围取决于字段类型",
                            },
                            "value": {"description": "kind=attribute 时必填：比较的目标值"},
                            "hops": {
                                "type": "array",
                                "description": "kind=relation 时必填：从锚点出发的关系跳数组，最多2跳",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "relation_type": {"type": "string", "description": "关系类型，如 HAS_VARIANT"},
                                        "direction": {"type": "string", "enum": ["outgoing", "incoming"]},
                                        "target_term_type": {"type": "string", "description": "这一跳到达的实体类型"},
                                    },
                                    "required": ["relation_type", "direction", "target_term_type"],
                                },
                            },
                            "target_field": {
                                "type": "string",
                                "description": "kind=relation 时必填：在最后一跳到达的实体上比较哪个字段",
                            },
                            "target_operator": {
                                "type": "string",
                                "enum": ["gt", "gte", "lt", "lte", "eq", "ne", "starts_with",
                                         "all_lte", "all_gte", "any_lte", "any_gte"],
                                "description": "kind=relation 时必填：对 target_field 用的运算符",
                            },
                            "target_value": {"description": "kind=relation 时必填：比较的目标值"},
                        },
                        "required": ["kind"],
                    },
                },
                "group_by": {
                    "type": ["object", "null"],
                    "description": "可选：按某个字段做 distinct 值统计而不是返回实体列表本身",
                    "properties": {
                        "constraint_index": {
                            "type": "integer",
                            "description": "指向 constraints 数组里某个 kind=relation 约束的下标，按它的 target_field 分组",
                        },
                    },
                },
                "limit": {
                    "type": "integer",
                    "description": "返回结果的最大条数，默认20——预期命中数量较多时"
                                   "（如宽泛的数值区间过滤），请设置一个合理的值避免返回过多结果",
                },
            },
            "required": ["anchor_term_type", "constraints"],
        },
    },
}


async def vector_search_tool(
    query: str,
    *,
    tenant_id: str,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    bm25_index: BM25Index,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    rerank_provider: RerankProvider | None = None,
    query_rewrite_enabled: bool = True,
    top_k: int = 3,
) -> list[VectorRecord]:
    """vector_search_tool 的实际执行体，薄封装 hybrid_search。

    tenant_id 是关键字专属参数，只能由调用方（tool_call_node）从
    AgentState 传入，不出现在 VECTOR_SEARCH_TOOL_SCHEMA 里，LLM 无法控制。
    """
    return await hybrid_search(
        query,
        embedding_registry=embedding_registry,
        embedding_provider_name=embedding_provider_name,
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name=llm_provider_name,
        tenant_id=tenant_id,
        rerank_provider=rerank_provider,
        query_rewrite_enabled=query_rewrite_enabled,
        final_top_k=top_k,
    )


@dataclass(frozen=True)
class GraphQueryToolResult:
    resolved: bool
    standard_name: str | None
    subgraph: list[dict[str, Any]]


async def graph_query_tool(
    entity_name: str,
    *,
    terms: list[Term],
    tenant_id: str,
    graph_client: GraphClientProtocol,
    entity_type: str | None = None,
) -> GraphQueryToolResult:
    """graph_query_tool 的实际执行体：先对齐术语表，命中才查图谱。

    未命中术语表时直接返回 resolved=False，不发起图查询——和
    normalize_and_write_relations 的"先归一化再写入"是同一个原则：
    没有标准名就没有查询的意义，也避免拿一个不存在的名字去查图谱浪费一次调用。

    entity_type 是可选的类型提示（LLM 从对话上下文推断，见
    GRAPH_QUERY_TOOL_SCHEMA 的字段说明）：传了且该类型下确实存在这个
    entity_name（本身或别名），就精确解析到那一条；没传、或者传了但
    该类型下没有，退回"entity_name 作为标准名或别名在全部术语里是否
    唯一"——唯一就照样解析成功，不唯一就返回 resolved=False，不会像
    2026-08-22 之前那样在多个同名不同类型的实体里随便选一个回答给客户
    （那样可能答非所问，是本次改动要消除的风险）。

    用 `app.graphrag.ontology.resolve_term`——LLM 抽取归一化
    （normalization.py）、人工审核批准（review_queue.py）、这里三处
    "按名字查 Term"的调用路径 2026-08-23 起统一复用同一个函数、同一套
    判重规则（见 resolve_term 的文档），不再各自实现或调用不同的消歧
    逻辑，也不再需要像之前那样为了避开两套规则互相打架的坑，导入另一个
    模块的私有函数。graph_query_tool 的 entity_name 可能是别名（不止是
    标准名，见 GRAPH_QUERY_TOOL_SCHEMA 的字段说明），resolve_term 天然
    支持按 name-or-alias 解析，一次查找同时拿到 standard_name 和
    node_key。

    tenant_id 透传给 query_subgraph，防止返回给 LLM 的子图里混入其它
    租户的关系事实。
    """
    term = resolve_term(entity_name, terms, term_type_hint=entity_type)
    if term is None:
        return GraphQueryToolResult(resolved=False, standard_name=None, subgraph=[])

    # term.node_key 和 term.standard_name 来自同一次查找、同一个 Term
    # 对象——改名后 standard_name 会变但 node_key 不变（ADR-0003），查
    # 图谱必须用稳定的 node_key，不能用展示名。
    subgraph = await graph_client.query_subgraph(term.node_key, tenant_id=tenant_id)
    return GraphQueryToolResult(
        resolved=True, standard_name=term.standard_name, subgraph=subgraph
    )


async def structured_filter_query_tool(
    arguments: dict[str, Any],
    *,
    tenant_id: str,
    graph_client: GraphClientProtocol,
    confirmed_relation_types: set[str],
    term_type_schema: dict[str, TermTypeCategory],
) -> dict[str, Any]:
    """structured_filter_query_tool 的实际执行体，薄封装
    structured_filter_query.py::run_structured_filter_query。"""
    return await run_structured_filter_query(
        arguments, graph_client=graph_client, tenant_id=tenant_id,
        confirmed_relation_types=confirmed_relation_types, term_type_schema=term_type_schema,
    )
