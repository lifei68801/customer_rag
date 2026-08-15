from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_categories import (
    CategoryInUseError,
    CategoryNameConflictError,
    CategoryNotFoundError,
    TermTypeCategory,
    create_product_line,
    create_term_type,
    delete_product_line,
    delete_term_type,
    list_product_lines,
    list_term_types,
    update_product_line,
    update_term_type,
)
from app.graphrag.ontology_lifecycle import ensure_ontology_schema

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    # 生产环境里分类表从不单独建——deps.py::get_review_conn 总是紧接着调用
    # ensure_ontology_schema，把 term_type_relation_allowlist 一起建出来（见该
    # 函数的说明）。update_term_type/delete_term_type 的 allowlist 级联依赖这张
    # 表存在，测试这里也建整套本体 schema 而不是只建分类表，跟生产环境的调用
    # 顺序保持一致。
    await ensure_ontology_schema(conn)
    return conn


async def test_create_and_list_term_type_with_extra_fields():
    conn = await _conn()
    await create_term_type(conn, value="错误码", extra_fields=["严重等级", "影响范围"])

    result = await list_term_types(conn)

    assert result == [TermTypeCategory(value="错误码", extra_fields=["严重等级", "影响范围"])]


async def test_create_term_type_without_extra_fields_defaults_to_empty_list():
    conn = await _conn()
    await create_term_type(conn, value="地点")

    result = await list_term_types(conn)

    assert result == [TermTypeCategory(value="地点", extra_fields=[])]


async def test_create_and_list_product_line():
    conn = await _conn()
    await create_product_line(conn, value="示例产品线")

    assert await list_product_lines(conn) == ["示例产品线"]


async def test_create_duplicate_term_type_raises_conflict():
    conn = await _conn()
    await create_term_type(conn, value="错误码")

    with pytest.raises(CategoryNameConflictError):
        await create_term_type(conn, value="错误码")


async def test_update_term_type_renames_without_referencing_terms():
    conn = await _conn()
    await create_term_type(conn, value="错误码", extra_fields=["严重等级"])
    await conn.execute(
        "CREATE TABLE terms (standard_name TEXT PRIMARY KEY, term_type TEXT NOT NULL, "
        "product_line TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]')"
    )
    await conn.commit()

    await update_term_type(conn, value="错误码", new_value="故障码", extra_fields=["严重等级", "影响范围"])

    result = await list_term_types(conn)
    assert result == [TermTypeCategory(value="故障码", extra_fields=["严重等级", "影响范围"])]


async def test_update_term_type_cascades_rename_to_referencing_terms():
    conn = await _conn()
    await create_term_type(conn, value="错误码")
    await conn.execute(
        "CREATE TABLE terms (standard_name TEXT PRIMARY KEY, term_type TEXT NOT NULL, "
        "product_line TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]')"
    )
    await conn.execute(
        "INSERT INTO terms (standard_name, term_type, product_line) VALUES (?, ?, ?)",
        ("错误码E502", "错误码", "示例产品线"),
    )
    await conn.commit()

    await update_term_type(conn, value="错误码", new_value="故障码", extra_fields=[])

    cursor = await conn.execute("SELECT term_type FROM terms WHERE standard_name = ?", ("错误码E502",))
    row = await cursor.fetchone()
    assert row[0] == "故障码"


async def test_update_term_type_into_existing_name_raises_conflict():
    conn = await _conn()
    await create_term_type(conn, value="错误码")
    await create_term_type(conn, value="模块")

    with pytest.raises(CategoryNameConflictError):
        await update_term_type(conn, value="错误码", new_value="模块", extra_fields=[])


async def test_delete_term_type_not_in_use_succeeds():
    conn = await _conn()
    await create_term_type(conn, value="错误码")
    await conn.execute(
        "CREATE TABLE terms (standard_name TEXT PRIMARY KEY, term_type TEXT NOT NULL, "
        "product_line TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]')"
    )
    await conn.commit()

    await delete_term_type(conn, "错误码")

    assert await list_term_types(conn) == []


async def test_delete_term_type_in_use_raises_conflict():
    conn = await _conn()
    await create_term_type(conn, value="错误码")
    await conn.execute(
        "CREATE TABLE terms (standard_name TEXT PRIMARY KEY, term_type TEXT NOT NULL, "
        "product_line TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]')"
    )
    await conn.execute(
        "INSERT INTO terms (standard_name, term_type, product_line) VALUES (?, ?, ?)",
        ("错误码E502", "错误码", "示例产品线"),
    )
    await conn.commit()

    with pytest.raises(CategoryInUseError):
        await delete_term_type(conn, "错误码")


async def test_update_term_type_cascades_rename_to_allowlist_references():
    conn = await _conn()
    await create_term_type(conn, value="客房")
    await create_term_type(conn, value="酒店")
    await conn.executescript(
        "CREATE TABLE terms (standard_name TEXT PRIMARY KEY, term_type TEXT NOT NULL, "
        "product_line TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]');"
    )
    await conn.execute(
        "INSERT INTO term_type_relation_allowlist "
        "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("t1", "客房", "PART_OF", "酒店", "confirmed"),
    )
    await conn.commit()

    await update_term_type(conn, value="客房", new_value="大床房", extra_fields=[])

    cursor = await conn.execute(
        "SELECT subject_term_type FROM term_type_relation_allowlist WHERE tenant_id = 't1'"
    )
    row = await cursor.fetchone()
    assert row[0] == "大床房"


async def test_delete_term_type_referenced_only_by_allowlist_raises_conflict():
    conn = await _conn()
    await create_term_type(conn, value="客房")
    await create_term_type(conn, value="酒店")
    await conn.executescript(
        "CREATE TABLE terms (standard_name TEXT PRIMARY KEY, term_type TEXT NOT NULL, "
        "product_line TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]');"
    )
    await conn.execute(
        "INSERT INTO term_type_relation_allowlist "
        "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("t1", "客房", "PART_OF", "酒店", "draft"),
    )
    await conn.commit()

    with pytest.raises(CategoryInUseError):
        await delete_term_type(conn, "客房")


async def test_delete_product_line_in_use_raises_conflict():
    conn = await _conn()
    await create_product_line(conn, value="示例产品线")
    await conn.execute(
        "CREATE TABLE terms (standard_name TEXT PRIMARY KEY, term_type TEXT NOT NULL, "
        "product_line TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]')"
    )
    await conn.execute(
        "INSERT INTO terms (standard_name, term_type, product_line) VALUES (?, ?, ?)",
        ("错误码E502", "错误码", "示例产品线"),
    )
    await conn.commit()

    with pytest.raises(CategoryInUseError):
        await delete_product_line(conn, "示例产品线")
