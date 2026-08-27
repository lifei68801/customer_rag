from app.retrieval.collection_init import main
from tests.settings_factory import build_settings


class FakeAdminClient:
    def __init__(self, *, existing: bool) -> None:
        self._existing = existing
        self.created_with: dict | None = None

    def has_collection(self, collection_name: str) -> bool:
        return self._existing

    def create_collection(self, collection_name: str, **kwargs) -> None:
        self.created_with = {"collection_name": collection_name, **kwargs}


def _settings():
    return build_settings()


def test_main_creates_collection_using_settings_dimension_and_name():
    captured: dict = {}

    def fake_client_factory(uri: str) -> FakeAdminClient:
        captured["uri"] = uri
        captured["client"] = FakeAdminClient(existing=False)
        return captured["client"]

    created = main(settings=_settings(), client_factory=fake_client_factory)

    assert created is True
    assert captured["uri"] == "http://localhost:19530"
    assert captured["client"].created_with == {
        "collection_name": "faq_chunks",
        "dimension": 1024,
        "id_type": "string",
        "max_length": 512,
        "enable_dynamic_field": True,
    }
