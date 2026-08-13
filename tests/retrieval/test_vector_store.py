from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord, chunk_index_from_id


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


async def test_list_by_source_returns_only_matching_records_in_chunk_order():
    store = InMemoryVectorStore()
    await store.upsert(
        [
            VectorRecord(
                id="a.md#1", vector=[0.1], text="第二段",
                tenant_id="t1", metadata={"source": "a.md"},
            ),
            VectorRecord(
                id="b.md#0", vector=[0.1], text="不相关文档",
                tenant_id="t1", metadata={"source": "b.md"},
            ),
            VectorRecord(
                id="a.md#0", vector=[0.1], text="第一段",
                tenant_id="t1", metadata={"source": "a.md"},
            ),
            VectorRecord(
                id="a.md#10", vector=[0.1], text="第十一段",
                tenant_id="t1", metadata={"source": "a.md"},
            ),
        ]
    )

    records = await store.list_by_source(source="a.md", tenant_id="t1")

    # a.md#1 排在 a.md#10 前面证明是按数字序号排序，不是按字符串字典序
    # （字典序会把 "a.md#1" 排在 "a.md#10" 之后，"1" < "10" 的字符串比较
    # 结果和期望的数值顺序相反）
    assert [r.text for r in records] == ["第一段", "第二段", "第十一段"]


async def test_list_by_source_only_matches_the_given_tenant():
    store = InMemoryVectorStore()
    await store.upsert(
        [
            VectorRecord(
                id="a.md#0", vector=[0.1], text="t1的内容",
                tenant_id="t1", metadata={"source": "a.md"},
            ),
            VectorRecord(
                id="a.md#0", vector=[0.1], text="t2的内容",
                tenant_id="t2", metadata={"source": "a.md"},
            ),
        ]
    )

    records = await store.list_by_source(source="a.md", tenant_id="t1")

    assert [r.text for r in records] == ["t1的内容"]


def test_chunk_index_from_id_parses_trailing_numeric_suffix():
    assert chunk_index_from_id("data/uploads/a.md#0") == 0
    assert chunk_index_from_id("data/uploads/a.md#10") == 10


def test_chunk_index_from_id_defaults_to_zero_for_malformed_id():
    assert chunk_index_from_id("no-hash-separator") == 0
