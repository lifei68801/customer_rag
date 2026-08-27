import pytest

from app.retrieval.milvus_store import MilvusVectorStore
from app.retrieval.vector_store import VectorRecord


class FakeMilvusClient:
    def __init__(self) -> None:
        self.inserted: dict | None = None
        self.last_search_kwargs: dict | None = None
        self.last_delete_kwargs: dict | None = None
        self.last_query_kwargs: dict | None = None

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

    def query(self, *, collection_name: str, filter: str, **kwargs):
        self.last_query_kwargs = {"collection_name": collection_name, "filter": filter, **kwargs}
        # For list_by_source calls with a source filter, return chunked data
        if 'source == "faq/network.md"' in filter:
            return [
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
        # For list_all calls with filter='id != ""', return the original test data
        return [
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


async def test_list_all_uses_the_milvus_server_side_max_query_window_as_limit():
    # 16384 是 Milvus 服务端对单次 query() 的硬性上限（(offset+limit) 必须
    # 落在 [1, 16384]），不是我们自己挑的数字——这条测试钉住 list_all()
    # 确实把这个值传给了 limit，而不是继续用旧的、纯拍脑袋的 10000。
    #
    # 2026-08-27 曾经改用过 MilvusClient.query_iterator（pymilvus 官方的
    # "无上限"分页导出 API），线上实测直接把服务打挂：query_iterator 内部
    # 翻页时会把上一批最后一条记录的主键值拼进游标过滤表达式，但不做
    # 转义——这个项目的 id 是 `{文件路径}#{chunk序号}` 格式，Windows 环境
    # 上传路径必然含反斜杠，一崩到底。已经回退到单次 query(limit=16384)，
    # 真正"不管多大都不截断"的安全游标分页留作已知缺口。
    from app.retrieval.milvus_store import _MAX_QUERY_WINDOW

    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    await store.list_all()

    assert client.last_query_kwargs["limit"] == _MAX_QUERY_WINDOW == 16384


async def test_list_by_source_sends_expected_filter_expression():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    await store.list_by_source(source="faq/network.md", tenant_id="t1")

    assert client.last_query_kwargs["collection_name"] == "faq_chunks"
    assert client.last_query_kwargs["filter"] == (
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

    assert client.last_query_kwargs["filter"] == (
        'tenant_id == "t1" && source == "data\\\\uploads\\\\weird\\"path.md"'
    )


async def test_list_by_source_rejects_unsafe_tenant_id():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    with pytest.raises(ValueError):
        await store.list_by_source(source="doc.md", tenant_id='t1" or "1"=="1')
