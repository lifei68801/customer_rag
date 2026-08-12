from __future__ import annotations

import asyncio
from datetime import datetime

import aiosqlite

from app.graphrag.llm_extractor import extract_candidate_relations
from app.graphrag.normalization import GraphWriteClientProtocol, normalize_and_write_relations
from app.graphrag.ontology import Term
from app.ingestion.chunking import Chunk
from app.providers.registry import ProviderRegistry


def _batch_chunks_by_char_budget(
    chunks: list[Chunk], *, max_chars: int = 3000
) -> list[list[Chunk]]:
    """依次把 chunk 塞进当前批次，累计字符数超过 max_chars 就切下一批；
    单个 chunk 本身已经超过 max_chars 时自己单独成一批，不因为攒批逻辑被
    拆散或跳过。批次之间不重叠——攒批只是为了减少 LLM 调用次数，不改变
    任何一个 chunk 的内容。
    """
    if not chunks:
        return []
    batches: list[list[Chunk]] = []
    current: list[Chunk] = []
    current_len = 0
    for chunk in chunks:
        chunk_len = len(chunk.text)
        if current and current_len + chunk_len > max_chars:
            batches.append(current)
            current = []
            current_len = 0
        current.append(chunk)
        current_len += chunk_len
    if current:
        batches.append(current)
    return batches


async def extract_and_write_graph_relations(
    chunks: list[Chunk],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    terms: list[Term],
    graph_client: GraphWriteClientProtocol,
    source: str,
    tenant_id: str,
    now: datetime,
    review_conn: aiosqlite.Connection | None = None,
    extract_timeout_sec: float = 30.0,
    batch_max_chars: int = 3000,
    max_concurrency: int = 8,
) -> int:
    """摄取时的图谱构建：按字符预算把 chunk 攒批，批次间有限并发地做
    LLM 关系抽取，再顺序做术语表归一化 + 写入 Neo4j。

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

    攒批+并发是效率改造的核心：关系写入只按 source+tenant_id 溯源，不
    依赖 chunk 粒度，合并多个 chunk 进一次 LLM 调用是纯效率提升；
    max_concurrency 用 Semaphore 限制同时在途的批次数，避免大文档一次性
    发出几十个并发请求触发 LLM 供应商限流。并发只作用于 LLM 抽取这一步
    （无共享可变状态，天然安全）；写入 Neo4j/审核队列的步骤保持顺序执行，
    避免"多协程并发操作同一个 aiosqlite 连接是否安全"这个没有把握的
    未知数。已知代价：一批失败会丢整批涉及 chunk 的关系（现状是一个
    chunk 失败只丢一个 chunk），接受这个代价换取更少的调用次数。
    """
    await graph_client.delete_relations_by_source(source, tenant_id=tenant_id)
    batches = _batch_chunks_by_char_budget(chunks, max_chars=batch_max_chars)
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _process_batch(batch: list[Chunk]) -> list[dict[str, str]]:
        async with semaphore:
            return await extract_candidate_relations(
                [chunk.text for chunk in batch],
                llm_registry=llm_registry,
                llm_provider_name=llm_provider_name,
                timeout_sec=extract_timeout_sec,
            )

    all_relation_lists = await asyncio.gather(
        *(_process_batch(batch) for batch in batches)
    )

    total_written = 0
    for relations in all_relation_lists:
        total_written += await normalize_and_write_relations(
            relations,
            terms=terms,
            graph_client=graph_client,
            source=source,
            tenant_id=tenant_id,
            now=now,
            review_conn=review_conn,
        )
    return total_written
