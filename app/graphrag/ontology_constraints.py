from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from app.graphrag.ontology_categories import list_term_types
from app.graphrag.ontology_relations import list_relation_types

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS term_type_relation_allowlist (
    tenant_id          TEXT NOT NULL,
    subject_term_type  TEXT NOT NULL,
    relation_type       TEXT NOT NULL,
    object_term_type   TEXT NOT NULL,
    status              TEXT NOT NULL,
    PRIMARY KEY (tenant_id, subject_term_type, relation_type, object_term_type, status)
);
"""


class UnknownCategoryError(Exception):
    """引用的 term_type 不在全局分类枚举里。"""


class UnknownRelationTypeError(Exception):
    """引用的关系类型不在该租户当前草稿里。"""


@dataclass(frozen=True)
class AllowedCombination:
    subject_term_type: str
    relation_type: str
    object_term_type: str


async def ensure_constraints_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def list_allowed_combinations(
    conn: aiosqlite.Connection, tenant_id: str, *, status: str
) -> list[AllowedCombination]:
    cursor = await conn.execute(
        "SELECT subject_term_type, relation_type, object_term_type "
        "FROM term_type_relation_allowlist WHERE tenant_id = ? AND status = ? "
        "ORDER BY subject_term_type, relation_type, object_term_type",
        (tenant_id, status),
    )
    rows = await cursor.fetchall()
    return [AllowedCombination(subject_term_type=r[0], relation_type=r[1], object_term_type=r[2]) for r in rows]


async def _validate_references(
    conn: aiosqlite.Connection, tenant_id: str, *, subject_term_type: str,
    relation_type: str, object_term_type: str,
) -> None:
    # 约束条目必须与关系类型在同一草稿编辑会话中创建——这里校验 draft 关系类型，而非已发布的
    # confirmed 类型，是因为业务在编辑关系类型时需要同步编辑其约束白名单，两者是一对一绑定的
    # 完整流程。Task 4（草稿→确认生命周期编排）已经上线，ontology_lifecycle.py 的
    # confirm_ontology 把 tenant_relation_types 和 term_type_relation_allowlist 两张表在
    # 同一次 commit 里一起从 draft 提升为 confirmed，这里校验 draft 而非 confirmed 是经过
    # 落地验证后确认正确的设计，不再是待定问题。
    known_types = {c.value for c in await list_term_types(conn, tenant_id, status="draft")}
    if subject_term_type not in known_types:
        raise UnknownCategoryError(f"未知分类: {subject_term_type!r}")
    if object_term_type not in known_types:
        raise UnknownCategoryError(f"未知分类: {object_term_type!r}")
    known_relations = {
        r.relation_type for r in await list_relation_types(conn, tenant_id, status="draft")
    }
    if relation_type not in known_relations:
        raise UnknownRelationTypeError(f"该租户草稿里不存在关系类型: {relation_type!r}")


async def add_allowed_combination(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    subject_term_type: str,
    relation_type: str,
    object_term_type: str,
) -> None:
    await _validate_references(
        conn, tenant_id, subject_term_type=subject_term_type,
        relation_type=relation_type, object_term_type=object_term_type,
    )
    await conn.execute(
        "INSERT OR IGNORE INTO term_type_relation_allowlist "
        "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
        "VALUES (?, ?, ?, ?, 'draft')",
        (tenant_id, subject_term_type, relation_type, object_term_type),
    )
    await conn.commit()


async def remove_allowed_combination(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    subject_term_type: str,
    relation_type: str,
    object_term_type: str,
) -> None:
    await conn.execute(
        "DELETE FROM term_type_relation_allowlist WHERE tenant_id = ? AND "
        "subject_term_type = ? AND relation_type = ? AND object_term_type = ? AND status = 'draft'",
        (tenant_id, subject_term_type, relation_type, object_term_type),
    )
    await conn.commit()
