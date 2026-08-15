from __future__ import annotations

import aiosqlite

from app.graphrag.ontology_categories import ensure_categories_schema
from app.graphrag.ontology_constraints import ensure_constraints_schema
from app.graphrag.ontology_relations import ensure_relations_schema, seed_default_relation_types
from app.graphrag.tenant_ingestion_config import ensure_ingestion_config_schema, get_ingestion_mode

_TABLES_WITH_TENANT_LIFECYCLE = ("tenant_relation_types", "term_type_relation_allowlist")


async def ensure_ontology_schema(conn: aiosqlite.Connection) -> None:
    """统一入口：分类（按租户）+ 关系类型/约束（按租户）+ 接入模式配置
    四张表一起建。ensure_ingestion_config_schema 放进来，保证 checkout_draft
    需要读 ingestion_mode 时这张表一定已经存在，不需要调用方自己记得
    额外建表。
    """
    await ensure_categories_schema(conn)
    await ensure_relations_schema(conn)
    await ensure_constraints_schema(conn)
    await ensure_ingestion_config_schema(conn)


async def _has_any_row(
    conn: aiosqlite.Connection, table: str, tenant_id: str, status: str
) -> bool:
    cursor = await conn.execute(
        f"SELECT 1 FROM {table} WHERE tenant_id = ? AND status = ? LIMIT 1",
        (tenant_id, status),
    )
    return await cursor.fetchone() is not None


async def checkout_draft(conn: aiosqlite.Connection, tenant_id: str) -> None:
    """检出一份可编辑的草稿：如果该租户已经有草稿，什么都不做（幂等，不覆盖正在
    编辑的内容）；如果没有草稿但有已确认版本，从已确认版本复制一份新草稿；如果
    两者都没有（全新租户），关系类型草稿的默认值取决于该租户的接入模式
    （tenant_ingestion_config.ingestion_mode）——extraction 模式播种 10 种
    通用默认关系（面向 LLM 抽取场景设计，见 ontology_relations.py），etl
    模式不播种，从空白草稿开始（这些默认关系对结构化 ETL 租户没有意义，
    见 docs/superpowers/specs/2026-08-15-etl-driven-schema-construction-
    design.md §2）。约束表草稿两种模式都留空（没有分类数据支撑，写不出
    有意义的默认组合）。
    """
    if not await _has_any_row(conn, "tenant_relation_types", tenant_id, "draft"):
        if await _has_any_row(conn, "tenant_relation_types", tenant_id, "confirmed"):
            await conn.execute(
                "INSERT INTO tenant_relation_types "
                "(tenant_id, relation_type, example_phrase, description, allow_chain_query, "
                "source, status) "
                "SELECT tenant_id, relation_type, example_phrase, description, "
                "allow_chain_query, source, 'draft' FROM tenant_relation_types "
                "WHERE tenant_id = ? AND status = 'confirmed'",
                (tenant_id,),
            )
        elif await get_ingestion_mode(conn, tenant_id) == "extraction":
            await seed_default_relation_types(conn, tenant_id)
    if not await _has_any_row(conn, "term_type_relation_allowlist", tenant_id, "draft"):
        if await _has_any_row(conn, "term_type_relation_allowlist", tenant_id, "confirmed"):
            await conn.execute(
                "INSERT INTO term_type_relation_allowlist "
                "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
                "SELECT tenant_id, subject_term_type, relation_type, object_term_type, 'draft' "
                "FROM term_type_relation_allowlist WHERE tenant_id = ? AND status = 'confirmed'",
                (tenant_id,),
            )
    await conn.commit()


async def confirm_ontology(conn: aiosqlite.Connection, tenant_id: str) -> None:
    """把草稿原子性地提升为已确认版本：先删旧的已确认行，再把草稿行的 status 原地
    改成 confirmed——两张表（关系类型、约束）在同一次 commit 里一起提交，不会出现
    "关系类型确认了但约束表没确认"这种半提交状态。确认之后草稿即被清空（status
    改写成 confirmed，不再是 draft），下一次编辑需要重新调用 checkout_draft。

    防护：如果没有任何草稿行，直接返回（不操作已确认的数据），防止重复确认导致
    数据丢失。这使得函数对重复/重试请求自然幂等。
    """
    # 如果两个表都没有草稿，说明没有新的内容要提升，直接返回以避免删除已确认数据
    has_draft_in_any_table = (
        await _has_any_row(conn, "tenant_relation_types", tenant_id, "draft")
        or await _has_any_row(conn, "term_type_relation_allowlist", tenant_id, "draft")
    )
    if not has_draft_in_any_table:
        return

    for table in _TABLES_WITH_TENANT_LIFECYCLE:
        await conn.execute(
            f"DELETE FROM {table} WHERE tenant_id = ? AND status = 'confirmed'", (tenant_id,)
        )
        await conn.execute(
            f"UPDATE {table} SET status = 'confirmed' WHERE tenant_id = ? AND status = 'draft'",
            (tenant_id,),
        )
    await conn.commit()


async def is_ontology_confirmed(conn: aiosqlite.Connection, tenant_id: str) -> bool:
    return await _has_any_row(conn, "tenant_relation_types", tenant_id, "confirmed")
