from __future__ import annotations

from typing import Any, Protocol

from app.graphrag.ontology import Term

_SUBGRAPH_QUERY = """
MATCH (t:Term {standard_name: $standard_name})-[r]-(related:Term)
WHERE r.tenant_id = $tenant_id
RETURN related.standard_name AS related_name, type(r) AS relation_type, 1 AS hops

UNION

MATCH (t:Term {standard_name: $standard_name})-[r:REQUIRES|PRECEDES|PART_OF*2..2]-(related:Term)
WHERE ALL(rel IN r WHERE rel.tenant_id = $tenant_id)
RETURN related.standard_name AS related_name,
       [rel IN r | type(rel)][-1] AS relation_type,
       2 AS hops
"""
# 第二段 UNION 只对 REQUIRES/PRECEDES/PART_OF 这三种"链式"关系放开到
# 恰好 2 跳（*2..2，不是 *1..2，避免和第一段的 1 跳结果重复）——前提链、
# 流程顺序、包含层级经常需要连续追问两步；其余关系类型语义上查 1 跳就
# 有意义，继续放开多跳容易发散、引入噪声上下文。
#
# ALL(rel IN r WHERE rel.tenant_id = $tenant_id) 必须校验路径上每一条边
# 的租户归属，不能只查其中一条——:Term 标准节点本身不分租户、可能被
# 多个租户共用，如果只检查一跳，2 跳路径有可能"借道"另一个租户写入的边，
# 把不该出现的信息泄露给当前租户。这是本次改动里唯一一个如果实现疏忽
# 会导致真实安全问题的点。

# 关系类型白名单：10 种跨领域通用拓扑关系，刻意不含任何行业色彩（不是
# "错误码/模块"这类软件运维语义，也不是"房型/商品"这类某个垂直领域专属
# 语义）——领域信息由术语表的 term_type/product_line 字段承载，关系类型
# 词表本身保持跨租户通用。PART_OF 取代了旧的 BELONGS_TO_MODULE（语义
# 超集），本地无生产数据需要迁移，清理式切换，不写迁移脚本。
_ALLOWED_RELATION_TYPES = frozenset({
    "RELATED_TO",
    "PART_OF",
    "IS_A",
    "REQUIRES",
    "ALTERNATIVE_TO",
    "CAUSES",
    "ADDRESSED_BY",
    "LOCATED_IN",
    "APPLIES_TO",
    "PRECEDES",
})

# 关系边有向（MERGE (a)-[:TYPE]->(b)），按有向模式匹配删除保证每条边只
# 命中一次；r.source 只有 merge_relation 写入的抽取关系才有，sync_term/
# sync_terms 写入的 ALIAS_OF 边没有这个属性，天然不会被误删。
# ALIAS_OF 边（sync_term 写入）从不设置 tenant_id，这条按 r.tenant_id 精确
# 匹配的过滤天然把它们排除在外（Cypher 里 null = $tenant_id 恒为假）——
# 不需要额外按关系类型区分"这条边要不要按租户过滤"。
_DELETE_RELATIONS_BY_SOURCE_QUERY = """
MATCH ()-[r]->() WHERE r.source = $source AND r.tenant_id = $tenant_id
DELETE r
"""

# 别名节点用 alias_name 属性而不是 standard_name——避免和 _SUBGRAPH_QUERY
# 按 standard_name 精确匹配标准节点的查询模式产生歧义（别名节点本身不该被
# 当成标准节点查到）。
_SYNC_TERM_QUERY = """
MERGE (t:Term {standard_name: $standard_name})
SET t.type = $type, t.product_line = $product_line
WITH t
UNWIND $aliases AS alias_name
MERGE (a:Term {alias_name: alias_name})
MERGE (a)-[:ALIAS_OF]->(t)
"""


class Neo4jSessionProtocol(Protocol):
    async def run(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> Any: ...

    async def __aenter__(self) -> "Neo4jSessionProtocol": ...

    async def __aexit__(self, *args: object) -> None: ...


class Neo4jDriverProtocol(Protocol):
    def session(self) -> Neo4jSessionProtocol: ...


class Neo4jGraphClient:
    """Neo4j 图查询封装：给定标准术语名，返回与之相关的子图。

    别名到标准名的归一化在应用层（term_matcher）完成，这里只处理
    已归一化的标准名查询，保证返回给 LLM 的上下文使用统一的标准
    名称，而不是原样带入各种不同表述。
    """

    def __init__(self, *, driver: Neo4jDriverProtocol) -> None:
        self._driver = driver

    async def query_subgraph(
        self, standard_name: str, *, tenant_id: str
    ) -> list[dict[str, Any]]:
        async with self._driver.session() as session:
            result = await session.run(
                _SUBGRAPH_QUERY,
                {"standard_name": standard_name, "tenant_id": tenant_id},
            )
            return await result.data()

    async def merge_relation(
        self,
        *,
        subject_standard_name: str,
        object_standard_name: str,
        relation_type: str,
        source: str,
        tenant_id: str,
    ) -> None:
        """幂等写入一条术语间关系（MERGE，不存在则创建，存在则不重复）。

        source 记录这条边是从哪个文档抽取出来的，写在边的属性上——
        重新摄取同一文档前先按 source+tenant_id 删掉它写过的旧边（见
        delete_relations_by_source），避免文档内容变更后旧关系永久
        残留在图谱里，和 vector_store.delete_by_source() 是同一个思路。

        tenant_id 同样写在边的属性上，query_subgraph 按它强制过滤，
        防止不同租户的关系事实互相可见。
        """
        if relation_type not in _ALLOWED_RELATION_TYPES:
            raise ValueError(
                f"不允许的关系类型: {relation_type!r}，"
                f"仅支持: {sorted(_ALLOWED_RELATION_TYPES)}"
            )
        query = (
            "MERGE (a:Term {standard_name: $subject_name}) "
            "MERGE (b:Term {standard_name: $object_name}) "
            f"MERGE (a)-[r:{relation_type}]->(b) "
            "SET r.source = $source, r.tenant_id = $tenant_id"
        )
        async with self._driver.session() as session:
            await session.run(
                query,
                {
                    "subject_name": subject_standard_name,
                    "object_name": object_standard_name,
                    "source": source,
                    "tenant_id": tenant_id,
                },
            )

    async def delete_relations_by_source(self, source: str, *, tenant_id: str) -> None:
        """删除某个文档、某个租户抽取出的全部关系边，重新摄取该文档前调用。

        tenant_id 是必填过滤条件——不同租户即使摄取了相同相对路径的文档
        （source 字符串相同），也只会删自己那部分边，不会互相影响。
        """
        async with self._driver.session() as session:
            await session.run(
                _DELETE_RELATIONS_BY_SOURCE_QUERY,
                {"source": source, "tenant_id": tenant_id},
            )

    async def sync_term(self, term: Term) -> None:
        """把术语表里的一个标准术语同步进图谱：写入/更新标准节点的
        type/product_line 属性，并为每个别名建一个独立节点通过 ALIAS_OF
        指向标准节点——对应架构文档 §4.1"别名作为独立节点"的设计，是
        术语表（基准真相）到图谱的同步步骤，与 merge_relation（写入 LLM
        抽取出的关系边）是两条独立的写入路径。
        """
        async with self._driver.session() as session:
            await session.run(
                _SYNC_TERM_QUERY,
                {
                    "standard_name": term.standard_name,
                    "type": term.term_type,
                    "product_line": term.product_line,
                    "aliases": list(term.aliases),
                },
            )

    async def sync_terms(self, terms: list[Term]) -> None:
        for term in terms:
            await self.sync_term(term)
