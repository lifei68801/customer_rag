from __future__ import annotations

from pathlib import Path

from app.graphrag.normalization import GraphWriteClientProtocol
from app.graphrag.ontology import Term
from app.ingestion.chunking import Chunk, chunk_markdown
from app.ingestion.graph_extraction import extract_and_write_graph_relations
from app.ingestion.pdf_parser import parse_pdf
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest
from app.providers.registry import ProviderRegistry
from app.retrieval.vector_store import VectorRecord, VectorStore


async def _embed_and_upsert(
    chunks: list[Chunk],
    path: Path,
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
) -> int:
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


async def _maybe_extract_graph_relations(
    chunks: list[Chunk],
    *,
    graph_llm_registry: ProviderRegistry | None,
    graph_llm_provider_name: str | None,
    graph_terms: list[Term] | None,
    graph_client: GraphWriteClientProtocol | None,
) -> None:
    """图谱抽取为可选步骤，四项参数任一缺失则直接跳过，不影响向量化写入路径。"""
    if not (
        graph_llm_registry
        and graph_llm_provider_name
        and graph_terms
        and graph_client is not None
    ):
        return
    await extract_and_write_graph_relations(
        chunks,
        llm_registry=graph_llm_registry,
        llm_provider_name=graph_llm_provider_name,
        terms=graph_terms,
        graph_client=graph_client,
    )


async def ingest_markdown_file(
    path: Path,
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    graph_llm_registry: ProviderRegistry | None = None,
    graph_llm_provider_name: str | None = None,
    graph_terms: list[Term] | None = None,
    graph_client: GraphWriteClientProtocol | None = None,
) -> int:
    """读取单个 Markdown 文件，分块、向量化并写入向量库，返回写入的 chunk 数。

    图谱相关四个参数均为可选：全部提供时额外做 LLM 关系抽取+归一化+
    写入 Neo4j，缺一则跳过，与阶段2的纯向量化摄取行为完全兼容。
    """
    text = path.read_text(encoding="utf-8")
    chunks = chunk_markdown(text, source=str(path))
    count = await _embed_and_upsert(
        chunks,
        path,
        embedding_registry=embedding_registry,
        embedding_provider_name=embedding_provider_name,
        vector_store=vector_store,
    )
    await _maybe_extract_graph_relations(
        chunks,
        graph_llm_registry=graph_llm_registry,
        graph_llm_provider_name=graph_llm_provider_name,
        graph_terms=graph_terms,
        graph_client=graph_client,
    )
    return count


async def ingest_pdf_file(
    path: Path,
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    graph_llm_registry: ProviderRegistry | None = None,
    graph_llm_provider_name: str | None = None,
    graph_terms: list[Term] | None = None,
    graph_client: GraphWriteClientProtocol | None = None,
) -> int:
    """读取单个 PDF 文件（逐页分块），向量化并写入向量库，返回写入的 chunk 数。"""
    chunks = parse_pdf(path)
    count = await _embed_and_upsert(
        chunks,
        path,
        embedding_registry=embedding_registry,
        embedding_provider_name=embedding_provider_name,
        vector_store=vector_store,
    )
    await _maybe_extract_graph_relations(
        chunks,
        graph_llm_registry=graph_llm_registry,
        graph_llm_provider_name=graph_llm_provider_name,
        graph_terms=graph_terms,
        graph_client=graph_client,
    )
    return count


async def ingest_directory(
    directory: Path,
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    graph_llm_registry: ProviderRegistry | None = None,
    graph_llm_provider_name: str | None = None,
    graph_terms: list[Term] | None = None,
    graph_client: GraphWriteClientProtocol | None = None,
) -> int:
    """遍历目录下所有 .md/.pdf 文件并逐个摄取，返回写入的 chunk 总数。"""
    total = 0
    for md_file in sorted(directory.glob("*.md")):
        total += await ingest_markdown_file(
            md_file,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            vector_store=vector_store,
            graph_llm_registry=graph_llm_registry,
            graph_llm_provider_name=graph_llm_provider_name,
            graph_terms=graph_terms,
            graph_client=graph_client,
        )
    for pdf_file in sorted(directory.glob("*.pdf")):
        total += await ingest_pdf_file(
            pdf_file,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            vector_store=vector_store,
            graph_llm_registry=graph_llm_registry,
            graph_llm_provider_name=graph_llm_provider_name,
            graph_terms=graph_terms,
            graph_client=graph_client,
        )
    return total
