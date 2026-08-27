import pytest

from app.retrieval.milvus_store import MilvusVectorStore
from app.retrieval.vector_store import VectorRecord


class FakeQueryIterator:
    """模拟 pymilvus 的 QueryIterator：next() 按固定批大小分批吐出预置的行，
    取完后再调用一次 next() 返回空列表表示结束（不是抛异常），跟真实
    QueryIterator 的终止协议一致——见 MilvusVectorStore._query_all 的用法。"""

    def __init__(self, rows: list[dict], *, batch_size: int = 1) -> None:
        self._batches = [rows[i : i + batch_size] for i in range(0, len(rows), batch_size)]
        self._index = 0
        self.closed = False

    def next(self) -> list[dict]:
        if self._index >= len(self._batches):
            return []
        batch = self._batches[self._index]
        self._index += 1
        return batch

    def close(self) -> None:
        self.closed = True


class FakeMilvusClient:
    def __init__(self, *, query_iterator_batch_size: int = 1) -> None:
        self.inserted: dict | None = None
        self.last_search_kwargs: dict | None = None
        self.last_delete_kwargs: dict | None = None
        self.last_query_iterator_kwargs: dict | None = None
        self.last_iterator: FakeQueryIterator | None = None
        self._query_iterator_batch_size = query_iterator_batch_size

    def insert(self, *, collection_name: str, data: list[dict]) -> None:
        self.inserted = {"collection_name": collection_name, "data": data}

    def delete(self, *, collection_name: str, filter: str, **kwargs):
        self.last_delete_kwargs = {"collection_name": collection_name, "filter": filter}

    def search(self, **kwargs):
        self.last_search_kwargs = kwargs
        return [
            [
                {
                    "id": "faq/network.md",
                    "distance": 0.98,
                    "entity": {"text": "网络断开时请先重启路由器。"},
                },
                {
                    "id": "faq/login.md",
                    "distance": 0.42,
                    "entity": {"text": "登录失败请检查账号密码。"},
                },
            ]
        ]

    def query_iterator(self, *, collection_name: str, filter: str, **kwargs):
        self.last_query_iterator_kwargs = {"collection_name": collection_name, "filter": filter}
        # For list_by_source calls with a source filter, return chunked data
        if 'source == "faq/network.md"' in filter:
            rows = [
                {
                    "id": "faq/network.md#1",
                    "text": "第二段",
                    "tenant_id": "t1",
                    "source": "faq/network.md",
                },
                {
                    "id": "faq/network.md#0",
                    "text": "第一段",
                    "tenant_id": "t1",
                    "source": "faq/network.md",
                },
            ]
        else:
            # For list_all calls with filter='id != ""', return the original test data
            rows = [
                {
                    "id": "faq/network.md",
                    "text": "网络断开时请先重启路由器。",
                    "tenant_id": "t1",
                    "source": "faq/network.md",
                },
                {
                    "id": "faq/login.md",
                    "text": "登录失败请检查账号密码。",
                    "tenant_id": "t1",
                    "source": "faq/login.md",
                },
            ]
        self.last_iterator = FakeQueryIterator(rows, batch_size=self._query_iterator_batch_size)
        return self.last_iterator


async def test_upsert_sends_records_to_the_configured_collection():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    await store.upsert(
        [
            VectorRecord(
                id="faq/network.md",
                vector=[0.1, 0.2],
                text="网络断开时请先重启路由器。",
                tenant_id="t1",
                metadata={"source": "faq/network.md"},
            )
        ]
    )

    assert client.inserted["collection_name"] == "faq_chunks"
    assert client.inserted["data"] == [
        {
            "id": "faq/network.md",
            "vector": [0.1, 0.2],
            "text": "网络断开时请先重启路由器。",
            "tenant_id": "t1",
            "source": "faq/network.md",
        }
    ]


async def test_search_maps_milvus_hits_to_vector_records():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    results = await store.search(query_vector=[0.9, 0.1], top_k=2, tenant_id="t1")

    assert len(results) == 2
    assert results[0].id == "faq/network.md"
    assert results[0].text == "网络断开时请先重启路由器。"
    assert results[0].score == 0.98
    assert results[1].id == "faq/login.md"
    assert results[1].score == 0.42


