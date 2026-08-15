from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Protocol

from app.graphrag.ontology import Term

_RELATION_TYPE_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}\Z")

_SUBGRAPH_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})-[r]-(related:Term)
WHERE r.tenant_id = $tenant_id
RETURN related.standard_name AS related_name, type(r) AS relation_type, 1 AS hops

UNION

MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})-[r:REQUIRES|PRECEDES|PART_OF*2..2]-(related:Term)
WHERE ALL(rel IN r WHERE rel.tenant_id = $tenant_id) AND related <> t
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
#
# AND related <> t 是自环守卫：Cypher 的关系唯一性规则只保证一条路径内
# 不重复使用同一条边，并不能阻止"去程用一条边、回程用另一条边"绕回起点
# ——关系抽取经常在同一对术语之间产出双向边（如 A-REQUIRES->B 又
# B-PART_OF->A），若不加这个过滤，2 跳查询会把 t 自己当成"与自己间接
# 关联"的结果返回。

# 关系类型白名单：10 种跨领域通用拓扑关系，刻意不含任何行业色彩（不是
# "错误码/模块"这类软件运维语义，也不是"房型/商品"这类某个垂直领域专属
# 语义）——领域信息由术语表的 term_type/product_line 字段承载，关系类型
# 词表本身保持跨租户通用。PART_OF 取代了旧的 BELONGS_TO_MODULE（语义
# 超集），本地无生产数据需要迁移，清理式切换，不写迁移脚本。这份白名单
# 同时是 merge_relation 里那条 Cypher f-string 插值的注入防线——Cypher
# 关系类型不能参数化绑定，全靠这里的校验挡掉非法值。
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
# 按 tenant_id/node_key 精确匹配标准节点的查询模式产生歧义（别名节点本身
# 不该被当成标准节点查到）。
_SYNC_TERM_QUERY = """
MERGE (t:Term {tenant_id: $tenant_id, node_key: $node_key})
SET t.standard_name = $standard_name, t.type = $type, t.product_line = $product_line
SET t += $extra_properties
WITH t
UNWIND $aliases AS alias_name
MERGE (a:Term {alias_name: alias_name})
MERGE (a)-[:ALIAS_OF]->(t)
"""

_COUNT_TERM_RELATION_EDGES_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})-[r]-()
WHERE type(r) <> 'ALIAS_OF'
RETURN count(r) AS edge_count
"""
# ALIAS_OF 是术语表→图谱的结构性同步边（sync_term 写入，见上面
# _SYNC_TERM_QUERY），不代表"这个术语已经出现在真实知识图谱数据里"；
# 删除前的守卫检查只关心 LLM 抽取/人工审核产出的关系边（merge_relation
# 写入的 RELATED_TO/PART_OF/... 这些），排除 ALIAS_OF 避免每个术语只要
# 有别名就永远无法删除。

_RENAME_TERM_NODE_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})
SET t.standard_name = $new_standard_name
"""
# node_key 不参与这条语句——ADR-0003 的核心断言：改名只更新展示属性
# standard_name，身份键 node_key 创建后永不改变。必须是对同一个节点
# 对象做属性 SET，不能先 DELETE 再 CREATE——Neo4j 的关系边挂在节点对象
# 上，不是按属性值查找的，原地改属性不会影响节点已有的任何关系边。

