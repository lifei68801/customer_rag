from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_relations import (
    InvalidRelationTypeNameError,
    RelationTypeDef,
    create_relation_type,
    ensure_relations_schema,
    list_relation_types,
    seed_default_relation_types,
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


