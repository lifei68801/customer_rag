from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_constraints import add_allowed_combination
from app.graphrag.ontology_categories import create_term_type
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.ontology_relations import (
    InvalidRelationTypeNameError,
    RelationTypeDef,
    RelationTypeNameConflictError,
    RelationTypeNotFoundError,
    create_relation_type,
    delete_relation_type,
    list_relation_types,
    seed_default_relation_types,
    update_relation_type,
)

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    # 生产环境里关系类型表从不单独建——deps.py::get_review_conn 总是紧接着调用
    # ensure_ontology_schema，把 term_type_relation_allowlist 一起建出来。
    # delete_relation_type 的 allowlist 级联依赖这张表存在，测试这里也建整套
    # 本体 schema 而不是只建关系类型表，跟生产环境的调用顺序保持一致。
    await ensure_ontology_schema(conn)
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


async def test_create_relation_type_rejects_trailing_newline():
    """回归测试：Python 的 $ 在没有 re.MULTILINE 的情况下，仍然会匹配字符串末尾
    紧邻的一个换行符之前的位置，'SUITABLE_FOR\\n' 这种 payload 会被 .match() 放过
    ——改用 \\Z 后必须拒绝。"""
    conn = await _conn()

    with pytest.raises(InvalidRelationTypeNameError):
        await create_relation_type(conn, "t1", relation_type="SUITABLE_FOR\n", example_phrase="x")


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


async def test_delete_relation_type_removes_dangling_draft_allowlist_rows():
    """回归测试：删除关系类型时，草稿约束表里引用它的 draft 行必须一并删除，
    否则会在下一次 confirm_ontology 时被原样提升成 confirmed，变成永久指向
    一个已经不存在的关系类型的孤儿配置。"""
    conn = await _conn()
    await seed_default_relation_types(conn, "t1")
    await conn.execute(
        "INSERT INTO term_type_relation_allowlist "
        "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("t1", "客房", "PRECEDES", "酒店", "draft"),
    )
    await conn.commit()

    await delete_relation_type(conn, "t1", "PRECEDES")

    cursor = await conn.execute(
        "SELECT COUNT(*) FROM term_type_relation_allowlist WHERE tenant_id = 't1'"
    )
    row = await cursor.fetchone()
    assert row[0] == 0


async def test_create_relation_type_rejects_duplicate_name():
    """create_relation_type 用 INSERT OR REPLACE 会在名字冲突时静默覆盖已有
    行（source 从 default/custom 被悄悄改写，且没有任何错误信号）——跟
    ontology_categories.py::create_term_type 对重复名字正确报 Conflict 不对称。
    改成纯 INSERT 后，重复创建必须报错。"""
    conn = await _conn()
    await create_relation_type(conn, "t1", relation_type="SUITABLE_FOR", example_phrase="x")

    with pytest.raises(RelationTypeNameConflictError):
        await create_relation_type(conn, "t1", relation_type="SUITABLE_FOR", example_phrase="y")

    result = await list_relation_types(conn, "t1", status="draft")
    assert result == [
        RelationTypeDef(
            relation_type="SUITABLE_FOR", example_phrase="x", description="",
            allow_chain_query=False, source="custom",
        )
    ]


async def test_update_relation_type_renames_primary_key_and_cascades_to_allowlist():
    conn = await _conn()
    await create_term_type(conn, value="客房")
    await create_term_type(conn, value="酒店")
    await create_relation_type(conn, "t1", relation_type="SUITABLE_FOR", example_phrase="x SUITABLE_FOR y")
    await add_allowed_combination(
        conn, "t1", subject_term_type="客房", relation_type="SUITABLE_FOR", object_term_type="酒店"
    )

    await update_relation_type(
        conn, "t1", relation_type="SUITABLE_FOR", new_relation_type="GOOD_FOR",
        example_phrase="x GOOD_FOR y", description="", allow_chain_query=False,
    )

    result = {r.relation_type for r in await list_relation_types(conn, "t1", status="draft")}
    assert "GOOD_FOR" in result
    assert "SUITABLE_FOR" not in result
    cursor = await conn.execute(
        "SELECT relation_type FROM term_type_relation_allowlist WHERE tenant_id = 't1'"
    )
    row = await cursor.fetchone()
    assert row[0] == "GOOD_FOR"


async def test_update_relation_type_rename_into_existing_name_raises_conflict():
    conn = await _conn()
    await create_relation_type(conn, "t1", relation_type="SUITABLE_FOR", example_phrase="x")
    await create_relation_type(conn, "t1", relation_type="GOOD_FOR", example_phrase="y")

    with pytest.raises(RelationTypeNameConflictError):
        await update_relation_type(
            conn, "t1", relation_type="SUITABLE_FOR", new_relation_type="GOOD_FOR",
            example_phrase="x", description="", allow_chain_query=False,
        )


