"""报错明细：三个来源各自的失败清单（spec D7）。

这一页要回答的是「有什么坏了、我该做什么」。三个来源的修复动作完全不同：

| 来源 | 一行长什么样 | 修复动作 |
|---|---|---|
| 文档失败 | `xx.pdf` · OCR 超时 | **重试** |
| 表格跳行 | `商品表.csv` 第 88 行 · 价格非数字 | **下载这批**去改表格 |
| 问答未命中 | 「库存多少」· 无匹配 | **去建模** |

所以它们分成三个端点、三个页签，不压成一个"错误列表"——压平了等于让用户
自己猜该干什么，而这三件事分别落在三个不同的人手上。

三张表分别活在三个连接上（ingestion / review / memory），这也是为什么这一页
必须有自己的路由：任何一个端点都拿不到另外两个的数据。
"""
from __future__ import annotations

import logging

import aiosqlite
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api import deps
from app.graphrag.etl_skipped_rows import count_skipped_rows, list_skipped_rows
from app.ingestion.ingestion_queue import list_dead_jobs
from app.memory.qa_diagnostics import list_diagnostics

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/{tenant_id}/errors",
    dependencies=[Depends(deps.require_admin_session)],
)

#: 一页最多返回多少条。三个页签共用。
#:
#: 两万行的导入全跳掉是可能发生的（列名对不上），不设上限的话这一页会把
#: 两万行一次塞给浏览器。
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class DocumentFailure(BaseModel):
    job_id: str
    file_path: str
    #: 具体错误，不是一个"失败了"的布尔。用户据此判断是重试还是换个文件
    #: ——OCR 超时和格式不支持要做的事完全不同。
    last_error: str | None
    attempts: int
    updated_at: str


class EtlSkippedRow(BaseModel):
    id: int
    run_id: str
    label: str
    source_file: str
    #: 行号。只留原因不留行号的话，用户得在两万行里自己找那一行。
    row_number: int
    reason: str
    created_at: str


class QaFailure(BaseModel):
    id: int
    session_id: str
    question: str
    answer: str
    #: no_match（去建模）还是 error（去看日志）。两种完全不同的修复动作，
    #: 压成一个"失败"就等于没说。
    outcome: str
    created_at: str


class DocumentFailureList(BaseModel):
    items: list[DocumentFailure]


class EtlSkippedRowList(BaseModel):
    items: list[EtlSkippedRow]


class QaFailureList(BaseModel):
    items: list[QaFailure]


class ErrorCounts(BaseModel):
    """三个页签各自的数量。

    **三个键恒在，空的那一类是 0 不是键不存在**：键不存在的话前端读到
    undefined，角标要么不显示要么显示 NaN——而"这一类没有问题"是一个用户
    需要看到的结论，不是一个可以省略的空。
    """

    documents: int
    etl_rows: int
    qa: int


@router.get("/documents", response_model=DocumentFailureList)
async def list_document_failures(
    tenant_id: str,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
) -> DocumentFailureList:
    """重试耗尽、彻底失败的文档任务。

    复用 `list_dead_jobs`——"哪些状态算彻底失败"这件事只该有一个定义。
    这一页自己写一遍 `status = ...` 的话，队列那边改了状态词汇表，这里会
    静默地少列或多列，而没有任何东西会红。
    """
    jobs = await list_dead_jobs(ingestion_conn, limit=limit, tenant_id=tenant_id)
    return DocumentFailureList(
        items=[
            DocumentFailure(
                job_id=job["job_id"],
                file_path=job["file_path"],
                last_error=job.get("last_error"),
                attempts=job.get("attempts", 0),
                updated_at=job["updated_at"],
            )
            for job in jobs
        ]
    )


@router.get("/etl-rows", response_model=EtlSkippedRowList)
async def list_etl_skipped_rows(
    tenant_id: str,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> EtlSkippedRowList:
    """导入时跳掉的行，跨 run 累积。"""
    rows = await list_skipped_rows(
        review_conn, tenant_id=tenant_id, limit=limit, offset=offset
    )
    return EtlSkippedRowList(items=[EtlSkippedRow(**row) for row in rows])


@router.get("/qa", response_model=QaFailureList)
async def list_qa_failures(
    tenant_id: str,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    memory_conn: aiosqlite.Connection = Depends(deps.get_memory_conn),
) -> QaFailureList:
    """答不出来的那些问答（`outcome != 'answered'`）。

    过滤在 Python 侧做而不是 SQL：`list_diagnostics` 是共享的读函数，给它
    加一个 outcome 参数会让每个调用方都要想一遍"我要不要过滤"。这一页的
    量级由 `RETENTION_PER_TENANT`（500）封顶，取回来再筛不构成问题。
    """
    rows = await list_diagnostics(memory_conn, tenant_id=tenant_id, limit=None)
    failures = [row for row in rows if row["outcome"] != "answered"]
    return QaFailureList(items=[QaFailure(**row) for row in failures[:limit]])


@router.get("/counts", response_model=ErrorCounts)
async def get_error_counts(
    tenant_id: str,
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    memory_conn: aiosqlite.Connection = Depends(deps.get_memory_conn),
) -> ErrorCounts:
    """三个页签的角标。"""
    jobs = await list_dead_jobs(ingestion_conn, limit=MAX_LIMIT, tenant_id=tenant_id)
    diagnostics = await list_diagnostics(memory_conn, tenant_id=tenant_id, limit=None)
    return ErrorCounts(
        documents=len(jobs),
        etl_rows=await count_skipped_rows(review_conn, tenant_id=tenant_id),
        qa=sum(1 for row in diagnostics if row["outcome"] != "answered"),
    )
