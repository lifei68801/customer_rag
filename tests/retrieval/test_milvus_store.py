from app.retrieval.milvus_store import MilvusVectorStore
from app.retrieval.vector_store import VectorRecord


class FakeMilvusClient:
    def __init__(self) -> None:
        self.inserted: dict | None = None

    def insert(self, *, collection_name: str, data: list[dict]) -> None:
        self.inserted = {"collection_name": collection_name, "data": data}

    def search(self, **kwargs):
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
        return [
            {"id": "faq/network.md", "text": "网络断开时请先重启路由器。", "source": "faq/network.md"},
            {"id": "faq/login.md", "text": "登录失败请检查账号密码。", "source": "faq/login.md"},
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
            "source": "faq/network.md",
        }
    ]


async def test_search_maps_milvus_hits_to_vector_records():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    results = await store.search(query_vector=[0.9, 0.1], top_k=2)

    assert len(results) == 2
    assert results[0].id == "faq/network.md"
    assert results[0].text == "网络断开时请先重启路由器。"
    assert results[1].id == "faq/login.md"


async def test_list_all_maps_milvus_query_rows_to_vector_records():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    records = await store.list_all()

    assert {r.id for r in records} == {"faq/network.md", "faq/login.md"}
    network_record = next(r for r in records if r.id == "faq/network.md")
    assert network_record.text == "网络断开时请先重启路由器。"
    assert network_record.metadata == {"source": "faq/network.md"}
