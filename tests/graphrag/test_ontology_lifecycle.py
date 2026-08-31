from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_constraints import add_allowed_combination, list_allowed_combinations
from app.graphrag.ontology_categories import create_term_type, list_term_types
from app.graphrag.ontology_lifecycle import (
    checkout_draft,
    confirm_ontology,
    ensure_ontology_schema,
    is_ontology_confirmed,
)
from app.graphrag.ontology_relations import create_relation_type, list_relation_types

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    return conn


async def test_checkout_draft_seeds_defaults_for_brand_new_tenant():
    conn = await _conn()

    await checkout_draft(conn, "t1")

    result = await list_relation_types(conn, "t1", status="draft")
    assert len(result) == 10


async def test_is_ontology_confirmed_false_before_first_confirm():
    conn = await _conn()
    await checkout_draft(conn, "t1")

    assert await is_ontology_confirmed(conn, "t1") is False


async def test_confirm_ontology_promotes_draft_to_confirmed():
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await create_relation_type(conn, "t1", relation_type="SUITABLE_FOR", example_phrase="x")

    await confirm_ontology(conn, "t1")

    confirmed = await list_relation_types(conn, "t1", status="confirmed")
    assert len(confirmed) == 11
    assert await is_ontology_confirmed(conn, "t1") is True


async def test_confirm_ontology_clears_draft():
    conn = await _conn()
    await checkout_draft(conn, "t1")

    await confirm_ontology(conn, "t1")

    assert await list_relation_types(conn, "t1", status="draft") == []


async def test_confirm_ontology_replaces_previous_confirmed_version():
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await confirm_ontology(conn, "t1")
    await checkout_draft(conn, "t1")
    from app.graphrag.ontology_relations import delete_relation_type
    await delete_relation_type(conn, "t1", "PRECEDES")

    await confirm_ontology(conn, "t1")

    confirmed = {r.relation_type for r in await list_relation_types(conn, "t1", status="confirmed")}
    assert "PRECEDES" not in confirmed
    assert len(confirmed) == 9


async def test_checkout_draft_after_confirm_copies_confirmed_into_new_draft():
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await confirm_ontology(conn, "t1")

    await checkout_draft(conn, "t1")

    draft = await list_relation_types(conn, "t1", status="draft")
    assert len(draft) == 10


async def test_checkout_draft_does_not_reseed_after_user_deletes_all_draft_rows():
    """回归测试：草稿被用户主动删空后，后续的 checkout_draft 不能把它从已确认
    版本里悄悄复制回来。真实场景（管理后台"删除约束"无变化的 bug 根因）：每次
    在本体 schema 管理页增删一条草稿记录，前端都会先调用一次 checkout 再刷新
    列表；如果删除的正好是当前唯一一条草稿记录、且该租户历史上确认过非空的
    schema，旧实现把"草稿表当前有没有行"当成"是否需要重新播种"的信号，会把刚
    删除的记录从已确认版本原样复制回来，用户在界面上完全看不出删除生效过。

    用 ETL 租户（不播种默认关系类型）+ 单条手动创建的关系类型来构造"草稿只有
    一条记录"的干净场景，避免被 extraction 模式的 10 条默认关系干扰。
    """
    from app.graphrag.ontology_relations import delete_relation_type
    from app.graphrag.tenant_ingestion_config import set_ingestion_mode

    conn = await _conn()
    await set_ingestion_mode(conn, "t1", "etl")
    await checkout_draft(conn, "t1")
    await create_relation_type(conn, "t1", relation_type="HAS_SKU", example_phrase="x")
    await confirm_ontology(conn, "t1")

    # 重新检出草稿：从已确认版本复制一条过来
    await checkout_draft(conn, "t1")
    assert [r.relation_type for r in await list_relation_types(conn, "t1", status="draft")] == ["HAS_SKU"]

    # 用户在管理后台把这条唯一的草稿记录删掉
    await delete_relation_type(conn, "t1", "HAS_SKU")
    assert await list_relation_types(conn, "t1", status="draft") == []

    # 前端删除后会紧接着再调用一次 checkout 刷新界面：草稿必须保持为空，
    # 不能被悄悄从已确认版本里重新播种回来
    await checkout_draft(conn, "t1")
    assert await list_relation_types(conn, "t1", status="draft") == []


async def test_checkout_draft_is_idempotent_when_draft_already_exists():
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await create_relation_type(conn, "t1", relation_type="CUSTOM", example_phrase="x")

    await checkout_draft(conn, "t1")

    draft = await list_relation_types(conn, "t1", status="draft")
    assert len(draft) == 11


