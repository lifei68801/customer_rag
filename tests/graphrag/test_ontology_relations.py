from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_relations import (
    InvalidRelationTypeNameError,
    RelationTypeDef,
    RelationTypeNotFoundError,
    create_relation_type,
    delete_relation_type,
    ensure_relations_schema,
    list_relation_types,
    seed_default_relation_types,
    update_relation_type,
)

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_relations_schema(conn)
    return conn


async def test_seed_default_relation_types_writes_ten_draft_rows():
    conn = await _conn()

    await seed_default_relation_types(conn, "t1")

    result = await list_relation_types(conn, "t1", status="draft")
    assert len(result) == 10
    assert all(r.source == "default" for r in result)
    chain_eligible = {r.relation_type for r in result if r.allow_chain_query}
    assert chain_eligible == {"REQUIRES", "PRECEDES", "PART_OF"}


async def test_seed_default_relation_types_is_idempotent():
    conn = await _conn()
    await seed_default_relation_types(conn, "t1")

    await seed_default_relation_types(conn, "t1")

    assert len(await list_relation_types(conn, "t1", status="draft")) == 10


async def test_seed_is_scoped_per_tenant():
    conn = await _conn()
    await seed_default_relation_types(conn, "t1")

    assert await list_relation_types(conn, "t2", status="draft") == []


async def test_create_relation_type_with_valid_name():
    conn = await _conn()

    await create_relation_type(
        conn, "t1", relation_type="SUITABLE_FOR", example_phrase="大床房 SUITABLE_FOR 家庭出行"
    )

    result = await list_relation_types(conn, "t1", status="draft")
    assert result == [
        RelationTypeDef(
            relation_type="SUITABLE_FOR",
            example_phrase="大床房 SUITABLE_FOR 家庭出行",
            description="",
            allow_chain_query=False,
            source="custom",
        )
    ]


async def test_create_relation_type_rejects_invalid_format():
    conn = await _conn()

    with pytest.raises(InvalidRelationTypeNameError):
        await create_relation_type(conn, "t1", relation_type="suitable-for", example_phrase="x")


async def test_create_relation_type_rejects_empty_example_phrase():
    conn = await _conn()

    with pytest.raises(InvalidRelationTypeNameError):
        await create_relation_type(conn, "t1", relation_type="SUITABLE_FOR", example_phrase="")


async def test_update_relation_type_changes_example_and_chain_flag():
    conn = await _conn()
    await create_relation_type(conn, "t1", relation_type="SUITABLE_FOR", example_phrase="x SUITABLE_FOR y")

    await update_relation_type(
        conn, "t1",
        relation_type="SUITABLE_FOR",
        example_phrase="大床房 SUITABLE_FOR 家庭出行",
        description="适合的出行类型",
        allow_chain_query=True,
    )

    result = await list_relation_types(conn, "t1", status="draft")
    assert result == [
        RelationTypeDef(
            relation_type="SUITABLE_FOR",
            example_phrase="大床房 SUITABLE_FOR 家庭出行",
            description="适合的出行类型",
            allow_chain_query=True,
            source="custom",
        )
    ]


async def test_update_nonexistent_relation_type_raises_not_found():
    conn = await _conn()

    with pytest.raises(RelationTypeNotFoundError):
        await update_relation_type(
            conn, "t1", relation_type="NOPE", example_phrase="x", description="",
            allow_chain_query=False,
        )


async def test_delete_relation_type_removes_default_row_without_protection():
    """关系类型删除不设引用保护——已写入 Neo4j 的旧边不受影响（见 spec 文档
    第 7 节孤点数据保护规则表格），这里只验证 schema 表本身的删除行为。"""
    conn = await _conn()
    await seed_default_relation_types(conn, "t1")

    await delete_relation_type(conn, "t1", "PRECEDES")

    remaining = {r.relation_type for r in await list_relation_types(conn, "t1", status="draft")}
    assert "PRECEDES" not in remaining
    assert len(remaining) == 9


async def test_delete_relation_type_is_scoped_per_tenant():
    conn = await _conn()
    await seed_default_relation_types(conn, "t1")
    await seed_default_relation_types(conn, "t2")

    await delete_relation_type(conn, "t1", "PRECEDES")

    assert len(await list_relation_types(conn, "t2", status="draft")) == 10


