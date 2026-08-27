from __future__ import annotations

from pathlib import Path
from typing import Callable

from app.config.settings import Settings
from app.graphrag.neo4j_client import Neo4jDriverProtocol, Neo4jGraphClient
from app.graphrag.neptune_client import NeptuneClientProtocol, NeptuneGraphClient
from app.graphrag.ontology import Term, load_terminology


def _default_driver_factory(uri: str, *, auth: tuple[str, str]) -> Neo4jDriverProtocol:
    from neo4j import AsyncGraphDatabase

    return AsyncGraphDatabase.driver(uri, auth=auth)


def _default_neptune_client_factory(endpoint: str, *, port: int) -> NeptuneClientProtocol:
    raise NotImplementedError(
        "真实 AWS Neptune 连接尚未实现——这个仓库目前没有 boto3/AWS 认证签名依赖。"
        "接入真实 Neptune 环境时需要实现一个满足 NeptuneClientProtocol 的具体 "
        "client（openCypher HTTPS 端点 + AWS 请求签名），并通过 "
        "build_graph_client_from_settings(settings, neptune_client_factory=...) "
        "注入，而不是依赖这个默认工厂。"
    )


def build_graph_client_from_settings(
    settings: Settings,
    *,
    driver_factory: Callable[..., Neo4jDriverProtocol] | None = None,
    neptune_client_factory: Callable[..., NeptuneClientProtocol] | None = None,
) -> Neo4jGraphClient | NeptuneGraphClient:
    if settings.graph_backend == "neptune":
        factory = neptune_client_factory or _default_neptune_client_factory
        client = factory(settings.neptune.endpoint, port=settings.neptune.port)
        return NeptuneGraphClient(client=client)
    factory = driver_factory or _default_driver_factory
    driver = factory(
        settings.neo4j.uri, auth=(settings.neo4j.user, settings.neo4j.password)
    )
    return Neo4jGraphClient(driver=driver)


def load_terms_from_settings(settings: Settings) -> list[Term]:
    return load_terminology(Path(settings.terminology_path))
