from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

from app.graphrag.normalization import GraphWriteClientProtocol
from app.graphrag.ontology import Term
from app.ingestion.docx_parser import parse_docx
from app.ingestion.ocr_parser import OcrFunction, parse_image
from app.ingestion.pdf_parser import parse_pdf
from app.ingestion.pipeline import _ingest_chunks
from app.ingestion.chunking import chunk_markdown
from app.ingestion.ticket_parser import parse_ticket_csv
from app.ingestion.tracking import record_ingested, remove_tracked_file
from app.providers.embedding import EmbeddingRegistry
from app.providers.registry import ProviderRegistry
from app.retrieval.vector_store import VectorStore

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    job_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    content_hash TEXT,
    action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_status ON ingestion_jobs (status);
"""

_PARSERS = {
    ".md": lambda path: chunk_markdown(path.read_text(encoding="utf-8"), source=str(path)),
    ".docx": parse_docx,
    ".csv": parse_ticket_csv,
}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


async def ensure_ingestion_queue_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


def _compute_dedupe_key(
    *, tenant_id: str, file_path: str, content_hash: str, action: str
) -> str:
    raw = f"{tenant_id}:{file_path}:{content_hash}:{action}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def enqueue_ingestion_job(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    file_path: str,
    content_hash: str,
    action: str,
) -> str:
    """入队一个摄取任务（action 为 'ingest' 或 'delete'），幂等：同一个
    (tenant_id, file_path, content_hash, action) 组合重复入队只创建一条记录。
    """
    dedupe_key = _compute_dedupe_key(
        tenant_id=tenant_id, file_path=file_path, content_hash=content_hash, action=action
    )
    job_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO ingestion_jobs "
        "(job_id, dedupe_key, tenant_id, file_path, content_hash, action) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(dedupe_key) DO NOTHING",
        (job_id, dedupe_key, tenant_id, file_path, content_hash, action),
    )
    await conn.commit()
    cursor = await conn.execute(
        "SELECT job_id FROM ingestion_jobs WHERE dedupe_key = ?", (dedupe_key,)
    )
    row = await cursor.fetchone()
    return row[0]


async def list_pending_jobs(
    conn: aiosqlite.Connection, *, limit: int = 10
) -> list[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM ingestion_jobs WHERE status = 'pending' ORDER BY created_at LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def mark_job_completed(conn: aiosqlite.Connection, job_id: str) -> None:
    await conn.execute(
        "UPDATE ingestion_jobs SET status='completed', updated_at=datetime('now') "
        "WHERE job_id=?",
        (job_id,),
    )
    await conn.commit()


async def mark_job_failed(
    conn: aiosqlite.Connection, job_id: str, *, error: str, max_attempts: int = 3
) -> None:
    cursor = await conn.execute(
        "SELECT attempts FROM ingestion_jobs WHERE job_id = ?", (job_id,)
    )
    row = await cursor.fetchone()
    attempts = (row[0] if row else 0) + 1
    status = "dead" if attempts >= max_attempts else "pending"
    await conn.execute(
        "UPDATE ingestion_jobs SET status=?, attempts=?, last_error=?, "
        "updated_at=datetime('now') WHERE job_id=?",
        (status, attempts, error, job_id),
    )
    await conn.commit()


def _parse_file(path: Path, *, ocr: OcrFunction | None):
    suffix = path.suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return parse_image(path, ocr=ocr)
    if suffix == ".pdf":
        return parse_pdf(path, ocr=ocr)
    parser = _PARSERS.get(suffix)
    if parser is None:
        raise ValueError(f"不支持的文件类型: {suffix}")
    return parser(path)


async def process_pending_jobs(
    conn: aiosqlite.Connection,
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    graph_llm_registry: ProviderRegistry | None = None,
    graph_llm_provider_name: str | None = None,
    graph_terms: list[Term] | None = None,
    graph_client: GraphWriteClientProtocol | None = None,
    graph_review_conn: aiosqlite.Connection | None = None,
    ocr: OcrFunction | None = None,
    limit: int = 10,
    max_attempts: int = 3,
) -> int:
    """扫描并处理一批待处理摄取任务，返回成功完成的数量。

    每个 'ingest' 任务都先删掉该文件旧版本写入过的全部 chunk 再重新写入
    ——对全新文件这是无害的空操作（没有旧 chunk 可删），对变更过的文件
    这避免了新版本 chunk 数变少时残留旧 chunk。'delete' 任务只删 chunk
    + 清理 tracking 记录，不重新解析文件（通常此时文件已经不存在了）。

    每个任务的失败都单独捕获、单独判定重试/死信，不会因为一条任务出错
    影响同批次其它任务。
    """
    jobs = await list_pending_jobs(conn, limit=limit)
    processed = 0
    for job in jobs:
        tenant_id = job["tenant_id"]
        file_path = job["file_path"]
        try:
            await vector_store.delete_by_source(source=file_path, tenant_id=tenant_id)
            if job["action"] == "delete":
                await remove_tracked_file(
                    conn, tenant_id=tenant_id, file_path=file_path
                )
            else:
                chunks = _parse_file(Path(file_path), ocr=ocr)
                chunk_count = await _ingest_chunks(
                    chunks,
                    Path(file_path),
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
                await record_ingested(
                    conn,
                    tenant_id=tenant_id,
                    file_path=file_path,
                    content_hash=job["content_hash"],
                    chunk_count=chunk_count,
                )
        except Exception as exc:  # noqa: BLE001 - 任何异常都要落到重试/死信逻辑
            await mark_job_failed(
                conn, job["job_id"], error=str(exc), max_attempts=max_attempts
            )
            continue
        await mark_job_completed(conn, job["job_id"])
        processed += 1
    return processed
