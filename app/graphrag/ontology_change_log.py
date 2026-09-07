"""本体变更日志：对本体**定义**（分类、关系类型、关系约束、整份草稿）的
每一次写入都在这里留一行。

为什么是一张独立的日志表，而不是给那三张本体表各加 edited_by/edited_at
两列：ontology_term_types / tenant_relation_types /
term_type_relation_allowlist 的删除都是真 DELETE（见各自模块的
delete_*/remove_*），行没了，挂在行上的审计字段跟着一起没了。而"谁删了
这个分类"恰恰是最需要事后追查的那个问题——行上的字段结构性地覆盖不了
删除，加多少列都一样。

这张表是 append-only 的：只 INSERT，没有 UPDATE/DELETE 路径。它跟
term_edits 不同——那张表存的是"当前编辑状态"（每个 (node_key, field)
只留最后一次），这张存的是"发生过什么"。

actor 一律由调用方传入且必填（keyword-only、无默认值）。不给默认值是
刻意的：默认值会让某个调用点悄悄把变更记在一个假身份上，那正是
admin_terms_routes.py 里 _EDITED_BY = "admin" 当年的毛病。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ontology_change_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT NOT NULL,
    changed_at   TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    object_kind  TEXT NOT NULL,
    object_id    TEXT NOT NULL,
    details      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_ontology_change_log_tenant_id
    ON ontology_change_log (tenant_id, id);
"""

#: action 的取值。create/update/delete 是单个对象的增删改，confirm 是把整份
#: 草稿提升成已确认版本，replace 是整份替换草稿——后两个是批量操作，一次
#: 记一条汇总，不逐行展开。
ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_DELETE = "delete"
ACTION_CONFIRM = "confirm"
ACTION_REPLACE = "replace"

#: object_kind 的取值。前三个对应三张本体表里的一行，后两个对应整份本体/
#: 整份草稿（批量操作没有单个对象，object_id 落空串）。
KIND_TERM_TYPE = "term_type"
KIND_RELATION_TYPE = "relation_type"
KIND_CONSTRAINT = "constraint"
KIND_ONTOLOGY = "ontology"
KIND_ONTOLOGY_DRAFT = "ontology_draft"


@dataclass(frozen=True)
class OntologyChange:
    tenant_id: str
    changed_at: str
    actor: str
    action: str
    object_kind: str
    object_id: str
    details: dict


async def ensure_change_log_schema(conn: aiosqlite.Connection) -> None:
    """建表，幂等。CREATE TABLE IF NOT EXISTS 对已有数据的表是空操作，
    存量库重复调用不会清空已有日志。"""
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def record_ontology_change(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    actor: str,
    action: str,
    object_kind: str,
    object_id: str,
    details: dict,
) -> None:
    """写一行日志，**不提交**。

    不提交是关键：调用方（各个本体写入函数）在自己那次 conn.commit() 里
    把业务写入和这一行一起提交，两者要么都在要么都不在。单独提交的话，
    业务写入失败时会留下一条记录了一次并未发生的变更的日志。
    """
    await conn.execute(
        "INSERT INTO ontology_change_log "
        "(tenant_id, changed_at, actor, action, object_kind, object_id, details) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            tenant_id,
            datetime.now().isoformat(),
            actor,
            action,
            object_kind,
            object_id,
            json.dumps(details, ensure_ascii=False),
        ),
    )


async def commit_with_change_log(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    actor: str,
    action: str,
    object_kind: str,
    object_id: str,
    details: dict,
) -> None:
    """本体写入函数收尾用的唯一出口：写日志 + 提交，日志写不进去就连同
    业务写入一起回滚。

    每个写入函数原本结尾都是一句裸的 `await conn.commit()`，现在换成这个
    函数。它替代 commit 而不是叠在 commit 之后，是因为"日志和业务写入在
    同一个事务里"这件事只有在 commit 之前写日志才成立。

    回滚这一步不能省：sqlite3 的隐式事务在异常时不会自己回滚，未提交的
    业务写入会一直挂在连接上，而同一个连接读得到它自己未提交的改动——
    调用方于是看到一次"抛了异常但改动生效了"的写入，正是这个函数要防的
    那种状态。

    诚实的代价：deps.get_review_conn 是进程内单例连接，这里的 rollback 会
    连带撤销同一连接上其它协程尚未提交的写入。这跟既有的 commit 是同一枚
    硬币的两面——那些 commit 同样会把别人未提交的写入一起提交掉，这个连接
    模型下本来就没有请求间的事务隔离（见 ontology_lifecycle.replace_draft
    的 docstring 对显式事务的那段论证）。在"要么都写要么都不写"和"不碰
    别人的写入"之间，这里选前者：审计缺一行是查不出来的错，而单例连接上
    的并发穿插是既有的、已知的、被文档记录过的模型缺陷。
    """
    try:
        await record_ontology_change(
            conn, tenant_id, actor=actor, action=action, object_kind=object_kind,
            object_id=object_id, details=details,
        )
    except Exception:
        await conn.rollback()
        raise
    await conn.commit()


def _row_to_change(row: tuple) -> OntologyChange:
    return OntologyChange(
        tenant_id=row[0],
        changed_at=row[1],
        actor=row[2],
        action=row[3],
        object_kind=row[4],
        object_id=row[5],
        details=json.loads(row[6]),
    )


async def list_ontology_changes(
    conn: aiosqlite.Connection, tenant_id: str, *, limit: int = 200
) -> list[OntologyChange]:
    """按发生顺序（自增 id）读该租户的变更日志，最早的在前。

    按 id 排序而不是 changed_at：同一秒内的多次变更（整份替换草稿之后紧接
    着确认，就是这种节奏）时间戳可能相同，那时顺序就成了随机的，而"先改的
    是哪一条"正是读日志的人要看的。
    """
    cursor = await conn.execute(
        "SELECT tenant_id, changed_at, actor, action, object_kind, object_id, details "
        "FROM ontology_change_log WHERE tenant_id = ? ORDER BY id LIMIT ?",
        (tenant_id, limit),
    )
    return [_row_to_change(row) for row in await cursor.fetchall()]
