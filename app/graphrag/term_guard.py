from __future__ import annotations

import asyncio
from typing import Any, Protocol

from app.graphrag.ontology import Term
from app.graphrag.term_matcher import match_terms


class GraphClientProtocol(Protocol):
    async def query_subgraph(
        self, standard_name: str, *, tenant_id: str
    ) -> list[dict[str, Any]]: ...


def describe_association(hops: int) -> str:
    """把 hops 转成人类可读的标签：1 跳是直接关联，其余（目前只有 2）
    是经过 N 跳推导出的间接关联。两个消费 query_subgraph 结果的地方
    （这里的强制注入路径、app/agent/planner.py 的 Agent 自主查询路径）
    共用同一份标签文案，不各自维护一份容易语义漂移的重复逻辑。
    """
    return "关联" if hops == 1 else f"间接关联（经过 {hops} 跳）"


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

    # 命中多个术语时，每个术语各自的 query_subgraph 调用彼此没有数据
    # 依赖——2026-08-10 起改成 asyncio.gather 并发查询，不再是 for 循环
    # 顺序 await。展示顺序仍然严格按 matched（术语表原始顺序）排列，不
    # 按查询完成的先后顺序，靠先 gather 再按索引组装文本实现，不是靠
    # "谁先跑完谁先加进 lines"。
    async def _query_one(term: Term) -> list[dict[str, Any]]:
        return await graph_client.query_subgraph(term.standard_name, tenant_id=tenant_id)

    subgraphs = await asyncio.gather(*(_query_one(term) for term in matched))

    lines = ["检测到以下专有名词，已强制注入知识图谱上下文（回答时请使用标准名称）："]
    for term, subgraph in zip(matched, subgraphs):
        lines.append(
            f"- {term.standard_name}（类型: {term.term_type}, 产品线: {term.product_line}）"
        )
        for row in subgraph:
            # hops 字段区分直接事实（1 跳）和推导出的间接事实（2 跳，只有
            # REQUIRES/PRECEDES/PART_OF 这类链式关系才会出现），标注清楚
            # 避免 LLM 把两者当同等确定性的信息——见
            # neo4j_client.py::query_subgraph 的 UNION 查询设计。
            hops = row.get("hops", 1)
            label = describe_association(hops)
            lines.append(
                f"  {label}: {row['related_name']}（关系: {row['relation_type']}）"
            )
    return "\n".join(lines)
