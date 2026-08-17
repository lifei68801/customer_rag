from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.graphrag.normalization import resolve_to_standard_name
from app.graphrag.ontology import Term
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
                "limit": {"type": "integer", "description": "返回结果的最大条数，默认20"},
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
) -> GraphQueryToolResult:
    """graph_query_tool 的实际执行体：先对齐术语表，命中才查图谱。

    未命中术语表时直接返回 resolved=False，不发起图查询——和
    normalize_and_write_relations 的"先归一化再写入"是同一个原则：
    没有标准名就没有查询的意义，也避免拿一个不存在的名字去查图谱浪费一次调用。

    tenant_id 透传给 query_subgraph，防止返回给 LLM 的子图里混入其它
    租户的关系事实。
    """
    standard_name = resolve_to_standard_name(entity_name, terms)
    if standard_name is None:
        return GraphQueryToolResult(resolved=False, standard_name=None, subgraph=[])

    # resolve_to_standard_name 返回的是展示名，查图谱要用稳定身份键——
    # 改名后 standard_name 会变但 node_key 不变（ADR-0003），从已加载的
    # terms 列表里按 standard_name 反查对应的 node_key，不改
    # resolve_to_standard_name 本身（它是抽取管道 normalization.py 也在
    # 用的共享函数，签名不在本计划改动范围内）。
    node_key = next(t.node_key for t in terms if t.standard_name == standard_name)
    subgraph = await graph_client.query_subgraph(node_key, tenant_id=tenant_id)
    return GraphQueryToolResult(
        resolved=True, standard_name=standard_name, subgraph=subgraph
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
