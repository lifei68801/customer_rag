from app.retrieval.collection_init import ensure_collection


class FakeAdminClient:
    def __init__(self, *, existing: bool) -> None:
        self._existing = existing
        self.created_with: dict | None = None

    def has_collection(self, collection_name: str) -> bool:
        return self._existing

    def create_collection(self, collection_name: str, **kwargs) -> None:
        self.created_with = {"collection_name": collection_name, **kwargs}


def test_ensure_collection_skips_creation_when_already_exists():
    client = FakeAdminClient(existing=True)

    created = ensure_collection(
        client, collection_name="faq_chunks", dimension=1024
    )

    assert created is False
    assert client.created_with is None


def test_ensure_collection_creates_with_string_id_and_dynamic_fields():
    client = FakeAdminClient(existing=False)

    created = ensure_collection(
        client, collection_name="faq_chunks", dimension=1024
    )

    assert created is True
    assert client.created_with == {
        "collection_name": "faq_chunks",
        "dimension": 1024,
        "id_type": "string",
        "max_length": 512,
        "enable_dynamic_field": True,
    }