_DELETE_TERM_NODE_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})
OPTIONAL MATCH (a:Term)-[:ALIAS_OF]->(t)
DETACH DELETE t, a
"""
# 连同别名节点一起删——sync_term() 建的别名节点除了指向这个标准术语
# 没有其它用途，标准术语被删后别名节点留着就是纯垃圾数据。OPTIONAL
# MATCH 让"没有别名"的术语也能正常匹配到 t（DELETE 一个 null 值是
# Cypher 里的合法操作，不会报错）。

_ENSURE_INDEXES_QUERIES = [
    "CREATE INDEX IF NOT EXISTS term_tenant_node_key_idx FOR (t:Term) ON (t.tenant_id, t.node_key)",
    "CREATE INDEX IF NOT EXISTS term_tenant_term_type_idx FOR (t:Term) ON (t.tenant_id, t.type)",
]
# 所有节点共享同一个 :Term 标签（"多类型实体"是靠 term_type 取值模拟的，
# 不是原生多标签设计，见 docs/superpowers/specs/2026-08-15-etl-driven-
# schema-construction-design.md §3.4），按 tenant_id/node_key/term_type
# 过滤没有索引可用，量级大的租户（如 MUJI 的 SKU 18万+ 行）没有索引会
# 退化成全表扫描。

_BACKFILL_LEGACY_TERM_NODES_QUERY = """
MATCH (t:Term)
WHERE t.tenant_id IS NULL
SET t.tenant_id = 'default', t.node_key = t.standard_name
"""
# 一次性回填 2026-08-15 之前写入的、没有 tenant_id/node_key 属性的存量
# :Term 节点——WHERE t.tenant_id IS NULL 保证幂等，重复调用只会处理还没
# 打过标记的节点。别名节点（alias_name 属性）不参与这次回填：sync_term
# 的别名节点从来不设置 tenant_id/node_key/standard_name，这次改造不改变
# 别名节点的结构。


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
        self, node_key: str, *, tenant_id: str
    ) -> list[dict[str, Any]]:
        async with self._driver.session() as session:
            result = await session.run(
                _SUBGRAPH_QUERY,
                {"node_key": node_key, "tenant_id": tenant_id},
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
        provenance: str,
        recorded_at: datetime,
    ) -> None:
        """幂等写入一条术语间关系（MERGE，不存在则创建，存在则不重复）。

        source 记录这条边是从哪个文档抽取出来的，写在边的属性上——
        重新摄取同一文档前先按 source+tenant_id 删掉它写过的旧边（见
        delete_relations_by_source），避免文档内容变更后旧关系永久
        残留在图谱里，和 vector_store.delete_by_source() 是同一个思路。

        两端节点的 MERGE 匹配条件现在带 tenant_id——:Term 节点本次改造
        前不分租户、可能被多个租户共用，这是 docs/EXECUTION_PLAN.md 第9节
        列为"尚未做的"多租户隔离项之一，本次一并补齐：不这样做的话两个
        租户各自抽取出同一对术语间的关系时，会共用同一对 Neo4j 节点，
        产生跨租户数据污染。

        subject_standard_name/object_standard_name 在 extraction 数据
        接入模式下本身就是 node_key 的值（见 Global Constraints 的
        node_key 生成规则），因此这里直接把它们当 node_key 用，不改变
        这个函数对外的参数名/调用方传参方式——app/graphrag/
        normalization.py 和 review_queue.py 的现有调用点不用改。

        tenant_id 必须写进 MERGE 的匹配模式本身（不能只在匹配到之后才
        SET）——:Term 标准节点不分租户、可能被多个租户共用，如果匹配
        条件只看 (a, 关系类型, b) 不看 tenant_id，两个租户各自抽取出同一对
        标准术语间的同类型关系时，第二次 merge_relation 会命中并覆盖第一
        个租户写的那条边（同一条边的 tenant_id/source/provenance 被悄悄
        改写成后来者的），而不是各自新建一条边——这是 2026-08-12 修的
        真实跨租户数据覆盖问题，不是假设性风险。

        provenance 标记这条边是怎么进来的（app/graphrag/provenance.py 的
        AUTO_MERGED："摄取时术语表精确对齐后自动写入"，或
        HUMAN_APPROVED："未对齐候选经人工审核批准后写入"）；recorded_at
        是这次写入发生的时间。两者都只是可观测性字段，不参与
        query_subgraph 的检索过滤——检索侧目前仍然不区分来源，一视同仁
        地返回，这是刻意保留的现状（见该模块的说明），加这两个字段只是
        让"这条边有没有被人看过"这件事变得可事后追查。
        """
        if relation_type not in _ALLOWED_RELATION_TYPES:
            raise ValueError(
                f"不允许的关系类型: {relation_type!r}，"
                f"仅支持: {sorted(_ALLOWED_RELATION_TYPES)}"
            )
        query = (
            "MERGE (a:Term {tenant_id: $tenant_id, node_key: $subject_name}) "
            "MERGE (b:Term {tenant_id: $tenant_id, node_key: $object_name}) "
            f"MERGE (a)-[r:{relation_type} {{tenant_id: $tenant_id}}]->(b) "
            "SET r.source = $source, r.provenance = $provenance, "
            "r.recorded_at = $recorded_at"
        )
        async with self._driver.session() as session:
            await session.run(
                query,
                {
                    "subject_name": subject_standard_name,
                    "object_name": object_standard_name,
                    "source": source,
                    "tenant_id": tenant_id,
                    "provenance": provenance,
                    "recorded_at": recorded_at.strftime("%Y-%m-%d %H:%M:%S"),
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
                    "tenant_id": term.tenant_id,
                    "node_key": term.node_key,
                    "standard_name": term.standard_name,
                    "type": term.term_type,
                    "product_line": term.product_line,
                    "aliases": list(term.aliases),
                    "extra_properties": term.extra_properties,
                },
            )

    async def sync_terms(self, terms: list[Term]) -> None:
        for term in terms:
            await self.sync_term(term)

    async def count_relation_edges_for_term(self, *, tenant_id: str, node_key: str) -> int:
        """统计该术语节点参与的、非结构性同步边（ALIAS_OF）的关系边数量，
        供管理后台删除术语前的守卫检查用——见 _COUNT_TERM_RELATION_EDGES_QUERY
        的说明。"""
        async with self._driver.session() as session:
            result = await session.run(
                _COUNT_TERM_RELATION_EDGES_QUERY,
                {"tenant_id": tenant_id, "node_key": node_key},
            )
            rows = await result.data()
            return rows[0]["edge_count"] if rows else 0

    async def rename_term_node(
        self, *, tenant_id: str, node_key: str, new_standard_name: str
    ) -> None:
        """把一个术语节点的 standard_name 属性原地改成新值，不影响节点
        已有的关系边、也不改变 node_key——见 _RENAME_TERM_NODE_QUERY 的
        说明。调用方必须自己先确认 new_standard_name 不会跟同租户下另一个
        已存在的术语节点冲突。"""
        async with self._driver.session() as session:
            await session.run(
                _RENAME_TERM_NODE_QUERY,
                {
                    "tenant_id": tenant_id,
                    "node_key": node_key,
                    "new_standard_name": new_standard_name,
                },
            )

    async def delete_term_node(self, *, tenant_id: str, node_key: str) -> None:
        """删除一个术语节点及其别名节点——只应该在确认过
        count_relation_edges_for_term() 返回 0 之后调用，见
        _DELETE_TERM_NODE_QUERY 的说明。"""
        async with self._driver.session() as session:
            await session.run(
                _DELETE_TERM_NODE_QUERY, {"tenant_id": tenant_id, "node_key": node_key}
            )

    async def ensure_tenant_scoped_schema(self) -> None:
        """建按租户/节点键、按租户/分类的属性索引，并把存量（本次改造前
        写入、没有 tenant_id/node_key 属性的）:Term 节点回填成
        tenant_id='default'——与 SQLite 侧 terms 表的迁移是同一次改造的
        两半，缺一半就会出现"SQLite 里租户隔离了，Neo4j 里还是老样子"
        的不一致状态。幂等，可在每次进程启动时调用。
        """
        async with self._driver.session() as session:
            for query in _ENSURE_INDEXES_QUERIES:
                await session.run(query)
            await session.run(_BACKFILL_LEGACY_TERM_NODES_QUERY)

    async def migrate_relation_type_edges(
        self, *, tenant_id: str, old_type: str, new_type: str
    ) -> int:
        """把某个租户所有旧类型的边批量改成新类型，返回迁移的边数。

        Neo4j 的关系类型（edge type）一旦写入不可原地修改——"改名"只能新建一条
        新类型的边、把原边的全部属性复制过去、再删掉旧边，这是一次真正的数据
        迁移，不是字符串替换（见 app/graphrag/ontology_relations.py 改名逻辑的
        说明）。这是租户级自定义关系类型改名后，业务显式触发的可选操作——不改名
        的旧边永远留着旧类型字符串也完全可用（query_subgraph 的 1 跳查询不按
        类型过滤），触发这个方法只是为了让图谱里同一语义不再同时存在新旧两种
        类型字符串。

        单条 Cypher 语句一次性处理该租户全部旧类型的边，不做分批——当前没有
        证据支撑单租户单次改名会涉及大量边到需要分批的程度，等真实场景出现
        性能问题再引入分批处理（YAGNI）。
        """
        if not _RELATION_TYPE_NAME_PATTERN.match(old_type):
            raise ValueError(f"旧关系类型名字不合法: {old_type!r}")
        if not _RELATION_TYPE_NAME_PATTERN.match(new_type):
            raise ValueError(f"新关系类型名字不合法: {new_type!r}")
        query = (
            f"MATCH (a)-[r:{old_type} {{tenant_id: $tenant_id}}]->(b) "
            "WITH a, b, r, properties(r) AS props "
            f"CREATE (a)-[r2:{new_type}]->(b) "
            "SET r2 = props "
            "WITH r, r2 "
            "DELETE r "
            "RETURN count(r2) AS migrated_count"
        )
        async with self._driver.session() as session:
            result = await session.run(query, {"tenant_id": tenant_id})
            rows = await result.data()
        return rows[0]["migrated_count"] if rows else 0
