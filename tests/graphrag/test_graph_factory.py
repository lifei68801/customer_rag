import pytest

from app.graphrag.factory import build_graph_client_from_settings, load_terms_from_settings
from tests.settings_factory import build_settings


def test_build_graph_client_from_settings_uses_configured_uri_and_auth():
    captured: dict = {}

    def fake_driver_factory(uri: str, *, auth: tuple[str, str]):
        captured["uri"] = uri
        captured["auth"] = auth
        return object()

    settings = build_settings(
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
    settings = build_settings(terminology_path=str(terminology_path))

    terms = load_terms_from_settings(settings)

    assert [t.standard_name for t in terms] == ["测试术语"]


def test_build_graph_client_from_settings_defaults_to_neo4j():
    settings = build_settings()

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

    settings = build_settings(
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

    settings = build_settings(graph_backend="neptune", neptune_endpoint="neptune.example.com")

    build_graph_client_from_settings(
        settings, driver_factory=fake_driver_factory,
        neptune_client_factory=lambda endpoint, *, port: object(),
    )

    assert driver_factory_called is False


def test_build_graph_client_from_settings_neptune_without_injected_factory_raises_not_implemented():
    # 真实 AWS Neptune 连接尚未实现（见 factory.py::_default_neptune_client_factory
    # 的说明）——这条测试钉住"没有注入 neptune_client_factory 时必须 fail-fast 抛
    # NotImplementedError"这个行为，而不是默默产出一个看似能用、实际连不上 Neptune
    # 的 client。这是本次改动能安全合并（在 Neptune 连通性尚未实测的前提下）的
    # 论据之一，必须有测试钉住，不能只靠代码审查记住这一点。
    settings = build_settings(
        graph_backend="neptune",
        neptune_endpoint="neptune.example.com",
        neptune_port=8182,
    )

    with pytest.raises(NotImplementedError):
        build_graph_client_from_settings(settings)
