from __future__ import annotations

from pathlib import Path

import aiosqlite

from app.graphrag.normalization import GraphWriteClientProtocol
from app.graphrag.ontology import Term
from app.ingestion.chunking import Chunk, chunk_markdown
from app.ingestion.docx_parser import parse_docx
from app.ingestion.graph_extraction import extract_and_write_graph_relations
from app.ingestion.ocr_parser import OcrFunction, parse_image
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
    tenant_id: str,
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
            tenant_id=tenant_id,
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
    source: str,
    graph_llm_registry: ProviderRegistry | None,
    graph_llm_provider_name: str | None,
    graph_terms: list[Term] | None,
    graph_client: GraphWriteClientProtocol | None,
    graph_review_conn: aiosqlite.Connection | None,
) -> None:
    """图谱抽取为可选步骤，四项必需参数任一缺失则直接跳过，不影响向量化写入路径。

    graph_review_conn 独立于这四项之外是可选项：未能对齐术语表的候选
    关系会转入人工待审核队列而非直接丢弃（见 normalize_and_write_relations）。
    """
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
        source=source,
        review_conn=graph_review_conn,
    )


async def _ingest_chunks(
    chunks: list[Chunk],
    path: Path,
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    tenant_id: str,
    graph_llm_registry: ProviderRegistry | None,
    graph_llm_provider_name: str | None,
    graph_terms: list[Term] | None,
    graph_client: GraphWriteClientProtocol | None,
    graph_review_conn: aiosqlite.Connection | None,
) -> int:
    """已解析出 chunk 之后共用的写入逻辑：向量化+入库，可选做图谱抽取。

    各文件格式的 ingest_*_file 只负责"怎么把文件解析成 chunk 列表"这一步
    不同，解析完之后的处理管线完全一致，抽出来避免四份文件格式各写一遍
    近乎相同的代码。
    """
    count = await _embed_and_upsert(
        chunks,
        path,
        embedding_registry=embedding_registry,
        embedding_provider_name=embedding_provider_name,
        vector_store=vector_store,
        tenant_id=tenant_id,
    )
    await _maybe_extract_graph_relations(
        chunks,
        source=str(path),
        graph_llm_registry=graph_llm_registry,
        graph_llm_provider_name=graph_llm_provider_name,
        graph_terms=graph_terms,
        graph_client=graph_client,
        graph_review_conn=graph_review_conn,
    )
    return count


async def ingest_markdown_file(
    path: Path,
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    tenant_id: str,
    graph_llm_registry: ProviderRegistry | None = None,
    graph_llm_provider_name: str | None = None,
    graph_terms: list[Term] | None = None,
    graph_client: GraphWriteClientProtocol | None = None,
    graph_review_conn: aiosqlite.Connection | None = None,
) -> int:
    """读取单个 Markdown 文件，分块、向量化并写入向量库，返回写入的 chunk 数。

    tenant_id 是必填项：每条写入的 VectorRecord 都会打上这个租户标签，
    决定了后续检索时哪些租户能看到这批数据。

    图谱相关四个参数均为可选：全部提供时额外做 LLM 关系抽取+归一化+
    写入 Neo4j，缺一则跳过，与阶段2的纯向量化摄取行为完全兼容。
    graph_review_conn 额外可选：提供时未对齐术语表的候选进人工待审核队列。
    """
    text = path.read_text(encoding="utf-8")
    chunks = chunk_markdown(text, source=str(path))
    return await _ingest_chunks(
        chunks,
        path,
        embedding_registry=embedding_registry,
        embedding_provider_name=embedding_provider_name,
        vector_store=vector_store,
        tenant_id=tenant_id,
        graph_llm_registry=graph_llm_registry,
        graph_llm_provider_name=graph_llm_provider_name,
        graph_terms=graph_terms,
        graph_client=graph_client,
        graph_review_conn=graph_review_conn,
    )


async def ingest_pdf_file(
    path: Path,
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    tenant_id: str,
    graph_llm_registry: ProviderRegistry | None = None,
    graph_llm_provider_name: str | None = None,
    graph_terms: list[Term] | None = None,
    graph_client: GraphWriteClientProtocol | None = None,
    graph_review_conn: aiosqlite.Connection | None = None,
    ocr: OcrFunction | None = None,
) -> int:
    """读取单个 PDF 文件（逐页分块），向量化并写入向量库，返回写入的 chunk 数。

    ocr 可选：提供时，提取不到文字层的扫描件页面会渲染成图片走 OCR
    （见 pdf_parser.py），不提供则保持"跳过无文字层页面"的原有行为。
    """
    chunks = parse_pdf(path, ocr=ocr)
    return await _ingest_chunks(
        chunks,
        path,
        embedding_registry=embedding_registry,
        embedding_provider_name=embedding_provider_name,
        vector_store=vector_store,
        tenant_id=tenant_id,
        graph_llm_registry=graph_llm_registry,
        graph_llm_provider_name=graph_llm_provider_name,
        graph_terms=graph_terms,
        graph_client=graph_client,
        graph_review_conn=graph_review_conn,
    )


