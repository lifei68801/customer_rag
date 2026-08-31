from __future__ import annotations

import aiosqlite

from app.graphrag.ontology_categories import ensure_categories_schema
from app.graphrag.ontology_constraints import ensure_constraints_schema
from app.graphrag.ontology_relations import ensure_relations_schema, seed_default_relation_types
from app.graphrag.tenant_ingestion_config import ensure_ingestion_config_schema, get_ingestion_mode

_TABLES_WITH_TENANT_LIFECYCLE = (
    "tenant_relation_types", "term_type_relation_allowlist", "ontology_term_types",
)

_CHECKOUT_STATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ontology_draft_checkout_state (
    tenant_id TEXT PRIMARY KEY
);
"""


async def _ensure_checkout_state_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_CHECKOUT_STATE_SCHEMA_SQL)
    await conn.commit()


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
    await _ensure_checkout_state_schema(conn)


async def _has_any_row(
    conn: aiosqlite.Connection, table: str, tenant_id: str, status: str
) -> bool:
    cursor = await conn.execute(
        f"SELECT 1 FROM {table} WHERE tenant_id = ? AND status = ? LIMIT 1",
        (tenant_id, status),
    )
    return await cursor.fetchone() is not None


async def _has_checked_out_since_last_confirm(conn: aiosqlite.Connection, tenant_id: str) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM ontology_draft_checkout_state WHERE tenant_id = ?", (tenant_id,)
    )
    return await cursor.fetchone() is not None


async def checkout_draft(conn: aiosqlite.Connection, tenant_id: str) -> None:
    """检出一份可编辑的草稿：如果该租户自上次 confirm 以来已经检出过草稿，什么都
    不做（哪怕当前三张草稿表都是空的——那是用户主动删完了草稿内容的合法终态，
    不是"还没检出过"，不能拿"草稿是否为空"当检出信号，见下面的踩坑记录）；如果
    还没检出过但有已确认版本，从已确认版本复制一份新草稿；如果两者都没有（全新
    租户），关系类型草稿的默认值取决于该租户的接入模式
    （tenant_ingestion_config.ingestion_mode）——extraction 模式播种 10 种
    通用默认关系（面向 LLM 抽取场景设计，见 ontology_relations.py），etl
    模式不播种，从空白草稿开始（这些默认关系对结构化 ETL 租户没有意义，
    见 docs/superpowers/specs/2026-08-15-etl-driven-schema-construction-
    design.md §2）。约束表草稿两种模式都留空（没有分类数据支撑，写不出
    有意义的默认组合）。

    踩坑记录（这个函数曾经的实现方式，以及为什么改掉了）：早期版本没有
    ontology_draft_checkout_state 这张表，直接用"这张草稿表当前有没有行"
    当"是否需要从已确认版本重新播种"的信号。这个信号在草稿从有到无是"用户主动
    删除干净"的场景下会撒谎——管理后台里删除本体管理页面（实体类型/关系类型/
    约束三个 tab）任何一条草稿记录后，前端都会紧接着调用一次本函数刷新界面，
    如果删除的正好是该租户当时唯一一条草稿记录、且该租户历史上确认过 schema
    （已确认版本非空），旧实现会把这条刚删除的记录从已确认版本原样复制回来，
    用户在界面上完全看不出删除生效过——点删除、页面刷新、数据"原地不动"。现在
    用一张独立的状态表记录"自上次 confirm 以来是否已经检出过"，检出信号不再
    跟"当前行数是否为零"绑在一起，删空草稿就是删空草稿，不会被这个函数悄悄
    撤销。

    并发安全：三条"从已确认版本复制成草稿"的插入都用 INSERT OR IGNORE。
    这个函数是典型的"先查后写、中间无锁"——检查草稿是否为空、检查已确认
    版本是否存在、再插入，三步之间有 await 让出点，而 deps.get_review_conn
    是进程内单例连接，多个请求的协程共用它、可以在这些让出点互相穿插。
    管理后台的本体页面三个 tab（实体类型/关系类型/约束）各自发一次
    checkout，实测会几乎同时到达：两个请求都看到"草稿为空"、都执行复制，
    第二个撞主键 UNIQUE (tenant_id, relation_type, status)，返回 500，
    界面显示"schema 草稿初始化失败"。

    复制这件事天生幂等——行已经在了就该跳过——所以 OR IGNORE 是语义上
    正确的写法，而不是掩盖冲突。用加锁或包事务也能解决，但那会把单例
    连接上的并发请求串行化，代价更大，而且没必要：本来就该幂等的操作
    不需要互斥。
    """
    await _ensure_checkout_state_schema(conn)
    if await _has_checked_out_since_last_confirm(conn, tenant_id):
        return
    if not await _has_any_row(conn, "tenant_relation_types", tenant_id, "draft"):
        if await _has_any_row(conn, "tenant_relation_types", tenant_id, "confirmed"):
            await conn.execute(
                "INSERT OR IGNORE INTO tenant_relation_types "
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
                "INSERT OR IGNORE INTO term_type_relation_allowlist "
                "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
                "SELECT tenant_id, subject_term_type, relation_type, object_term_type, 'draft' "
                "FROM term_type_relation_allowlist WHERE tenant_id = ? AND status = 'confirmed'",
                (tenant_id,),
            )
    if not await _has_any_row(conn, "ontology_term_types", tenant_id, "draft"):
        if await _has_any_row(conn, "ontology_term_types", tenant_id, "confirmed"):
            await conn.execute(
                "INSERT OR IGNORE INTO ontology_term_types "
                "(tenant_id, value, extra_fields, node_key_template, status) "
                "SELECT tenant_id, value, extra_fields, node_key_template, 'draft' "
                "FROM ontology_term_types WHERE tenant_id = ? AND status = 'confirmed'",
                (tenant_id,),
            )
        # 新租户没有默认实体类型可播种——不同于关系类型有 10 种通用拓扑
        # 关系兜底，实体类型完全依赖业务定义，没有"合理默认值"这回事，
        # 两种接入模式（extraction/etl）在这一点上没有区别。
    await conn.execute(
        "INSERT OR IGNORE INTO ontology_draft_checkout_state (tenant_id) VALUES (?)", (tenant_id,)
    )
    await conn.commit()


async def confirm_ontology(conn: aiosqlite.Connection, tenant_id: str) -> None:
    """把草稿原子性地提升为已确认版本：先删旧的已确认行，再把草稿行的 status 原地
    改成 confirmed——两张表（关系类型、约束）在同一次 commit 里一起提交，不会出现
    "关系类型确认了但约束表没确认"这种半提交状态。确认之后草稿即被清空（status
    改写成 confirmed，不再是 draft），下一次编辑需要重新调用 checkout_draft。

    防护：如果没有任何草稿行，直接返回（不操作已确认的数据），防止重复确认导致
    数据丢失。这使得函数对重复/重试请求自然幂等。

    确认成功后同时清掉 ontology_draft_checkout_state 里这个租户的检出标记——
    草稿这一轮编辑会话已经结束（内容原地提升成了 confirmed），下一次
    checkout_draft 应该被当成"还没检出过"，重新从刚确认的版本复制一份新草稿，
    而不是延续本轮已经过期的检出状态（那样会导致新草稿从一开始就是空的，
    checkout_draft 却因为标记还在而不去重新播种）。
    """
    await _ensure_checkout_state_schema(conn)
    # 如果两个表都没有草稿，说明没有新的内容要提升，直接返回以避免删除已确认数据
    has_draft_in_any_table = (
        await _has_any_row(conn, "tenant_relation_types", tenant_id, "draft")
        or await _has_any_row(conn, "term_type_relation_allowlist", tenant_id, "draft")
        or await _has_any_row(conn, "ontology_term_types", tenant_id, "draft")
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
    await conn.execute(
        "DELETE FROM ontology_draft_checkout_state WHERE tenant_id = ?", (tenant_id,)
    )
    await conn.commit()


async def is_ontology_confirmed(conn: aiosqlite.Connection, tenant_id: str) -> bool:
    return await _has_any_row(conn, "tenant_relation_types", tenant_id, "confirmed")
