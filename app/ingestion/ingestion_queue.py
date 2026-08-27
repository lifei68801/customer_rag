from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
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
    _DEFAULT_TABLE_EXTRACTION_MAX_CONCURRENCY,
    parse_pdf,
)
from app.ingestion.pipeline import _ingest_chunks
from app.ingestion.table_extraction import TableExtractionFunction
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


class JobNotFoundError(Exception):
    """指定的 job_id 在该租户下不存在（包括存在于别的租户名下的情况——
    不做区分，统一按"不存在"处理，避免向调用方泄露"这个任务属于别的
    租户"这个信息）。"""


class JobNotDeadError(Exception):
    """指定的 job_id 存在，但既不是 dead 状态，也不是"疑似卡死"的
    pending 状态（见 _is_stuck_pending）——重试/删除只对"确认不会再有
    活跃 worker 处理它"的任务开放，不该误伤正在正常排队/处理中的任务。"""


_STUCK_AFTER_MINUTES = 30
"""pending 任务的 updated_at 到现在超过这个阈值，判定为"疑似卡死"。30
分钟远超过任意单次摄取（含 OCR、表格抽取）的合理耗时，避免把"正常处理
中，只是这份文档比较大"误判为卡死。"""


def _is_stuck_pending(job: dict[str, Any]) -> bool:
    """判定见 _STUCK_AFTER_MINUTES 的说明。以 updated_at（而不是只看
    started_at）作判据——本系统没有任何周期性调度器，process_pending_jobs()
    只在"新上传/手动重试"这类外部动作触发时才会跑一次，所以"入队后从未被
    worker 取用过"（started_at 为空）不是罕见的竞态，而是常见的终态：
    队列较长、或者没有别的上传/重试再来触发一次批处理时，这条任务会一直
    停在 pending，永远等不到下一次处理。只看 started_at 会漏掉这整类任务
    （实测：线上确实存在 created_at 是几天前、started_at 仍为空的 pending
    任务）。updated_at 在入队、开始处理（mark_job_started）、每次失败重试
    （mark_job_failed）时都会刷新，统一覆盖"从未被取用"和"取用后处理中途
    异常终止"两种情况——只要最近 _STUCK_AFTER_MINUTES 分钟内这条记录完全
    没被碰过，就判定为疑似卡死。
    """
    if job["status"] != "pending":
        return False
    updated_at = datetime.strptime(job["updated_at"], "%Y-%m-%d %H:%M:%S")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now - updated_at > timedelta(minutes=_STUCK_AFTER_MINUTES)


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
    # started_at 记录一条任务"实际开始被 worker 处理"的时间，跟 created_at
    # （入队时间）是两个不同的概念——用于判定卡死任务，见 _is_stuck_pending。
    # 历史任务默认 NULL：既不会被误判为"刚开始处理"，也不会被误判为卡死
    # （_is_stuck_pending 对 started_at 为空的任务直接返回 False）。
    await add_column_if_missing(
        conn, table="ingestion_jobs", column="started_at", ddl="TEXT",
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
    jobs = [dict(row) for row in rows]
    # is_stuck 是派生字段，不落库——每次查询按当前时间现算，供管理后台
    # 决定是否对这一行展示"重新执行/删除"（正常排队/处理中的任务不展示，
    # 避免误伤，见 _is_stuck_pending 的说明）。
    for job in jobs:
        job["is_stuck"] = _is_stuck_pending(job)
    return jobs


async def list_dead_jobs(
    conn: aiosqlite.Connection, *, limit: int = 50, tenant_id: str | None = None
) -> list[dict[str, Any]]:
    """列出重试耗尽、彻底失败的任务，按最近失败的排前面（updated_at 倒序）
    ——管理后台展示"失败任务"区块用，参数含义和 list_pending_jobs() 一致。
    """
    conn.row_factory = aiosqlite.Row
    if tenant_id is None:
        cursor = await conn.execute(
            "SELECT * FROM ingestion_jobs WHERE status = 'dead' "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
    else:
        cursor = await conn.execute(
            "SELECT * FROM ingestion_jobs WHERE status = 'dead' AND tenant_id = ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (tenant_id, limit),
        )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def _fetch_job(
    conn: aiosqlite.Connection, job_id: str, *, tenant_id: str
) -> dict[str, Any]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM ingestion_jobs WHERE job_id = ? AND tenant_id = ?",
        (job_id, tenant_id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise JobNotFoundError(f"任务不存在: {job_id}")
    return dict(row)


async def retry_job(conn: aiosqlite.Connection, job_id: str, *, tenant_id: str) -> None:
    """人工点击重试。两种任务状态可以走到这里：

    - **dead**（失败任务区块）：重新拉回 pending 队列，attempts 清零、
      last_error 清空——下一轮处理会把它当成一个全新任务重新尝试一次完整
      的 3 次自动重试，不受它之前已经用完的重试次数影响。
    - **疑似卡死的 pending 任务**（处理中任务区块，见 _is_stuck_pending）：
      任务本来就还是 pending 状态，不需要状态迁移，只需要把 started_at
      清空——process_pending_jobs() 的 list_pending_jobs() 查询本来就没有
      "已被取用不能再取"这层锁（job_concurrency 只限制同一批内的并发度，
      不是跨批次的任务级锁），下一次触发时这条任务会被当成一条正常排队
      的任务重新捡起来，就像它从未被处理过一样。

    两种情况之外（任务当前正常排队中，或正在被处理，或已完成）一律拒绝，
    见 JobNotDeadError 的说明。
    """
    job = await _fetch_job(conn, job_id, tenant_id=tenant_id)
    if job["status"] == "dead":
        await conn.execute(
            "UPDATE ingestion_jobs SET status='pending', attempts=0, last_error=NULL, "
            "started_at=NULL, updated_at=datetime('now') WHERE job_id=? AND tenant_id=?",
            (job_id, tenant_id),
        )
        await conn.commit()
        return
    if _is_stuck_pending(job):
        await conn.execute(
            "UPDATE ingestion_jobs SET started_at=NULL, updated_at=datetime('now') "
            "WHERE job_id=? AND tenant_id=?",
            (job_id, tenant_id),
        )
        await conn.commit()
        return
    raise JobNotDeadError(f"任务当前不是失败状态、也不是疑似卡死的处理中状态，无法重试: {job_id}")


async def delete_job(conn: aiosqlite.Connection, job_id: str, *, tenant_id: str) -> str:
    """删除一条任务记录，返回它的 file_path 供调用方清理磁盘上的孤儿文件
    ——这个函数本身不碰文件系统，"删磁盘文件"这个副作用留给调用方
    （app/api/admin_document_routes.py 已经有 _unlink_uploaded_file()
    做路径安全校验，delete_document() 也在用同一个函数，不重复实现一遍）。

    允许删除的两种状态（dead / 疑似卡死的 pending，见 retry_job 的同款
    说明和 _is_stuck_pending）都可能在中途已经往向量库写入过部分 chunk
    （_embed_and_upsert 分批 upsert，中途失败/中断不会回滚已经 upsert 的
    批次），调用方需要额外清理，这个函数本身只负责队列记录本身。
    """
    job = await _fetch_job(conn, job_id, tenant_id=tenant_id)
    if job["status"] != "dead" and not _is_stuck_pending(job):
        raise JobNotDeadError(f"任务当前不是失败状态、也不是疑似卡死的处理中状态，无法删除: {job_id}")
    await conn.execute(
        "DELETE FROM ingestion_jobs WHERE job_id=? AND tenant_id=?",
        (job_id, tenant_id),
    )
    await conn.commit()
    return job["file_path"]


async def mark_job_started(conn: aiosqlite.Connection, job_id: str) -> None:
    """在 worker 真正开始处理一条任务（而不是仅仅"排在队列里"）时调用，
    记录 started_at——这是判定"卡死"的唯一依据，见 _is_stuck_pending。"""
    await conn.execute(
        "UPDATE ingestion_jobs SET started_at=datetime('now'), updated_at=datetime('now') "
        "WHERE job_id=?",
        (job_id,),
    )
    await conn.commit()


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


async def _parse_file(
    path: Path,
    *,
    ocr: OcrFunction | None,
    ocr_render_dpi: int,
    ocr_max_concurrency: int,
    table_extractor: TableExtractionFunction | None,
    table_extraction_max_concurrency: int,
    ocr_semaphore: asyncio.Semaphore | None = None,
    table_semaphore: asyncio.Semaphore | None = None,
):
    """PDF/图片走原生异步（OCR 内部按 ocr_max_concurrency 并发），其余
    格式（.md/.docx/.csv）本身是同步解析函数，用 asyncio.to_thread 丢进
    线程池执行，不让这几种格式的解析占用事件循环——和 PDF/图片路径保持
    一样的"不阻塞其它请求"保证，不因为格式不同就有不同的行为。

    table_extractor 只对 .pdf 有意义（.md/.docx/.csv/图片本身不会有
    PyMuPDF 意义上的"表格"）。
    """
    suffix = path.suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return await parse_image(path, ocr=ocr)
    if suffix == ".pdf":
        return await parse_pdf(
            path,
            ocr=ocr,
            render_dpi=ocr_render_dpi,
            max_concurrency=ocr_max_concurrency,
            table_extractor=table_extractor,
            table_extraction_max_concurrency=table_extraction_max_concurrency,
            ocr_semaphore=ocr_semaphore,
            table_semaphore=table_semaphore,
        )
    parser = _PARSERS.get(suffix)
    if parser is None:
        raise ValueError(f"不支持的文件类型: {suffix}")
    return await asyncio.to_thread(parser, path)


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
    table_extractor: TableExtractionFunction | None = None,
    table_extraction_max_concurrency: int = _DEFAULT_TABLE_EXTRACTION_MAX_CONCURRENCY,
    job_concurrency: int = 1,
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

    job_concurrency 控制这一批任务里最多同时处理几份文档，默认 1（严格
    串行，和这次改造前完全一致）——见 IngestionSettings.job_concurrency
    的说明，提高这个值前必须先做真实的多文档并发负载测试。
    """
    jobs = await list_pending_jobs(conn, limit=limit)

    # OCR/表格提取的 Semaphore 只在这一批任务里构造一次、跨文档共享——
    # 不这样做的话，job_concurrency>1 时每份文档各自在 parse_pdf() 内部
    # 新建一个 Semaphore，等于每份文档独立拥有一份"满额"的账号并发预算，
    # 多文档同时摄取会共同把真实请求数顶到远超账号承受能力，而不是像
    # 现在这样按配置的上限受控地共享。
    ocr_semaphore = asyncio.Semaphore(ocr_max_concurrency) if ocr is not None else None
    table_semaphore = (
        asyncio.Semaphore(table_extraction_max_concurrency)
        if table_extractor is not None
        else None
    )
    # job_concurrency 控制"同时有几份文档在处理"，默认 1 和改造前完全
    # 一致；调大之前需要先实测多文档同时摄取时账号的真实承受能力（见
    # IngestionSettings.job_concurrency 的说明）。
    job_semaphore = asyncio.Semaphore(job_concurrency)

    async def _process_one_job(job: dict[str, Any]) -> bool:
        async with job_semaphore:
            tenant_id = job["tenant_id"]
            file_path = job["file_path"]
            await mark_job_started(conn, job["job_id"])
            try:
                await vector_store.delete_by_source(source=file_path, tenant_id=tenant_id)
                if job["action"] == "delete":
                    await remove_tracked_file(
                        conn, tenant_id=tenant_id, file_path=file_path
                    )
                else:
                    # _parse_file() 自己是异步的：PDF/图片路径原生走
                    # asyncio（OCR 网络请求用 asyncio.gather 并发，不阻塞
                    # 事件循环），其余同步格式内部用 asyncio.to_thread 兜底
                    # （见 _parse_file 的说明）。process_pending_jobs() 跑在
                    # FastAPI 后台任务的同一个事件循环里，这里不需要再额外包
                    # 一层 asyncio.to_thread。
                    chunks = await _parse_file(
                        Path(file_path),
                        ocr=ocr,
                        ocr_render_dpi=ocr_render_dpi,
                        ocr_max_concurrency=ocr_max_concurrency,
                        table_extractor=table_extractor,
                        table_extraction_max_concurrency=table_extraction_max_concurrency,
                        ocr_semaphore=ocr_semaphore,
                        table_semaphore=table_semaphore,
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
                return False
            await mark_job_completed(conn, job["job_id"])
            return True

    # 用 return_exceptions=True 等所有任务都跑完（不管成败）再统计结果，
    # 而不是用 gather 默认行为——_process_one_job 末尾的 mark_job_completed
    # 在 try/except 之外，万一它本身失败（比如并发场景下 aiosqlite 的偶发
    # 错误），默认 gather 行为会让这个异常直接从这里抛出，其它还在跑的
    # 任务变成没人处理的后台任务。这里把"跑出未捕获异常"也算作失败（不
    # 重新抛出），不影响每个任务真正的成功/失败判定——那部分已经在
    # _process_one_job 内部的 try/except 完成了，这里只是最后一道防线。
    results = await asyncio.gather(
        *(_process_one_job(job) for job in jobs), return_exceptions=True
    )
    return sum(1 for ok in results if ok is True)
