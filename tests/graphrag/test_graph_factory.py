from app.config.settings import Settings
from app.graphrag.factory import build_graph_client_from_settings, load_terms_from_settings


def _base_kwargs() -> dict:
    return dict(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=1024,
    )


def test_build_graph_client_from_settings_uses_configured_uri_and_auth():
    captured: dict = {}

    def fake_driver_factory(uri: str, *, auth: tuple[str, str]):
        captured["uri"] = uri
        captured["auth"] = auth
        return object()

    settings = Settings(
        **_base_kwargs(),
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="secret",
    )

    build_graph_client_from_settings(settings, driver_factory=fake_driver_factory)

    assert captured["uri"] == "bolt://localhost:7687"
    assert captured["auth"] == ("neo4j", "secret")


def test_load_terms_from_settings_reads_configured_path(tmp_path):
    terminology_path = tmp_path / "terms.yaml"
    terminology_path.write_text(
        "terms:\n"
        "  - standard_name: 测试术语\n"
        "    aliases: [别名]\n"
        "    term_type: module\n",
        encoding="utf-8",
    )
    settings = Settings(
        **_base_kwargs(),
        terminology_path=str(terminology_path),
    )

    terms = load_terms_from_settings(settings)

    assert [t.standard_name for t in terms] == ["测试术语"]


def test_build_graph_client_from_settings_defaults_to_neo4j():
    settings = Settings(**_base_kwargs())

    client = build_graph_client_from_settings(
        settings, driver_factory=lambda uri, *, auth: object()
    )

    from app.graphrag.neo4j_client import Neo4jGraphClient
    assert isinstance(client, Neo4jGraphClient)


def test_build_graph_client_from_settings_selects_neptune_backend():
    captured: dict = {}

    def fake_neptune_client_factory(endpoint: str, *, port: int):
        captured["endpoint"] = endpoint
        captured["port"] = port
        return object()

    settings = Settings(
        **_base_kwargs(),
        graph_backend="neptune",
        neptune_endpoint="neptune.example.com",
        neptune_port=8182,
    )

    client = build_graph_client_from_settings(
        settings, neptune_client_factory=fake_neptune_client_factory
    )

    from app.graphrag.neptune_client import NeptuneGraphClient
    assert isinstance(client, NeptuneGraphClient)
    assert captured == {"endpoint": "neptune.example.com", "port": 8182}


def test_build_graph_client_from_settings_neptune_does_not_call_driver_factory():
    driver_factory_called = False

    def fake_driver_factory(uri: str, *, auth):
        nonlocal driver_factory_called
        driver_factory_called = True
        return object()

    settings = Settings(
        **_base_kwargs(), graph_backend="neptune", neptune_endpoint="neptune.example.com",
    )

    build_graph_client_from_settings(
        settings, driver_factory=fake_driver_factory,
        neptune_client_factory=lambda endpoint, *, port: object(),
    )

    assert driver_factory_called is False
