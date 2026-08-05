from __future__ import annotations

from pathlib import Path
from typing import Callable

from app.config.settings import Settings
from app.graphrag.neo4j_client import Neo4jDriverProtocol, Neo4jGraphClient
from app.graphrag.ontology import Term, load_terminology


def _default_driver_factory(uri: str, *, auth: tuple[str, str]) -> Neo4jDriverProtocol:
    from neo4j import AsyncGraphDatabase

    return AsyncGraphDatabase.driver(uri, auth=auth)


def build_graph_client_from_settings(
    settings: Settings,
    *,
    driver_factory: Callable[..., Neo4jDriverProtocol] | None = None,
) -> Neo4jGraphClient:
    factory = driver_factory or _default_driver_factory
    driver = factory(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    return Neo4jGraphClient(driver=driver)


def load_terms_from_settings(settings: Settings) -> list[Term]:
    return load_terminology(Path(settings.terminology_path))
