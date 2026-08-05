from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.graphrag.normalization import resolve_to_standard_name
from app.graphrag.ontology import Term
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
    graph_client: GraphClientProtocol,
) -> GraphQueryToolResult:
    """graph_query_tool 的实际执行体：先对齐术语表，命中才查图谱。

    未命中术语表时直接返回 resolved=False，不发起图查询——和
    normalize_and_write_relations 的"先归一化再写入"是同一个原则：
    没有标准名就没有查询的意义，也避免拿一个不存在的名字去查图谱浪费一次调用。
    """
    standard_name = resolve_to_standard_name(entity_name, terms)
    if standard_name is None:
        return GraphQueryToolResult(resolved=False, standard_name=None, subgraph=[])

    subgraph = await graph_client.query_subgraph(standard_name)
    return GraphQueryToolResult(
        resolved=True, standard_name=standard_name, subgraph=subgraph
    )
