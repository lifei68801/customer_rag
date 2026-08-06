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


async def test_search_attaches_cosine_similarity_score_to_results():
    # 相关性判断（Agent 兜底路径的相关性阈值）需要一个可比较的分数，不能只
    # 依赖"检索结果是否为空"——真实向量库几乎总能返回 Top-K 个最近邻，
    # 哪怕语义上完全不相关。
    store = InMemoryVectorStore()
    await store.upsert(
        [
            VectorRecord(
                id="a", vector=[1.0, 0.0], text="关于安装", metadata={}, tenant_id="t1"
            ),
            VectorRecord(
                id="b", vector=[0.0, 1.0], text="不相关内容", metadata={}, tenant_id="t1"
            ),
        ]
    )

    results = await store.search(query_vector=[1.0, 0.0], top_k=2, tenant_id="t1")

    scores = {r.id: r.score for r in results}
    assert scores["a"] == 1.0
    assert scores["b"] == 0.0


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


async def test_delete_by_source_removes_only_matching_records():
    store = InMemoryVectorStore()
    await store.upsert(
        [
            VectorRecord(
                id="a#0", vector=[1.0, 0.0], text="旧版本第一段",
                tenant_id="t1", metadata={"source": "doc.md"},
            ),
            VectorRecord(
                id="a#1", vector=[1.0, 0.0], text="旧版本第二段",
                tenant_id="t1", metadata={"source": "doc.md"},
            ),
            VectorRecord(
                id="b#0", vector=[1.0, 0.0], text="另一份文档",
                tenant_id="t1", metadata={"source": "other.md"},
            ),
        ]
    )

    await store.delete_by_source(source="doc.md", tenant_id="t1")

    remaining = await store.list_all()
    assert [r.id for r in remaining] == ["b#0"]


async def test_delete_by_source_only_affects_the_matching_tenant():
    store = InMemoryVectorStore()
    await store.upsert(
        [
            VectorRecord(
                id="a#0", vector=[1.0, 0.0], text="租户1的文档",
                tenant_id="t1", metadata={"source": "doc.md"},
            ),
            VectorRecord(
                id="a#0", vector=[1.0, 0.0], text="租户2同名文档",
                tenant_id="t2", metadata={"source": "doc.md"},
            ),
        ]
    )

    await store.delete_by_source(source="doc.md", tenant_id="t1")

    remaining = await store.list_all()
    assert [r.tenant_id for r in remaining] == ["t2"]
