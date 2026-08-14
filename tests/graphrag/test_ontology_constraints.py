from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_categories import create_term_type, ensure_categories_schema
from app.graphrag.ontology_constraints import (
    AllowedCombination,
    UnknownCategoryError,
    UnknownRelationTypeError,
    add_allowed_combination,
    ensure_constraints_schema,
    list_allowed_combinations,
    remove_allowed_combination,
)
from app.graphrag.ontology_relations import create_relation_type, ensure_relations_schema

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_categories_schema(conn)
    await ensure_relations_schema(conn)
    await ensure_constraints_schema(conn)
    return conn


async def test_add_allowed_combination_with_valid_references():
    conn = await _conn()
    await create_term_type(conn, value="客房")
    await create_term_type(conn, value="酒店")
    await create_relation_type(conn, "t1", relation_type="PART_OF", example_phrase="客房 PART_OF 酒店")

    await add_allowed_combination(
        conn, "t1", subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店"
    )

    result = await list_allowed_combinations(conn, "t1", status="draft")
    assert result == [
        AllowedCombination(subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店")
    ]


async def test_add_allowed_combination_rejects_unknown_subject_type():
    conn = await _conn()
    await create_term_type(conn, value="酒店")
    await create_relation_type(conn, "t1", relation_type="PART_OF", example_phrase="x")

    with pytest.raises(UnknownCategoryError):
        await add_allowed_combination(
            conn, "t1", subject_term_type="不存在的分类", relation_type="PART_OF",
            object_term_type="酒店",
        )


async def test_add_allowed_combination_rejects_unknown_relation_type():
    conn = await _conn()
    await create_term_type(conn, value="客房")
    await create_term_type(conn, value="酒店")

    with pytest.raises(UnknownRelationTypeError):
        await add_allowed_combination(
            conn, "t1", subject_term_type="客房", relation_type="NOT_SEEDED",
            object_term_type="酒店",
        )


async def test_add_allowed_combination_is_idempotent():
    conn = await _conn()
    await create_term_type(conn, value="客房")
    await create_term_type(conn, value="酒店")
    await create_relation_type(conn, "t1", relation_type="PART_OF", example_phrase="x")

    await add_allowed_combination(conn, "t1", subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店")
    await add_allowed_combination(conn, "t1", subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店")

    assert len(await list_allowed_combinations(conn, "t1", status="draft")) == 1


async def test_remove_allowed_combination():
    conn = await _conn()
    await create_term_type(conn, value="客房")
    await create_term_type(conn, value="酒店")
    await create_relation_type(conn, "t1", relation_type="PART_OF", example_phrase="x")
    await add_allowed_combination(conn, "t1", subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店")

    await remove_allowed_combination(conn, "t1", subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店")

    assert await list_allowed_combinations(conn, "t1", status="draft") == []
