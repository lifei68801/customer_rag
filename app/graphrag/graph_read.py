"""图谱读接缝——这个项目里唯一一个有两个真适配器的图谱接缝。

## 为什么单独有这个模块

图谱这一侧一共有六个协议：

| 协议 | 消费方 | 适配器 |
|---|---|---|
| `GraphReadProtocol`（本模块） | 问答 / Agent 工具 / 评测 | **Neo4j + Neptune，两个** |
| `GraphWriteProtocol`（neo4j_client） | 后台管理路由 | Neo4j 一个 |
| `GraphWriteClientProtocol`（normalization） | 抽取管道 | Neo4j 一个 |
| `SchemaEtlGraphProtocol`（schema_etl） | ETL 引擎 | Neo4j 一个 |
| `ReviewGraphClientProtocol`（review_queue） | 人工审核批准 | Neo4j 一个 |
| `RelationWriterProtocol`（relation_writer） | 上面三条写路径共用的 merge_relation | Neo4j 一个 |

**一个适配器只是假接缝，两个才是真的。** 按这条判据，六个里只有第一个是真
接缝——而在它搬到这里之前，它叫 `GraphClientProtocol`、住在
`term_guard.py`（一个具体特性模块）里，名字看不出它是那个真接缝；与此同时
读路径的几个消费方（`qa_routes` / `agent_routes` / `agent/graph.py`）反而
直接标注了具体的 `Neo4jGraphClient`——协议的分布和真实的变化点正好反着。

`NeptuneGraphClient` 的类 docstring 一直写着「满足 GraphClientProtocol」，
也就是说代码本来就知道 Neptune 填的是哪个槽，只是这件事没有名字。

## 两个适配器各自实现到什么程度

`NeptuneGraphClient` 真正实现的方法只有四个：`query_subgraph`、
`execute_structured_filter_query`、`ensure_tenant_scoped_schema`、
`probe_relation_fanout`。其余 15 个是显式的 `NotImplementedError` 存根
（每个都有测试钉住报错文案）。这四个里的前三个加上 `probe_relation_fanout`
恰好就是读路径要的——所以这个协议不是"挑了几个方法凑出来的"，它就是
Neptune 这个适配器今天真实的能力边界。

写路径想换后端，缺的不是一个协议，是第二个适配器。见
`app/api/deps.py::get_graph_writer`——那里把"这个后端不支持写"变成了一次
说得清的失败，而不是深在某个请求里的 15 种 NotImplementedError。
"""

from __future__ import annotations

from typing import Any, Protocol

from app.graphrag.ontology_categories import TermTypeCategory
from app.graphrag.structured_filter_query import ResolvedAnchor, StructuredFilterQueryArgs


class GraphReadProtocol(Protocol):
    """读路径要的全部图谱能力。两个适配器：Neo4jGraphClient、NeptuneGraphClient。

    `probe_relation_fanout` 名字像写、其实是读（探某个关系类型的扇出有多大），
    它在 `GraphWriteProtocol` 里也出现一次——那边是后台管理页面用的同一个
    探测。两处指的是同一个方法，不是两份实现。
    """

    async def query_subgraph(
        self, node_key: str, *, tenant_id: str, chain_query_relation_types: set[str]
    ) -> list[dict[str, Any]]: ...

    async def execute_structured_filter_query(
        self,
        args: StructuredFilterQueryArgs,
        *,
        resolved: ResolvedAnchor,
        tenant_id: str,
        term_type_schema: dict[str, TermTypeCategory],
    ) -> dict[str, Any]: ...

    async def probe_relation_fanout(
        self,
        *,
        tenant_id: str,
        relation_type: str,
        from_term_type: str,
        to_term_type: str,
        direction: str,
    ) -> int: ...
