from __future__ import annotations

from typing import Any, Protocol

from app.graphrag.ontology import Term
from app.graphrag.term_matcher import match_terms


class GraphClientProtocol(Protocol):
    async def query_subgraph(
        self, standard_name: str, *, tenant_id: str
    ) -> list[dict[str, Any]]: ...


async def build_term_guard_context(
    text: str,
    *,
    terms: list[Term],
    tenant_id: str,
    graph_client: GraphClientProtocol,
) -> str | None:
    """术语安全网：命中术语表则强制查图谱并生成上下文，未命中返回 None。

    这是架构文档第3节 TermGuard 节点的核心逻辑，先作为独立函数实现
    （未绑定具体的 Agent 框架），阶段4构建 LangGraph 状态图时将其包装
    为一个图节点，而不必现在就搭一个只有单个节点的临时状态图。

    tenant_id 透传给 query_subgraph，保证强制注入的图谱上下文只包含
    当前租户自己的关系事实，不会把其它租户的知识泄露进来。
    """
    matched = match_terms(text, terms)
    if not matched:
        return None

    lines = ["检测到以下专有名词，已强制注入知识图谱上下文（回答时请使用标准名称）："]
    for term in matched:
        lines.append(
            f"- {term.standard_name}（类型: {term.term_type}, 产品线: {term.product_line}）"
        )
        subgraph = await graph_client.query_subgraph(
            term.standard_name, tenant_id=tenant_id
        )
        for row in subgraph:
            lines.append(
                f"  关联: {row['related_name']}（关系: {row['relation_type']}）"
            )
    return "\n".join(lines)
