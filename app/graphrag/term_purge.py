"""按实体类型**彻底清空**一个租户的数据：词表行、编辑层、周边记录、图谱节点。

## 为什么需要它

实体明细页的删除是软删除：写一条 `__deleted__` 编辑，terms 行留着，图谱节点
真删。规则是"人工删除不能被数据源更新恢复"（Foundry: Deletions aren't
reversible by datasource updates），所以删掉之后重新导入，那些实体仍然看不见。

这条规则保护的是"人刻意删掉的东西不被 ETL 带回来"。但"清空一个类型、从零
重新导入"是另一件事：demo 租户批量删掉了 Order ID / Customer Name / Product
等 39362 个实体，本意是清理后重导，而软删除让重导出来的实体永远不可见。

这里提供的是那件事的正确形态：不是撤销删除，也不是让 ETL 越过删除，而是把
这个类型在存储里的痕迹整个抹掉——下一次导入就是第一次导入。

## 不可逆

没有回收站。所以 purge_term_type 要求调用方给出预览时看到的实体数：服务端
重新算一遍，对不上就拒绝（预览之后有人导入了新数据，删的会比看到的多）。

## 不清的东西

稳定码分配（AllocatedCodeNodeKeyPart 的编号表）不动：重导时同一个原始值仍然
拿到同一个编号，下游引用这些编号的地方不会错位。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiosqlite

from app.graphrag.ontology_change_log import record_ontology_change
from app.graphrag.term_edits_store import FIELD_CREATED, FIELD_DELETED, list_term_edits

logger = logging.getLogger(__name__)

#: 图谱侧每批删多少个节点。一次 UNWIND 上万个 DETACH DELETE 会是一个巨大的事务；
#: 分批之后中途失败最多留下"部分节点已删"——候选集来自 SQLite、SQLite 还没动，
#: 重跑一次就收敛。
GRAPH_BATCH_SIZE = 1000

#: 按 node_key 挂着这个实体数据的表，以及挂在哪一列上。
#: 缺表时跳过（老库、测试库不一定建了全部）。
_SATELLITES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("term_edits", ("node_key",)),
    ("review_alias_usage", ("node_key",)),
    ("attribute_conflicts", ("node_key",)),
    ("duplicate_review_queue", ("candidate_a_node_key", "candidate_b_node_key")),
)


class PurgeCountChangedError(Exception):
    """预览之后数据变了——拒绝执行，让用户看一眼新的数字再决定。"""


@dataclass(frozen=True)
class StoredTermType:
    term_type: str
    #: terms 表里这个类型的行数（含被人工删除、列表里看不见的）。
    stored: int
    #: 其中没被人工删除的。stored > visible 就是"删了但还占着存储、重导也回不来"。
    visible: int


@dataclass(frozen=True)
class PurgePlan:
    term_type: str
    node_keys: list[str]
    #: terms 表里的行数。
    stored_rows: int
    #: 只存在于编辑层（后台新建、terms 无行）的实体数。
    created_only: int


@dataclass(frozen=True)
class PurgeResult:
    term_type: str
    node_count: int
    removed_by_table: dict[str, int]


async def list_stored_term_types(
    conn: aiosqlite.Connection, tenant_id: str
) -> list[StoredTermType]:
    """存储里实际有哪些类型、各占多少行。

    实体列表页的类型摘要走合并视图，全被删掉的类型根本不出现——而那恰恰是
    最需要清空的那些。所以这里读原始表。
    """
    cursor = await conn.execute(
        "SELECT term_type, COUNT(*), "
        "SUM(CASE WHEN node_key IN (SELECT node_key FROM term_edits "
        "WHERE tenant_id = ? AND field = ?) THEN 0 ELSE 1 END) "
        "FROM terms WHERE tenant_id = ? GROUP BY term_type ORDER BY term_type",
        (tenant_id, FIELD_DELETED, tenant_id),
    )
    return [
        StoredTermType(term_type=row[0], stored=int(row[1]), visible=int(row[2] or 0))
        for row in await cursor.fetchall()
    ]


async def plan_purge_term_type(
    conn: aiosqlite.Connection, tenant_id: str, term_type: str
) -> PurgePlan:
    cursor = await conn.execute(
        "SELECT node_key FROM terms WHERE tenant_id = ? AND term_type = ?",
        (tenant_id, term_type),
    )
    stored_keys = [row[0] for row in await cursor.fetchall()]
    stored_set = set(stored_keys)

    # 后台新建、terms 里没有行的实体：列表里它就挂在这个类型下，清空这个类型
    # 时不带上它，用户会看到"清空了，但还剩几条"。
    created_only: list[str] = []
    for node_key, fields in (await list_term_edits(conn, tenant_id)).items():
        if node_key in stored_set:
            continue
        created = fields.get(FIELD_CREATED)
        if isinstance(created, dict) and fields.get("term_type", created.get("term_type")) == term_type:
            created_only.append(node_key)

    return PurgePlan(
        term_type=term_type,
        node_keys=stored_keys + created_only,
        stored_rows=len(stored_keys),
        created_only=len(created_only),
    )


async def _existing_tables(conn: aiosqlite.Connection) -> set[str]:
    cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row[0] for row in await cursor.fetchall()}


async def purge_term_type(
    conn: aiosqlite.Connection,
    graph_client,
    tenant_id: str,
    term_type: str,
    *,
    expected_node_count: int,
    actor: str,
) -> PurgeResult:
    plan = await plan_purge_term_type(conn, tenant_id, term_type)
    if len(plan.node_keys) != expected_node_count:
        raise PurgeCountChangedError(
            f"「{term_type}」现在有 {len(plan.node_keys)} 个实体，确认时看到的是 "
            f"{expected_node_count} 个——预览之后数据变了，本次未删除任何东西。"
            "重新打开预览核对后再执行。"
        )
    if not plan.node_keys:
        return PurgeResult(term_type=term_type, node_count=0, removed_by_table={})

    # 先删图谱，再删 SQLite。候选集来自 SQLite：图谱删到一半失败，SQLite 完好，
    # 重跑一次就收敛；反过来先删 SQLite，图谱里剩下的节点就再也没有人记得。
    for start in range(0, len(plan.node_keys), GRAPH_BATCH_SIZE):
        await graph_client.delete_term_nodes(
            tenant_id=tenant_id, node_keys=plan.node_keys[start:start + GRAPH_BATCH_SIZE]
        )

    tables = await _existing_tables(conn)
    removed: dict[str, int] = {}
    # 临时表装 node_key：几万个 key 展开成 IN (?, ...) 会撞 SQLite 的参数上限
    # （实体列表因此 500 过一次，见 terms_store._EDITED_KEYS_SUBQUERY）。
    await conn.execute("DROP TABLE IF EXISTS temp.purge_keys")
    await conn.execute("CREATE TEMP TABLE purge_keys (node_key TEXT PRIMARY KEY)")
    try:
        await conn.executemany(
            "INSERT OR IGNORE INTO temp.purge_keys (node_key) VALUES (?)",
            [(key,) for key in plan.node_keys],
        )
        for table, columns in _SATELLITES:
            if table not in tables:
                continue
            where = " OR ".join(f"{col} IN (SELECT node_key FROM temp.purge_keys)" for col in columns)
            cursor = await conn.execute(
                f"DELETE FROM {table} WHERE tenant_id = ? AND ({where})", (tenant_id,)
            )
            removed[table] = cursor.rowcount
        cursor = await conn.execute(
            "DELETE FROM terms WHERE tenant_id = ? AND node_key IN "
            "(SELECT node_key FROM temp.purge_keys)",
            (tenant_id,),
        )
        removed["terms"] = cursor.rowcount
        if "ontology_change_log" in tables:
            await record_ontology_change(
                conn, tenant_id, actor=actor, action="purge", object_kind="term_data",
                object_id=term_type,
                details={"node_count": len(plan.node_keys), "removed": removed},
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.execute("DROP TABLE IF EXISTS temp.purge_keys")

    logger.warning(
        "租户 %r 的实体类型 %r 已彻底清空：%d 个实体，各表删除行数 %s（操作人 %s）",
        tenant_id, term_type, len(plan.node_keys), removed, actor,
    )
    return PurgeResult(term_type=term_type, node_count=len(plan.node_keys), removed_by_table=removed)
