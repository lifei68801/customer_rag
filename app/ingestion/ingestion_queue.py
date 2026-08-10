from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

from app.db_migrations import add_column_if_missing
from app.graphrag.normalization import GraphWriteClientProtocol
from app.graphrag.ontology import Term
from app.ingestion.docx_parser import parse_docx
from app.ingestion.ocr_parser import OcrFunction, parse_image
from app.ingestion.pdf_parser import (
    _DEFAULT_OCR_MAX_CONCURRENCY,
    _DEFAULT_OCR_RENDER_DPI,
    parse_pdf,
)
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

# _parse_file() 能处理的全部扩展名，作为唯一权威来源导出，供上传接口在
# 落盘/入队之前做同步校验——否则不受支持的文件会先落盘、再在后台任务里
# 重试三次后进死信队列，用户只能从"处理中任务"的报错里发现问题。
SUPPORTED_SUFFIXES = frozenset(_PARSERS) | _IMAGE_SUFFIXES | {".pdf"}


async def ensure_ingestion_queue_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
    # build_graph 支持逐任务决定是否触发图谱构建，历史任务默认 0（不建图，
    # 与迁移前的行为一致——迁移前所有任务的图谱资源都是调用方整批统一决定的）。
    await add_column_if_missing(
        conn, table="ingestion_jobs", column="build_graph",
        ddl="INTEGER NOT NULL DEFAULT 0",
    )


def _compute_dedupe_key(
    *, tenant_id: str, file_path: str, content_hash: str, action: str, build_graph: bool
) -> str:
    raw = f"{tenant_id}:{file_path}:{content_hash}:{action}:{build_graph}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def enqueue_ingestion_job(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    file_path: str,
    content_hash: str,
    action: str,
    build_graph: bool = False,
) -> str:
    """入队一个摄取任务（action 为 'ingest' 或 'delete'），幂等：同一个
    (tenant_id, file_path, content_hash, action, build_graph) 组合重复
    入队只创建一条记录。
    """
    dedupe_key = _compute_dedupe_key(
        tenant_id=tenant_id, file_path=file_path, content_hash=content_hash,
        action=action, build_graph=build_graph,
    )
    job_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO ingestion_jobs "
        "(job_id, dedupe_key, tenant_id, file_path, content_hash, action, build_graph) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(dedupe_key) DO NOTHING",
        (job_id, dedupe_key, tenant_id, file_path, content_hash, action, int(build_graph)),
    )
    await conn.commit()
    cursor = await conn.execute(
        "SELECT job_id FROM ingestion_jobs WHERE dedupe_key = ?", (dedupe_key,)
    )
    row = await cursor.fetchone()
    return row[0]


async def list_pending_jobs(
    conn: aiosqlite.Connection, *, limit: int = 10, tenant_id: str | None = None
) -> list[dict[str, Any]]:
    """列出待处理任务，`tenant_id=None` 时不区分租户（`process_pending_jobs`
    批处理场景就是要跨租户轮询）；传了就在 SQL 里过滤，而不是查出来在
    Python 里筛——否则租户一多，某个租户的任务会被别的租户的任务挤出
    `limit` 之外，管理页面就看不到自己的处理中任务了。
    """
    conn.row_factory = aiosqlite.Row
    if tenant_id is None:
        cursor = await conn.execute(
            "SELECT * FROM ingestion_jobs WHERE status = 'pending' "
            "ORDER BY created_at LIMIT ?",
            (limit,),
        )
    else:
        cursor = await conn.execute(
            "SELECT * FROM ingestion_jobs WHERE status = 'pending' AND tenant_id = ? "
            "ORDER BY created_at LIMIT ?",
            (tenant_id, limit),
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


def _parse_file(
    path: Path,
    *,
    ocr: OcrFunction | None,
    ocr_render_dpi: int,
    ocr_max_concurrency: int,
):
    suffix = path.suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return parse_image(path, ocr=ocr)
    if suffix == ".pdf":
        return parse_pdf(
            path,
            ocr=ocr,
            render_dpi=ocr_render_dpi,
            max_concurrency=ocr_max_concurrency,
        )
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
    ocr_render_dpi: int = _DEFAULT_OCR_RENDER_DPI,
    ocr_max_concurrency: int = _DEFAULT_OCR_MAX_CONCURRENCY,
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
                # _parse_file 是同步阻塞调用（表格版面分析是 CPU 密集型，
                # OCR 兜底路径还会用同步 httpx.Client 逐页发请求，扫描件
                # 大文件能跑几十分钟）。process_pending_jobs() 本身跑在
                # FastAPI 后台任务的同一个事件循环里，直接同步调用会把
                # 循环独占整个处理时长，导致这期间所有其它请求（包括别的
                # 租户的 /qa、/agent/chat）全部无响应。用 asyncio.to_thread
                # 丢到线程池执行，和 milvus_store.py 里对同步 pymilvus
                # 调用的处理方式保持一致。
                chunks = await asyncio.to_thread(
                    _parse_file,
                    Path(file_path),
                    ocr=ocr,
                    ocr_render_dpi=ocr_render_dpi,
                    ocr_max_concurrency=ocr_max_concurrency,
                )
                use_graph = bool(job["build_graph"])
                chunk_count = await _ingest_chunks(
                    chunks,
                    Path(file_path),
                    embedding_registry=embedding_registry,
                    embedding_provider_name=embedding_provider_name,
                    vector_store=vector_store,
                    tenant_id=tenant_id,
                    graph_llm_registry=graph_llm_registry if use_graph else None,
                    graph_llm_provider_name=graph_llm_provider_name if use_graph else None,
                    graph_terms=graph_terms if use_graph else None,
                    graph_client=graph_client if use_graph else None,
                    graph_review_conn=graph_review_conn if use_graph else None,
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
