from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.config.settings import Settings
from app.graphrag.factory import build_graph_client_from_settings, load_terms_from_settings
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.ingestion.pipeline import ingest_directory
from app.providers.embedding import EmbeddingRegistry
from app.providers.factory import (
    DEFAULT_EMBEDDING_PROVIDER_NAME,
    DEFAULT_LLM_PROVIDER_NAME,
    build_embedding_registry_from_settings,
    build_llm_registry_from_settings,
)
from app.providers.registry import ProviderRegistry
from app.retrieval.factory import build_vector_store_from_settings
from app.retrieval.vector_store import VectorStore


async def main(
    *,
    directory: Path,
    build_graph: bool = False,
    settings: Settings | None = None,
    embedding_registry: EmbeddingRegistry | None = None,
    vector_store: VectorStore | None = None,
    graph_llm_registry: ProviderRegistry | None = None,
    graph_terms: list[Term] | None = None,
    graph_client: Neo4jGraphClient | None = None,
) -> int:
    """批量摄取脚本入口：分块→向量化→写入向量库，可选同步构建知识图谱。

    用法：
      python -m app.ingestion.main --dir path/to/docs
      python -m app.ingestion.main --dir path/to/docs --build-graph
    """
    resolved_settings = settings or Settings()
    registry = embedding_registry or build_embedding_registry_from_settings(
        resolved_settings
    )
    store = vector_store or build_vector_store_from_settings(resolved_settings)

    resolved_graph_llm_registry = None
    resolved_graph_terms = None
    resolved_graph_client = None
    if build_graph:
        resolved_graph_llm_registry = (
            graph_llm_registry
            or build_llm_registry_from_settings(resolved_settings)
        )
        resolved_graph_terms = graph_terms or load_terms_from_settings(
            resolved_settings
        )
        resolved_graph_client = graph_client or build_graph_client_from_settings(
            resolved_settings
        )

    total = await ingest_directory(
        directory,
        embedding_registry=registry,
        embedding_provider_name=DEFAULT_EMBEDDING_PROVIDER_NAME,
        vector_store=store,
        graph_llm_registry=resolved_graph_llm_registry,
        graph_llm_provider_name=DEFAULT_LLM_PROVIDER_NAME if build_graph else None,
        graph_terms=resolved_graph_terms,
        graph_client=resolved_graph_client,
    )
    print(f"已摄取 {total} 个 chunk，来自目录: {directory}")
    return total


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量摄取文档到向量库")
    parser.add_argument("--dir", required=True, type=Path, help="待摄取的文档目录")
    parser.add_argument(
        "--build-graph",
        action="store_true",
        help="同步做 LLM 关系抽取+术语表归一化+写入 Neo4j（需先配置好术语表与 Neo4j）",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(main(directory=args.dir, build_graph=args.build_graph))
