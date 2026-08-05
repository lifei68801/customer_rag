from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord


async def test_search_returns_closest_record_first():
    store = InMemoryVectorStore()
    await store.upsert(
        [
            VectorRecord(
                id="a", vector=[1.0, 0.0], text="关于安装", metadata={}, tenant_id="t1"
            ),
            VectorRecord(
                id="b", vector=[0.0, 1.0], text="关于登录", metadata={}, tenant_id="t1"
            ),
        ]
    )

    results = await store.search(query_vector=[0.9, 0.1], top_k=1, tenant_id="t1")

    assert len(results) == 1
    assert results[0].id == "a"


async def test_search_does_not_return_records_from_a_different_tenant():
    store = InMemoryVectorStore()
    await store.upsert(
        [
            VectorRecord(
                id="a", vector=[1.0, 0.0], text="租户1的资料", metadata={}, tenant_id="t1"
            ),
            VectorRecord(
                id="b", vector=[1.0, 0.0], text="租户2的资料", metadata={}, tenant_id="t2"
            ),
        ]
    )

    results = await store.search(query_vector=[1.0, 0.0], top_k=5, tenant_id="t1")

    assert [r.id for r in results] == ["a"]
