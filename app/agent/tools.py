from __future__ import annotations

from typing import Any

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

STRUCTURED_FILTER_QUERY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "structured_filter_query_tool",
        "description": (
            "在知识图谱里查询实体——支持三种用法，可以组合使用：\n"
            "1. 已知实体名，查它是什么/关联着什么：anchor.name（会做别名模糊匹配）+ expand。\n"
            "2. 不知道具体实体名，按条件筛选一批满足条件的实体，"
            "适用于「有没有xx以上的」「比xx大的有哪些」「xx类目下有没有yy」"
            "「xx有多少个/数量是多少」这类问题：anchor.term_type + constraints。\n"
            "3. 上述两种可以叠加 expand，展开命中锚点的邻居关系。\n"
            "「xx类目/公司下有多少个yy」这类需要先确定xx是什么、再数yy数量的问题，"
            "通常需要 anchor.name 消歧 + constraints 筛选组合两次调用，"
            "或者一次调用里 anchor.term_type 直接按关系条件筛选（见 constraints.kind=relation）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "anchor": {
                    "type": "object",
                    "description": "起点定位方式，二选一",
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "已知的实体名称或别名"},
                                "type_hint": {
                                    "type": "string",
                                    "description": "该实体的类型（可选，同名实体存在多个类型时用于消歧）",
                                },
                            },
                            "required": ["name"],
                        },
                        {
                            "type": "object",
                            "properties": {
                                "term_type": {
                                    "type": "string",
                                    "description": "要筛选的实体类型（如 SKU、Product、Category），结果就是这个类型的实体列表",
                                },
                            },
                            "required": ["term_type"],
                        },
                    ],
                },
                "constraints": {
                    "type": "array",
                    "description": "过滤条件列表，条件之间是 AND 关系，可以为空（anchor.name 模式下留空表示不额外过滤，"
                                   "直接用解析出的锚点）。standard_name 字段的 eq/ne 比较值支持别名/模糊匹配，"
                                   "不要求填精确的标准名称——比如用户说的口语化名字可以直接填进来，系统会自动解析。",
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
                "expand": {
                    "type": ["object", "null"],
                    "description": "可选：展开命中锚点的邻居关系",
                    "properties": {
                        "hops": {"type": "integer", "enum": [1, 2], "description": "展开几跳，默认1"},
                        "relation_type": {
                            "type": ["string", "null"],
                            "description": "只展开这种关系类型；不传或传 null 表示任意类型",
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["outgoing", "incoming", "both"],
                            "description": "关系方向，默认 both",
                        },
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
            "required": ["anchor"],
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


async def structured_filter_query_tool(
    arguments: dict[str, Any],
    *,
    tenant_id: str,
    terms: list[Term],
    graph_client: GraphClientProtocol,
    confirmed_relation_types: set[str],
    term_type_schema: dict[str, TermTypeCategory],
) -> dict[str, Any]:
    """structured_filter_query_tool 的实际执行体，薄封装
    structured_filter_query.py::run_structured_filter_query。"""
    return await run_structured_filter_query(
        arguments, terms=terms, graph_client=graph_client, tenant_id=tenant_id,
        confirmed_relation_types=confirmed_relation_types, term_type_schema=term_type_schema,
    )
