import aiosqlite

from app.memory.memory_store import upsert_memory_item
from app.memory.recall import recall_memory_items
from app.memory.schema import ensure_schema
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult


async def _connect():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    return conn


class FixedEmbeddingProvider:
    """query 和"网络断开"相关记忆embed成相同向量，和"套餐"相关记忆embed成正交向量。"""

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        vectors = []
        for text in request.texts:
            if "网络" in text or "路由器" in text:
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return EmbeddingResult(vectors=vectors)


async def test_recall_ranks_semantically_relevant_memory_first():
    conn = await _connect()
    await upsert_memory_item(
        conn, memory_id="m1", tenant_id="t1", user_id="u1",
        text="客户使用企业版套餐", embedding=[0.0, 1.0],
    )
    await upsert_memory_item(
        conn, memory_id="m2", tenant_id="t1", user_id="u1",
        text="客户家里的路由器型号是TP-Link", embedding=[1.0, 0.0],
    )

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FixedEmbeddingProvider())

    results = await recall_memory_items(
        conn,
        tenant_id="t1",
        user_id="u1",
        question="网络连不上怎么办？",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        top_k=2,
    )

    assert results[0]["memory_id"] == "m2"


async def test_recall_returns_empty_list_when_no_memories():
    conn = await _connect()
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FixedEmbeddingProvider())

    results = await recall_memory_items(
        conn,
        tenant_id="t1",
        user_id="u1",
        question="随便什么问题",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        top_k=5,
    )

    assert results == []


async def test_recall_respects_top_k():
    conn = await _connect()
    for i in range(5):
        await upsert_memory_item(
            conn, memory_id=f"m{i}", tenant_id="t1", user_id="u1",
            text=f"关于路由器的记忆{i}", embedding=[1.0, 0.0],
        )

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FixedEmbeddingProvider())

    results = await recall_memory_items(
        conn,
        tenant_id="t1",
        user_id="u1",
        question="网络连不上怎么办？",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        top_k=2,
    )

    assert len(results) == 2


async def test_recall_mmr_prefers_diverse_results_over_near_duplicates():
    """三条记忆里两条几乎重复（同一 embedding），一条内容不同但相关性稍低；
    MMR 应该在保留最相关那条的同时，选出内容不同的那条而不是重复的那条。"""
    conn = await _connect()
    await upsert_memory_item(
        conn, memory_id="dup1", tenant_id="t1", user_id="u1",
        text="路由器重启记忆A", embedding=[1.0, 0.0],
    )
    await upsert_memory_item(
        conn, memory_id="dup2", tenant_id="t1", user_id="u1",
        text="路由器重启记忆B", embedding=[1.0, 0.0],
    )
    await upsert_memory_item(
        conn, memory_id="diverse", tenant_id="t1", user_id="u1",
        text="客户使用企业版套餐", embedding=[0.6, 0.8],
    )

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FixedEmbeddingProvider())

    results = await recall_memory_items(
        conn,
        tenant_id="t1",
        user_id="u1",
        question="网络连不上怎么办？",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        top_k=2,
        mmr_lambda=0.3,
    )

    result_ids = {r["memory_id"] for r in results}
    assert "diverse" in result_ids
