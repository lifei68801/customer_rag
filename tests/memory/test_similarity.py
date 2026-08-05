import aiosqlite

from app.memory.memory_store import upsert_memory_item
from app.memory.schema import ensure_schema
from app.memory.similarity import find_similar_memory_items


async def _connect():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    return conn


async def test_returns_top_k_most_similar_items_by_cosine_similarity():
    conn = await _connect()
    await upsert_memory_item(
        conn, memory_id="m1", tenant_id="t1", user_id="u1", text="很像的", embedding=[1.0, 0.0]
    )
    await upsert_memory_item(
        conn, memory_id="m2", tenant_id="t1", user_id="u1", text="有点像的", embedding=[0.7, 0.3]
    )
    await upsert_memory_item(
        conn, memory_id="m3", tenant_id="t1", user_id="u1", text="不像的", embedding=[0.0, 1.0]
    )

    results = await find_similar_memory_items(
        conn, tenant_id="t1", user_id="u1", query_vector=[1.0, 0.0], top_k=2
    )

    assert [r["memory_id"] for r in results] == ["m1", "m2"]


async def test_ignores_items_without_an_embedding():
    conn = await _connect()
    await upsert_memory_item(
        conn, memory_id="m1", tenant_id="t1", user_id="u1", text="有向量", embedding=[1.0, 0.0]
    )
    await upsert_memory_item(
        conn, memory_id="m2", tenant_id="t1", user_id="u1", text="没有向量"
    )

    results = await find_similar_memory_items(
        conn, tenant_id="t1", user_id="u1", query_vector=[1.0, 0.0], top_k=5
    )

    assert [r["memory_id"] for r in results] == ["m1"]


async def test_returns_empty_list_when_no_items_have_embeddings():
    conn = await _connect()
    await upsert_memory_item(conn, memory_id="m1", tenant_id="t1", user_id="u1", text="没有向量")

    results = await find_similar_memory_items(
        conn, tenant_id="t1", user_id="u1", query_vector=[1.0, 0.0], top_k=5
    )

    assert results == []
