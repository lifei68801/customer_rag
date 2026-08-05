from __future__ import annotations

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
    extract_timeout_sec: float = 2.0,
) -> int:
    """摄取时的图谱构建：逐 chunk 做 LLM 关系抽取 + 术语表归一化 + 写入 Neo4j。

    这是可选步骤（未接入 ingest_markdown_file/ingest_pdf_file 的默认路径），
    调用方需要显式提供 llm_registry/terms/graph_client 才会执行；不提供
    则摄取流程只做向量化写入，与阶段2的行为保持完全兼容。
    """
    total_written = 0
    for chunk in chunks:
        relations = await extract_candidate_relations(
            chunk.text,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            timeout_sec=extract_timeout_sec,
        )
        total_written += await normalize_and_write_relations(
            relations, terms=terms, graph_client=graph_client
        )
    return total_written
