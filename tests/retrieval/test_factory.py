from app.config.settings import Settings
from app.retrieval.factory import build_vector_store_from_settings
from app.retrieval.vector_store import VectorRecord


class FakeMilvusClient:
    def __init__(self) -> None:
        self.inserted: dict | None = None

    def insert(self, *, collection_name: str, data: list[dict]) -> None:
        self.inserted = {"collection_name": collection_name, "data": data}

    def search(self, **kwargs):
        return [[]]


async def test_build_vector_store_from_settings_uses_configured_uri_and_collection():
    captured: dict = {}

    def fake_client_factory(uri: str) -> FakeMilvusClient:
        captured["uri"] = uri
        captured["client"] = FakeMilvusClient()
        return captured["client"]

    settings = Settings(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=1024,
        milvus_uri="http://localhost:19530",
        milvus_collection="faq_chunks",
    )

    store = build_vector_store_from_settings(
        settings, client_factory=fake_client_factory
    )
    await store.upsert(
        [VectorRecord(id="a", vector=[0.1], text="text", metadata={})]
    )

    assert captured["uri"] == "http://localhost:19530"
    assert captured["client"].inserted["collection_name"] == "faq_chunks"
