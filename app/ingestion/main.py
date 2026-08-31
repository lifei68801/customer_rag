from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import aiosqlite

from app.config.settings import Settings
from app.graphrag.factory import build_graph_client_from_settings
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.ontology_store import open_ontology_store_conn
from app.graphrag.terms_store import list_terms_merged
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
    tenant_id: str,
    build_graph: bool = False,
    settings: Settings | None = None,
    embedding_registry: EmbeddingRegistry | None = None,
    vector_store: VectorStore | None = None,
    graph_llm_registry: ProviderRegistry | None = None,
    graph_terms: list[Term] | None = None,
    graph_client: Neo4jGraphClient | None = None,
    graph_review_conn: aiosqlite.Connection | None = None,
) -> int:
    """批量摄取脚本入口：分块→向量化→写入向量库，可选同步构建知识图谱。

    一次调用只摄取给一个租户（--tenant-id），不同租户的文档要分开跑。

    --build-graph 时会自动启用人工待审核队列（graph_review_conn），
    未能对齐术语表的候选关系进队列而非直接丢弃；可通过
    `python -m app.graphrag.review_cli` 查看/批准/驳回。

    用法：
      python -m app.ingestion.main --dir path/to/docs --tenant-id t1
      python -m app.ingestion.main --dir path/to/docs --tenant-id t1 --build-graph
    """
    resolved_settings = settings or Settings()
    registry = embedding_registry or build_embedding_registry_from_settings(
        resolved_settings
    )
    store = vector_store or build_vector_store_from_settings(resolved_settings)

    resolved_graph_llm_registry = None
    resolved_graph_terms = None
    resolved_graph_client = None
    resolved_graph_review_conn = None
    if build_graph:
        resolved_graph_llm_registry = (
            graph_llm_registry
            or build_llm_registry_from_settings(resolved_settings)
        )
        if graph_client is not None:
            resolved_graph_client = graph_client
        else:
            resolved_graph_client = build_graph_client_from_settings(resolved_settings)
            await resolved_graph_client.ensure_tenant_scoped_schema()
        resolved_graph_review_conn = (
            graph_review_conn
            or await open_ontology_store_conn(resolved_settings)
        )
        resolved_graph_terms = graph_terms or await list_terms_merged(
            resolved_graph_review_conn, tenant_id
        )
        # 术语表（基准真相）先同步进图谱：写入/更新标准节点的 type
        # 属性 + 别名节点，再进入下面的文档摄取+关系抽取——保证图谱里不只有
        # LLM 抽取出的关系边，也有完整的实体+别名+分类信息（架构文档 §4.1）。
        await resolved_graph_client.sync_terms(resolved_graph_terms)

    total = await ingest_directory(
        directory,
        embedding_registry=registry,
        embedding_provider_name=DEFAULT_EMBEDDING_PROVIDER_NAME,
        vector_store=store,
        tenant_id=tenant_id,
        graph_llm_registry=resolved_graph_llm_registry,
        graph_llm_provider_name=DEFAULT_LLM_PROVIDER_NAME if build_graph else None,
        graph_terms=resolved_graph_terms,
        graph_client=resolved_graph_client,
        graph_review_conn=resolved_graph_review_conn,
    )
    print(f"已摄取 {total} 个 chunk，来自目录: {directory}（租户: {tenant_id}）")
    return total


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量摄取文档到向量库")
    parser.add_argument("--dir", required=True, type=Path, help="待摄取的文档目录")
    parser.add_argument("--tenant-id", required=True, help="本次摄取归属的租户/产品线标识")
    parser.add_argument(
        "--build-graph",
        action="store_true",
        help="同步做 LLM 关系抽取+术语表归一化+写入 Neo4j（需先配置好术语表与 Neo4j）",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(
        main(directory=args.dir, tenant_id=args.tenant_id, build_graph=args.build_graph)
    )
