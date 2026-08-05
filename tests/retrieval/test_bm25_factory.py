from app.retrieval.bm25 import build_bm25_index_from_store
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord


async def test_build_bm25_index_from_store_indexes_all_records():
    store = InMemoryVectorStore()
    await store.upsert(
        [
            VectorRecord(
                id="errors/e502.md",
                vector=[],
                text="错误码 E502 表示网关超时",
                tenant_id="t1",
                metadata={},
            )
        ]
    )

    index = await build_bm25_index_from_store(store)
    hits = index.search("E502 网关超时", top_k=1, tenant_id="t1")

    assert len(hits) == 1
    assert hits[0].id == "errors/e502.md"
