from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord


async def test_search_returns_closest_record_first():
    store = InMemoryVectorStore()
    await store.upsert(
        [
            VectorRecord(
                id="a", vector=[1.0, 0.0], text="关于安装", metadata={}
            ),
            VectorRecord(
                id="b", vector=[0.0, 1.0], text="关于登录", metadata={}
            ),
        ]
    )

    results = await store.search(query_vector=[0.9, 0.1], top_k=1)

    assert len(results) == 1
    assert results[0].id == "a"
