from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.config.settings import Settings
from app.ingestion.pipeline import ingest_directory
from app.providers.embedding import EmbeddingRegistry
from app.providers.factory import (
    DEFAULT_EMBEDDING_PROVIDER_NAME,
    build_embedding_registry_from_settings,
)
from app.retrieval.factory import build_vector_store_from_settings
from app.retrieval.vector_store import VectorStore


async def main(
    *,
    directory: Path,
    settings: Settings | None = None,
    embedding_registry: EmbeddingRegistry | None = None,
    vector_store: VectorStore | None = None,
) -> int:
    """批量摄取脚本入口：分块→向量化→写入向量库。

    用法：python -m app.ingestion.main --dir path/to/markdown_docs
    """
    resolved_settings = settings or Settings()
    registry = embedding_registry or build_embedding_registry_from_settings(
        resolved_settings
    )
    store = vector_store or build_vector_store_from_settings(resolved_settings)

    total = await ingest_directory(
        directory,
        embedding_registry=registry,
        embedding_provider_name=DEFAULT_EMBEDDING_PROVIDER_NAME,
        vector_store=store,
    )
    print(f"已摄取 {total} 个 chunk，来自目录: {directory}")
    return total


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量摄取 Markdown 文档到向量库")
    parser.add_argument(
        "--dir", required=True, type=Path, help="待摄取的 Markdown 文档目录"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(main(directory=args.dir))
