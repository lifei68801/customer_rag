from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord


async def test_list_all_returns_every_upserted_record():
    store = InMemoryVectorStore()
    await store.upsert(
        [
            VectorRecord(id="a", vector=[1.0], text="内容A", metadata={}),
            VectorRecord(id="b", vector=[0.0], text="内容B", metadata={}),
        ]
    )

    records = await store.list_all()

    assert {r.id for r in records} == {"a", "b"}
