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
    ensure_categories_schema,
    list_product_lines,
    list_term_types,
    update_product_line,
    update_term_type,
)

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_categories_schema(conn)
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