async def test_search_passes_tenant_id_as_a_scalar_filter_expression():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    await store.search(query_vector=[0.9, 0.1], top_k=2, tenant_id="t1")

    assert client.last_search_kwargs["filter"] == 'tenant_id == "t1"'


async def test_search_rejects_tenant_id_with_unsafe_characters():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    with pytest.raises(ValueError):
        await store.search(query_vector=[0.9, 0.1], top_k=2, tenant_id='t1" or "1"=="1')


async def test_delete_by_source_sends_expected_filter_expression():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    await store.delete_by_source(source="faq/network.md", tenant_id="t1")

    assert client.last_delete_kwargs["collection_name"] == "faq_chunks"
    assert client.last_delete_kwargs["filter"] == (
        'tenant_id == "t1" && source == "faq/network.md"'
    )


async def test_delete_by_source_escapes_double_quotes_in_source():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    await store.delete_by_source(source='weird"path.md', tenant_id="t1")

    assert client.last_delete_kwargs["filter"] == (
        'tenant_id == "t1" && source == "weird\\"path.md"'
    )


async def test_delete_by_source_escapes_backslashes_in_windows_style_paths():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    await store.delete_by_source(
        source=r"data\uploads\demo\9c022d73_faq-network.md", tenant_id="t1"
    )

    assert client.last_delete_kwargs["filter"] == (
        'tenant_id == "t1" && source == '
        '"data\\\\uploads\\\\demo\\\\9c022d73_faq-network.md"'
    )


async def test_delete_by_source_rejects_unsafe_tenant_id():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    with pytest.raises(ValueError):
        await store.delete_by_source(source="doc.md", tenant_id='t1" or "1"=="1')


async def test_list_all_maps_milvus_query_rows_to_vector_records():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    records = await store.list_all()

    assert {r.id for r in records} == {"faq/network.md", "faq/login.md"}
    network_record = next(r for r in records if r.id == "faq/network.md")
    assert network_record.text == "网络断开时请先重启路由器。"
    assert network_record.tenant_id == "t1"
    assert network_record.metadata == {"source": "faq/network.md"}


async def test_list_all_concatenates_every_batch_from_the_query_iterator():
    # batch_size=1 跟测试数据的 2 行组合起来，逼着 FakeQueryIterator 至少
    # 分两批吐出结果——如果 list_all() 只读了 iterator.next() 一次就当
    # 结束，这里只会拿到 1 条而不是 2 条。这条测试钉住"不管 Milvus 内部
    # 分几批，list_all() 必须读到耗尽为止"这个行为，而不是像旧的
    # limit=10000 写法那样只发一次请求、超过上限就静默丢数据。
    client = FakeMilvusClient(query_iterator_batch_size=1)
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    records = await store.list_all()

    assert len(records) == 2
    assert client.last_iterator.closed is True


async def test_list_by_source_sends_expected_filter_expression():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    await store.list_by_source(source="faq/network.md", tenant_id="t1")

    assert client.last_query_iterator_kwargs["collection_name"] == "faq_chunks"
    assert client.last_query_iterator_kwargs["filter"] == (
        'tenant_id == "t1" && source == "faq/network.md"'
    )


async def test_list_by_source_returns_records_in_chunk_order():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    records = await store.list_by_source(source="faq/network.md", tenant_id="t1")

    # FakeMilvusClient.query 故意按 #1、#0 的顺序返回（模拟向量库不保证
    # 顺序），list_by_source 必须自己按 chunk 序号重新排序成 #0、#1
    assert [r.id for r in records] == ["faq/network.md#0", "faq/network.md#1"]
    assert [r.text for r in records] == ["第一段", "第二段"]


async def test_list_by_source_escapes_backslashes_and_quotes_in_source():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    await store.list_by_source(source=r'data\uploads\weird"path.md', tenant_id="t1")

    assert client.last_query_iterator_kwargs["filter"] == (
        'tenant_id == "t1" && source == "data\\\\uploads\\\\weird\\"path.md"'
    )


async def test_list_by_source_rejects_unsafe_tenant_id():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    with pytest.raises(ValueError):
        await store.list_by_source(source="doc.md", tenant_id='t1" or "1"=="1')
