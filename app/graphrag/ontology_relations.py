from __future__ import annotations

import re
from dataclasses import dataclass

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenant_relation_types (
    tenant_id         TEXT NOT NULL,
    relation_type     TEXT NOT NULL,
    example_phrase    TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    allow_chain_query INTEGER NOT NULL DEFAULT 0,
    source            TEXT NOT NULL DEFAULT 'custom',
    status            TEXT NOT NULL,
    PRIMARY KEY (tenant_id, relation_type, status)
);
"""

# Cypher 关系类型不能参数化绑定，只能拼进查询字符串——这是注入防线，任何写入路径
# （无论数据来自默认种子还是业务自助新增）都必须过这道机械校验，不因为是"默认值"
# 就豁免。
_RELATION_TYPE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}\Z")

# 10 种全局默认拓扑关系的初始种子——例句取自 app/graphrag/llm_extractor.py 现有
# system prompt 里的例句，保持口径一致。REQUIRES/PRECEDES/PART_OF 默认放开链式
# 查询资格，理由见 docs/superpowers/specs/2026-08-09-chunking-graph-extraction-
# redesign-design.md §5。
_DEFAULT_RELATION_TYPES: list[tuple[str, str, str, bool]] = [
    ("RELATED_TO", "促销活动 RELATED_TO 会员日", "兜底：弱关联，语义不明确时的默认选项", False),
    ("PART_OF", "客房 PART_OF 酒店", "部分-整体", True),
    ("IS_A", "大床房 IS_A 客房", "类别从属/分类层级", False),
    ("REQUIRES", "预订套餐 REQUIRES 会员资格", "前提依赖", True),
    ("ALTERNATIVE_TO", "标准间 ALTERNATIVE_TO 大床房", "替代/类似", False),
    ("CAUSES", "恶劣天气 CAUSES 接送延误", "因果", False),
    ("ADDRESSED_BY", "房间异味 ADDRESSED_BY 更换房间", "问题由方案解决", False),
    ("LOCATED_IN", "健身房 LOCATED_IN 三楼", "空间/组织归属", False),
    ("APPLIES_TO", "会员折扣 APPLIES_TO 非节假日预订", "适用范围", False),
    ("PRECEDES", "入住登记 PRECEDES 领取房卡", "流程先后顺序", True),
]


class InvalidRelationTypeNameError(Exception):
    """关系类型名字不满足标识符格式，或例句为空。"""


class RelationTypeNotFoundError(Exception):
    """指定租户的草稿里不存在这个关系类型。"""


class RelationTypeNameConflictError(Exception):
    """提交的关系类型名字（新建，或改名的目标名字）在该租户的草稿里已存在——
    表主键是 (tenant_id, relation_type, status)。"""


@dataclass(frozen=True)
class RelationTypeDef:
    relation_type: str
    example_phrase: str
    description: str
    allow_chain_query: bool
    source: str


async def ensure_relations_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


def _row_to_def(row: aiosqlite.Row) -> RelationTypeDef:
    return RelationTypeDef(
        relation_type=row["relation_type"],
        example_phrase=row["example_phrase"],
        description=row["description"],
        allow_chain_query=bool(row["allow_chain_query"]),
        source=row["source"],
    )


async def list_relation_types(
    conn: aiosqlite.Connection, tenant_id: str, *, status: str
) -> list[RelationTypeDef]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT relation_type, example_phrase, description, allow_chain_query, source "
        "FROM tenant_relation_types WHERE tenant_id = ? AND status = ? ORDER BY relation_type",
        (tenant_id, status),
    )
    rows = await cursor.fetchall()
    return [_row_to_def(row) for row in rows]


async def seed_default_relation_types(conn: aiosqlite.Connection, tenant_id: str) -> None:
    """把 10 种默认关系类型写入该租户的草稿——INSERT OR IGNORE 保证重复调用幂等
    （已存在的行不会被覆盖，业务如果已经改过某个默认类型，重复调用不会把改动冲掉）。
    """
    for relation_type, example_phrase, description, allow_chain in _DEFAULT_RELATION_TYPES:
        await conn.execute(
            "INSERT OR IGNORE INTO tenant_relation_types "
            "(tenant_id, relation_type, example_phrase, description, allow_chain_query, "
            "source, status) VALUES (?, ?, ?, ?, ?, 'default', 'draft')",
            (tenant_id, relation_type, example_phrase, description, int(allow_chain)),
        )
    await conn.commit()


def _validate_relation_type(relation_type: str, example_phrase: str) -> None:
    if not _RELATION_TYPE_PATTERN.match(relation_type):
        raise InvalidRelationTypeNameError(
            f"关系类型名字不合法: {relation_type!r}，必须满足 ^[A-Z][A-Z0-9_]{{0,63}}$"
        )
    if not example_phrase.strip():
        raise InvalidRelationTypeNameError("example_phrase 不能为空")


async def create_relation_type(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    relation_type: str,
    example_phrase: str,
    description: str = "",
    allow_chain_query: bool = False,
) -> None:
    _validate_relation_type(relation_type, example_phrase)
    try:
        await conn.execute(
            "INSERT INTO tenant_relation_types "
            "(tenant_id, relation_type, example_phrase, description, allow_chain_query, "
            "source, status) VALUES (?, ?, ?, ?, ?, 'custom', 'draft')",
            (tenant_id, relation_type, example_phrase, description, int(allow_chain_query)),
        )
    except aiosqlite.IntegrityError:
        raise RelationTypeNameConflictError(
            f"{relation_type!r} 已经是该租户草稿里的关系类型，不能重复创建"
        )
    await conn.commit()


async def update_relation_type(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    relation_type: str,
    example_phrase: str,
    description: str,
    allow_chain_query: bool,
    new_relation_type: str | None = None,
) -> None:
    """relation_type 是当前（改名前）的名字，用来定位草稿里的这一行；
    new_relation_type 是提交的新名字，默认 None 表示不改名（沿用
    relation_type）。改名时级联更新该租户 term_type_relation_allowlist 里
    引用旧名字的 draft 行（confirmed 行不动，跟 delete_relation_type 的
    级联范围保持一致——约束条目跟着草稿走，不跟着已确认版本走）。"""
    new_relation_type = new_relation_type or relation_type
    _validate_relation_type(new_relation_type, example_phrase)
    cursor = await conn.execute(
        "SELECT 1 FROM tenant_relation_types WHERE tenant_id = ? AND relation_type = ? "
        "AND status = 'draft'",
        (tenant_id, relation_type),
    )
    if await cursor.fetchone() is None:
        raise RelationTypeNotFoundError(f"草稿里不存在关系类型: {relation_type}")
    try:
        await conn.execute(
            "UPDATE tenant_relation_types SET relation_type = ?, example_phrase = ?, "
            "description = ?, allow_chain_query = ? "
            "WHERE tenant_id = ? AND relation_type = ? AND status = 'draft'",
            (
                new_relation_type, example_phrase, description, int(allow_chain_query),
                tenant_id, relation_type,
            ),
        )
    except aiosqlite.IntegrityError:
        raise RelationTypeNameConflictError(
            f"{new_relation_type!r} 已经是该租户草稿里的关系类型，不能重复使用"
        )
    if new_relation_type != relation_type:
        await conn.execute(
            "UPDATE term_type_relation_allowlist SET relation_type = ? "
            "WHERE tenant_id = ? AND relation_type = ? AND status = 'draft'",
            (new_relation_type, tenant_id, relation_type),
        )
    await conn.commit()


async def delete_relation_type(
    conn: aiosqlite.Connection, tenant_id: str, relation_type: str
) -> None:
    """不设引用保护——关系类型表只是写入时的白名单闸门，不是任何表的外键约束
    对象；已写入 Neo4j 的旧边不因为闸门关闭而失效（见调用方 ontology_lifecycle.py
    以及 spec 文档第 7 节）。

    但草稿约束表（term_type_relation_allowlist）里引用这个关系类型的 draft 行
    要一并删除——否则这些行会在下一次 confirm_ontology 时被原样提升成
    confirmed，变成永久指向一个已经不存在的关系类型的孤儿配置。只删同一租户的
    draft 行，跟本函数原本的删除范围保持一致，不动 confirmed 行。"""
    await conn.execute(
        "DELETE FROM tenant_relation_types WHERE tenant_id = ? AND relation_type = ? "
        "AND status = 'draft'",
        (tenant_id, relation_type),
    )
    await conn.execute(
        "DELETE FROM term_type_relation_allowlist WHERE tenant_id = ? AND relation_type = ? "
        "AND status = 'draft'",
        (tenant_id, relation_type),
    )
    await conn.commit()
