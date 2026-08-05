from __future__ import annotations

from pathlib import Path

from app.ingestion.chunking import chunk_markdown
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest
from app.retrieval.vector_store import VectorRecord, VectorStore


async def ingest_markdown_file(
    path: Path,
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
) -> int:
    """读取单个 Markdown 文件，分块、向量化并写入向量库，返回写入的 chunk 数。"""
    text = path.read_text(encoding="utf-8")
    chunks = chunk_markdown(text, source=str(path))
    if not chunks:
        return 0

    embed_result = await embedding_registry.run(
        EmbeddingRequest(texts=[chunk.text for chunk in chunks]),
        provider_name=embedding_provider_name,
    )

    records = [
        VectorRecord(
            id=f"{path}#{i}",
            vector=vector,
            text=chunk.text,
            metadata={
                "source": chunk.source,
                "heading_path": "/".join(chunk.heading_path),
            },
        )
        for i, (chunk, vector) in enumerate(zip(chunks, embed_result.vectors))
    ]
    await vector_store.upsert(records)
    return len(records)


async def ingest_directory(
    directory: Path,
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
) -> int:
    """遍历目录下所有 .md 文件并逐个摄取，返回写入的 chunk 总数。"""
    total = 0
    for md_file in sorted(directory.glob("*.md")):
        total += await ingest_markdown_file(
            md_file,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            vector_store=vector_store,
        )
    return total