async def test_confirm_ontology_promotes_constraints_too():
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await create_term_type(conn, tenant_id="t1", value="客房")
    await create_term_type(conn, tenant_id="t1", value="酒店")
    await add_allowed_combination(conn, "t1", subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店")

    await confirm_ontology(conn, "t1")

    confirmed = await list_allowed_combinations(conn, "t1", status="confirmed")
    assert confirmed == [
        __import__("app.graphrag.ontology_constraints", fromlist=["AllowedCombination"]).AllowedCombination(
            subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店"
        )
    ]


async def test_confirm_ontology_is_idempotent_no_op_without_draft():
    """Regression test: confirm called without draft should be a no-op, not data loss."""
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await confirm_ontology(conn, "t1")

    confirmed_after_first = await list_relation_types(conn, "t1", status="confirmed")
    assert len(confirmed_after_first) == 10

    # Second confirm without checkout should be a no-op
    await confirm_ontology(conn, "t1")

    confirmed_after_second = await list_relation_types(conn, "t1", status="confirmed")
    assert len(confirmed_after_second) == 10
    assert confirmed_after_second == confirmed_after_first


async def test_confirm_ontology_with_no_draft_does_not_delete_confirmed():
    """Regression test: confirm on a tenant with only confirmed data should not wipe it."""
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await confirm_ontology(conn, "t1")

    # Verify 10 confirmed rows exist
    confirmed = await list_relation_types(conn, "t1", status="confirmed")
    assert len(confirmed) == 10

    # Call confirm again without any draft
    await confirm_ontology(conn, "t1")

    # Confirmed data should still be intact
    confirmed_after = await list_relation_types(conn, "t1", status="confirmed")
    assert len(confirmed_after) == 10
    assert {r.relation_type for r in confirmed_after} == {r.relation_type for r in confirmed}


async def test_checkout_draft_skips_default_relation_types_for_etl_tenants():
    conn = await _conn()  # 该文件已有的辅助函数，建整套本体 schema
    from app.graphrag.tenant_ingestion_config import set_ingestion_mode
    await set_ingestion_mode(conn, "muji", "etl")

    await checkout_draft(conn, "muji")

    from app.graphrag.ontology_relations import list_relation_types
    relation_types = await list_relation_types(conn, "muji", status="draft")
    assert relation_types == []


async def test_checkout_draft_still_seeds_defaults_for_extraction_tenants():
    conn = await _conn()

    await checkout_draft(conn, "hotel_tenant")

    from app.graphrag.ontology_relations import list_relation_types
    relation_types = await list_relation_types(conn, "hotel_tenant", status="draft")
    assert len(relation_types) == 10


async def test_checkout_draft_copies_confirmed_term_types_into_new_draft():
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await create_term_type(conn, tenant_id="t1", value="客房")
    await confirm_ontology(conn, "t1")

    await checkout_draft(conn, "t1")

    draft = await list_term_types(conn, "t1", status="draft")
    assert [t.value for t in draft] == ["客房"]


async def test_checkout_draft_does_not_seed_default_term_types_for_brand_new_tenant():
    conn = await _conn()

    await checkout_draft(conn, "t1")

    assert await list_term_types(conn, "t1", status="draft") == []


async def test_confirm_ontology_promotes_term_types_too():
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await create_term_type(conn, tenant_id="t1", value="客房")

    await confirm_ontology(conn, "t1")

    confirmed = await list_term_types(conn, "t1", status="confirmed")
    assert [t.value for t in confirmed] == ["客房"]


async def test_confirm_ontology_is_idempotent_no_op_without_any_draft():
    """确认新增的 ontology_term_types 检测分支没有破坏"无草稿时 confirm 是
    no-op"这条既有回归保证——三张表（关系类型、约束、实体类型）的草稿都为空时，
    confirm_ontology 依然直接返回，不动已确认数据。"""
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await confirm_ontology(conn, "t1")

    confirmed_relations_after_first = await list_relation_types(conn, "t1", status="confirmed")
    confirmed_term_types_after_first = await list_term_types(conn, "t1", status="confirmed")

    # Second confirm without checkout should be a no-op
    await confirm_ontology(conn, "t1")

    assert await list_relation_types(conn, "t1", status="confirmed") == confirmed_relations_after_first
    assert await list_term_types(conn, "t1", status="confirmed") == confirmed_term_types_after_first


async def test_concurrent_checkout_draft_does_not_violate_primary_key():
    """管理后台本体页面的三个 tab（实体类型/关系类型/约束）各自发一次
    checkout，实测会几乎同时到达。

    checkout_draft 是"先查后写、中间无锁"：查草稿是否为空、查已确认版本
    是否存在、再插入，三步之间有 await 让出点，而 deps.get_review_conn 是
    进程内单例连接，多个请求的协程共用它、可以在这些让出点互相穿插。两个
    请求都看到"草稿为空"就会都执行复制，第二个撞主键
    UNIQUE (tenant_id, relation_type, status)，返回 500，界面显示
    "schema 草稿初始化失败"。

    这条用例用 asyncio.gather 在同一个连接上并发调用，复现那个交错。
    """
    import asyncio

    conn = await _conn()
    await create_relation_type(
        conn, "t1", relation_type="BELONG_TO", example_phrase="A BELONG_TO B"
    )
    await create_term_type(conn, tenant_id="t1", value="产品")
    await add_allowed_combination(
        conn, tenant_id="t1", subject_term_type="产品",
        relation_type="BELONG_TO", object_term_type="产品",
    )
    await confirm_ontology(conn, "t1")

    # confirm 会清掉检出标记，所以这三次并发调用都会走到"需要重新复制"的分支。
    await asyncio.gather(
        checkout_draft(conn, "t1"),
        checkout_draft(conn, "t1"),
        checkout_draft(conn, "t1"),
    )

    # 复制是幂等的：三次并发不该产生重复行，也不该抛 IntegrityError。
    assert len(await list_relation_types(conn, "t1", status="draft")) == 1
    assert len(await list_term_types(conn, "t1", status="draft")) == 1
    assert len(await list_allowed_combinations(conn, "t1", status="draft")) == 1
