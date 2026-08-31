"""人工编辑层的存储：term_edits 表的建表与增删查。

这张表跟 terms 是物理分离的两批行——terms 由管道（ETL/抽取）维护，
term_edits 只由人工路径写入。读路径把两者合并（见 term_merge.py），
于是"重跑 ETL 不伤人工修正"成为结构性保证，而不是靠约定。
见 docs/superpowers/specs/2026-08-30-manual-edits-layer-design.md。

这一层只管存取，不认识合并语义——合并是 term_merge.py 的事。
"""

from __future__ import annotations

import json
from datetime import datetime

import aiosqlite

# 删除标记。value 存 SQL NULL；合并视图遇到它就把该实体整个排除。
FIELD_DELETED = "__deleted__"
# 编辑层创建标记。value 是创建时的完整字段对象。
FIELD_CREATED = "__created__"
# 属性字段编辑的 field 前缀，例如 "extra_properties.revenue"。
EXTRA_PROPERTY_PREFIX = "extra_properties."

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS term_edits (
    tenant_id     TEXT NOT NULL,
    node_key      TEXT NOT NULL,
    field         TEXT NOT NULL,
    value         TEXT,
    edited_at     TEXT NOT NULL,
    edited_by     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, node_key, field)
);
CREATE INDEX IF NOT EXISTS idx_term_edits_tenant ON term_edits (tenant_id);
"""


async def ensure_term_edits_schema(conn: aiosqlite.Connection) -> None:
    """建表，幂等。由 ontology_store.open_ontology_store_conn 统一调用——
    那是本项目唯一的建表入口（2026-08-30 起）。"""
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def upsert_term_edit(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    node_key: str,
    field: str,
    value: object,
    edited_by: str,
) -> None:
    """写入或覆盖一条字段级编辑。

    term_edits 保存的是**当前编辑状态**，不是 append-only 日志：同一个
    (tenant_id, node_key, field) 改两次只剩最后一次。需要审计流水时要
    另行设计，见 spec 的非目标。

    field == FIELD_DELETED 时 value 传 None，落库为 SQL NULL。其余字段的
    value 序列化成 JSON 文本——aliases 是列表、extra_properties.<name>
    可能是数值，不走 JSON 会在读回来时全变成字符串。
    """
    stored = None if field == FIELD_DELETED else json.dumps(value, ensure_ascii=False)
    await conn.execute(
        "INSERT INTO term_edits (tenant_id, node_key, field, value, edited_at, edited_by) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(tenant_id, node_key, field) DO UPDATE SET "
        "value = excluded.value, edited_at = excluded.edited_at, edited_by = excluded.edited_by",
        (tenant_id, node_key, field, stored, datetime.now().isoformat(), edited_by),
    )
    await conn.commit()


async def delete_term_edit(
    conn: aiosqlite.Connection, *, tenant_id: str, node_key: str, field: str
) -> None:
    """撤掉某一个字段的编辑——该字段之后重新跟随管道产出的值。"""
    await conn.execute(
        "DELETE FROM term_edits WHERE tenant_id = ? AND node_key = ? AND field = ?",
        (tenant_id, node_key, field),
    )
    await conn.commit()


def _row_to_value(field: str, raw: str | None) -> object:
    if field == FIELD_DELETED:
        return None
    return json.loads(raw) if raw is not None else None


async def list_term_edits(
    conn: aiosqlite.Connection, tenant_id: str
) -> dict[str, dict[str, object]]:
    """该租户的全部编辑，按 {node_key: {field: value}} 组织。

    合并视图一次性取全量：term_edits 通常远小于 terms（只有被人工碰过的
    行），逐个 node_key 查会在 list_terms_merged 里退化成 N+1。
    """
    cursor = await conn.execute(
        "SELECT node_key, field, value FROM term_edits WHERE tenant_id = ?",
        (tenant_id,),
    )
    edits: dict[str, dict[str, object]] = {}
    for node_key, field, raw in await cursor.fetchall():
        edits.setdefault(node_key, {})[field] = _row_to_value(field, raw)
    return edits


async def list_term_edits_for_node_key(
    conn: aiosqlite.Connection, tenant_id: str, node_key: str
) -> dict[str, object]:
    """单个实体的全部编辑，供按 node_key 取单条的读路径用。"""
    cursor = await conn.execute(
        "SELECT field, value FROM term_edits WHERE tenant_id = ? AND node_key = ?",
        (tenant_id, node_key),
    )
    return {field: _row_to_value(field, raw) for field, raw in await cursor.fetchall()}
