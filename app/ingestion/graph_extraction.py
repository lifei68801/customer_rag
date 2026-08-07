from __future__ import annotations

import aiosqlite

from app.graphrag.llm_extractor import extract_candidate_relations
from app.graphrag.normalization import GraphWriteClientProtocol, normalize_and_write_relations
from app.graphrag.ontology import Term
from app.ingestion.chunking import Chunk
from app.providers.registry import ProviderRegistry


async def extract_and_write_graph_relations(
    chunks: list[Chunk],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    terms: list[Term],
    graph_client: GraphWriteClientProtocol,
    source: str,
    tenant_id: str,
    review_conn: aiosqlite.Connection | None = None,
    extract_timeout_sec: float = 2.0,
) -> int:
    """摄取时的图谱构建：逐 chunk 做 LLM 关系抽取 + 术语表归一化 + 写入 Neo4j。

    这是可选步骤（未接入 ingest_markdown_file/ingest_pdf_file 的默认路径），
    调用方需要显式提供 llm_registry/terms/graph_client 才会执行；不提供
    则摄取流程只做向量化写入，与阶段2的行为保持完全兼容。

    写入前先删掉 source+tenant_id 这个文档、这个租户之前写过的全部关系边
    （delete_relations_by_source），再重新抽取写入——和
    vector_store.delete_by_source() 同样的道理：文档内容变更后，旧版本
    抽取出的关系不会永久残留在图谱里。对全新文档这是无害的空操作。
    tenant_id 同时保证了这个清理动作不会波及其它租户摄取过的同名文档。

    review_conn 同样可选：提供时，未能对齐术语表的候选关系会进入人工
    待审核队列而不是直接丢弃（见 normalize_and_write_relations）。
    """
    await graph_client.delete_relations_by_source(source, tenant_id=tenant_id)
    total_written = 0
    for chunk in chunks:
        relations = await extract_candidate_relations(
            chunk.text,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            timeout_sec=extract_timeout_sec,
        )
        total_written += await normalize_and_write_relations(
            relations,
            terms=terms,
            graph_client=graph_client,
            source=source,
            tenant_id=tenant_id,
            review_conn=review_conn,
        )
    return total_written
