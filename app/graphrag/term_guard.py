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
    # match_terms 是纯 CPU 计算（对整个术语表做模糊匹配），可能有实际
    # 耗时——包一层 asyncio.to_thread，避免它在事件循环上同步跑完才把
    # 控制权交还。build_term_guard_context() 会和 hybrid_search() 并发
    # 跑（见 app/qa/answer.py::answer_question），不这样做的话这段 CPU
    # 时间会拖慢 hybrid_search() 本该立刻发出的第一次网络请求，抵消掉
    # 并发本来要换来的收益。
    matched = await asyncio.to_thread(match_terms, text, terms)
    if not matched:
        return None

    # 命中多个术语时，每个术语各自的 query_subgraph 调用彼此没有数据
    # 依赖——2026-08-10 起改成 asyncio.gather 并发查询，不再是 for 循环
    # 顺序 await。展示顺序仍然严格按 matched 排列（术语表现在按
    # standard_name 字母序排列——terms 表的 list_terms() 用
    # ORDER BY standard_name），不按查询完成的先后顺序，靠先 gather 再
    # 按索引组装文本实现，不是靠"谁先跑完谁先加进 lines"。
    #
    # 并发数用一个小 Semaphore（8）兜底：match_terms 命中数量取决于文本
    # 长度和术语表规模，理论上没有上限，不限制在极端情况下（一段话命中
    # 几十个术语）可能让并发查询数超过 Neo4j 驱动的连接池上限
    # （max_connection_pool_size 默认 100），造成连接获取超时。8 给得
    # 很宽松，正常场景（命中几个术语）不会受这层节流影响。
    #
    # 用 return_exceptions=True 等所有查询都跑完（不管成败）再决定要不要
    # 重新抛出，而不是用 gather 默认行为——默认行为下一个查询失败会让
    # gather 立刻返回，其它还在执行的查询变成没人处理的后台任务。这里
    # 等全部落地再抛，语义上仍然和改造前一致：任一术语的图谱查询失败都
    # 让整个 build_term_guard_context() 失败。
    query_semaphore = asyncio.Semaphore(8)

    async def _query_one(term: Term) -> list[dict[str, Any]]:
        async with query_semaphore:
            return await graph_client.query_subgraph(
                term.node_key, tenant_id=tenant_id
            )

    results = await asyncio.gather(
        *(_query_one(term) for term in matched), return_exceptions=True
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    subgraphs = results

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
