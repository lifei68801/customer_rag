from __future__ import annotations

import logging
from typing import Protocol

import aiosqlite

from app.graphrag.ontology_constraints import list_allowed_combinations

logger = logging.getLogger(__name__)


class GraphUnavailable(Exception):
    """探测图谱失败，生成不出可信的引导问题。

    跟"生成出零条"分开：零条是本体里没有可问的组合，图谱不通是故障。
    对终端用户两者都表现为空引导区；对管理员必须能分开。
    """

#: 自动生成的问题最多几条。跟手写那一档同一个量级——引导区超过五六条就
#: 没人读了，而它占的是首屏最值钱的位置。
DEFAULT_QUESTION_LIMIT = 4


class FanoutProbe(Protocol):
    async def probe_relation_fanout(
        self, *, tenant_id: str, relation_type: str, from_term_type: str,
        to_term_type: str, direction: str,
    ) -> int: ...


async def generate_questions(
    conn: aiosqlite.Connection,
    graph_client: FanoutProbe,
    *,
    tenant_id: str,
    limit: int = DEFAULT_QUESTION_LIMIT,
) -> list[str]:
    """从已确认本体生成引导问题，**只用图里真有边的类型组合**。

    只看本体不看图的话，一个刚建好还没导数据的租户会推荐一整屏答不出来的
    问题——用户点了产品自己推荐的问题却什么也没有。这是自伤，也是这个函数
    要 probe 一遍图的全部理由（见 spec D2 的裁决）。

    只用 status='confirmed'：草稿是还没定的东西，把它念给终端用户听等于
    把内部草稿泄露出去。

    扇出大的组合排前面。这里的扇出是 `probe_relation_fanout` 的定义——
    **单个主语沿这条关系最多能连到几个不同的宾语**，不是这类边的总条数
    （那个函数原本是给扇出陷阱检测用的，查的是 `max(count(DISTINCT b))`）。
    拿它当排序依据是个启发式：一个产品能连出 20 种口味，说明这条关系在
    这个租户里被填得很实，问出来大概率有话可说；而不是在说"这类边最多"。
    过滤用的 `> 0` 与这层含义无关——只要有一个主语连得出一个宾语，
    图里就确实有这种边。

    图谱查不通时抛 `GraphUnavailable`，**不降级成「跳过校验」**——那会恰恰在
    最可能出问题的时刻给出一屏保证答不出来的问题。

    抛而不是返回空列表：对终端用户来说两者一样（前台都是空引导区，那是
    诚实的），但对管理员不一样——「本体建好了还没导数据」和「图谱连不上」
    在后台编辑页里长得一模一样，而后者是唯一该看到故障的人。调用方据此
    把这两种空区分开。
    """
    combinations = await list_allowed_combinations(conn, tenant_id, status="confirmed")
    scored: list[tuple[int, str]] = []
    for combo in combinations:
        try:
            fanout = await graph_client.probe_relation_fanout(
                tenant_id=tenant_id,
                relation_type=combo.relation_type,
                from_term_type=combo.subject_term_type,
                to_term_type=combo.object_term_type,
                direction="outgoing",
            )
        except Exception:
            logger.warning(
                "租户 %r 的引导问题生成中止：探测关系 %s(%s→%s) 的图谱查询失败。"
                "不降级成跳过校验——那会给出一屏保证答不出来的问题",
                tenant_id, combo.relation_type,
                combo.subject_term_type, combo.object_term_type,
                exc_info=True,
            )
            raise GraphUnavailable(
                f"探测关系 {combo.relation_type}"
                f"({combo.subject_term_type}→{combo.object_term_type}) 的图谱查询失败"
            ) from None
        if fanout > 0:
            scored.append(
                (fanout, f"{combo.subject_term_type}有哪些{combo.object_term_type}？")
            )
    # 先按扇出倒序、再按问题文本正序：扇出相同时顺序必须是确定的，
    # 否则同一个租户每次刷新看到的引导问题顺序都不一样。
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [question for _, question in scored[:limit]]