async def ingest_docx_file(
    path: Path,
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    tenant_id: str,
    graph_llm_registry: ProviderRegistry | None = None,
    graph_llm_provider_name: str | None = None,
    graph_terms: list[Term] | None = None,
    graph_client: GraphWriteClientProtocol | None = None,
    graph_review_conn: aiosqlite.Connection | None = None,
) -> int:
    """读取单个 Word 文件（按一级标题分块），向量化并写入向量库，返回写入的 chunk 数。"""
    chunks = parse_docx(path)
    return await _ingest_chunks(
        chunks,
        path,
        embedding_registry=embedding_registry,
        embedding_provider_name=embedding_provider_name,
        vector_store=vector_store,
        tenant_id=tenant_id,
        graph_llm_registry=graph_llm_registry,
        graph_llm_provider_name=graph_llm_provider_name,
        graph_terms=graph_terms,
        graph_client=graph_client,
        graph_review_conn=graph_review_conn,
    )


async def ingest_image_file(
    path: Path,
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    tenant_id: str,
    graph_llm_registry: ProviderRegistry | None = None,
    graph_llm_provider_name: str | None = None,
    graph_terms: list[Term] | None = None,
    graph_client: GraphWriteClientProtocol | None = None,
    graph_review_conn: aiosqlite.Connection | None = None,
    ocr: OcrFunction | None = None,
) -> int:
    """OCR 提取单张图片（扫描件/照片）里的文字，向量化并写入向量库。

    ocr 可注入替换默认的 pytesseract 实现，测试/自定义 OCR 引擎时用；
    默认实现需要本机安装 Tesseract 二进制（见 ocr_parser.py 的说明）。
    """
    chunks = parse_image(path, ocr=ocr)
    return await _ingest_chunks(
        chunks,
        path,
        embedding_registry=embedding_registry,
        embedding_provider_name=embedding_provider_name,
        vector_store=vector_store,
        tenant_id=tenant_id,
        graph_llm_registry=graph_llm_registry,
        graph_llm_provider_name=graph_llm_provider_name,
        graph_terms=graph_terms,
        graph_client=graph_client,
        graph_review_conn=graph_review_conn,
    )


async def ingest_directory(
    directory: Path,
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    tenant_id: str,
    graph_llm_registry: ProviderRegistry | None = None,
    graph_llm_provider_name: str | None = None,
    graph_terms: list[Term] | None = None,
    graph_client: GraphWriteClientProtocol | None = None,
    graph_review_conn: aiosqlite.Connection | None = None,
    ocr: OcrFunction | None = None,
) -> int:
    """遍历目录下所有 .md/.pdf/.docx/图片文件并逐个摄取，返回写入的 chunk 总数。

    图片格式覆盖 .png/.jpg/.jpeg；PDF 里提取不到文字层的扫描件页面同样
    走 ocr 参数指定的 OCR（默认走 ocr_parser.py 的 pytesseract 实现，
    需要本机装好 Tesseract；页面渲染用 PyMuPDF，不需要额外的 poppler
    系统依赖，见 pdf_parser.py）。不提供 ocr 时两者都保持"跳过没有文字
    的页面/图片"的原有行为。

    一次调用只摄取给一个租户；不同租户的文档要分开跑摄取脚本。
    """
    total = 0
    for md_file in sorted(directory.glob("*.md")):
        total += await ingest_markdown_file(
            md_file,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            vector_store=vector_store,
            tenant_id=tenant_id,
            graph_llm_registry=graph_llm_registry,
            graph_llm_provider_name=graph_llm_provider_name,
            graph_terms=graph_terms,
            graph_client=graph_client,
            graph_review_conn=graph_review_conn,
        )
    for pdf_file in sorted(directory.glob("*.pdf")):
        total += await ingest_pdf_file(
            pdf_file,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            vector_store=vector_store,
            tenant_id=tenant_id,
            graph_llm_registry=graph_llm_registry,
            graph_llm_provider_name=graph_llm_provider_name,
            graph_terms=graph_terms,
            graph_client=graph_client,
            graph_review_conn=graph_review_conn,
            ocr=ocr,
        )
    for docx_file in sorted(directory.glob("*.docx")):
        total += await ingest_docx_file(
            docx_file,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            vector_store=vector_store,
            tenant_id=tenant_id,
            graph_llm_registry=graph_llm_registry,
            graph_llm_provider_name=graph_llm_provider_name,
            graph_terms=graph_terms,
            graph_client=graph_client,
            graph_review_conn=graph_review_conn,
        )
    image_files = [
        f
        for pattern in ("*.png", "*.jpg", "*.jpeg")
        for f in directory.glob(pattern)
    ]
    for image_file in sorted(image_files):
        total += await ingest_image_file(
            image_file,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            vector_store=vector_store,
            tenant_id=tenant_id,
            graph_llm_registry=graph_llm_registry,
            graph_llm_provider_name=graph_llm_provider_name,
            graph_terms=graph_terms,
            graph_client=graph_client,
            graph_review_conn=graph_review_conn,
            ocr=ocr,
        )
    return total
