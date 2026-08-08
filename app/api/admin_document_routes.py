from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, UploadFile
from pydantic import BaseModel

import aiosqlite

from app.api import deps
from app.ingestion.ingestion_queue import (
    enqueue_ingestion_job,
    list_pending_jobs,
    process_pending_jobs,
)
from app.ingestion.tracking import compute_file_hash, list_tracked_files, remove_tracked_file
from app.providers.embedding import EmbeddingRegistry
from app.retrieval.vector_store import VectorStore

router = APIRouter(
    prefix="/api/admin/documents", dependencies=[Depends(deps.require_admin_session)]
)

_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100MB

# tenant_id 和 file.filename 都会被拼进落盘路径，两者都来自请求体（可被
# 调用方控制），必须先消毒再拼路径，否则 "../../x" 这种值能让文件写到
# upload_dir 之外。允许的字符集：Unicode 字母/数字/下划线 + 点 + 连字符，
# 路径分隔符（/ \）和盘符冒号都不在其中。
_UNSAFE_NAME_CHARS = re.compile(r"[^\w.\-]", re.UNICODE)


def _sanitize_filename(filename: str | None) -> str:
    """把上传文件名压成一个安全的单层文件名。

    结果会再被加上 uuid 前缀，所以即使原名是 "." / ".." 这类纯点串，最终
    文件名也只会是 "<uuid>_.." 这样的普通名字，不构成向上跳目录。
    """
    sanitized = _UNSAFE_NAME_CHARS.sub("_", filename or "")
    return sanitized or "upload"


def _validate_tenant_id(tenant_id: str) -> str:
    """tenant_id 直接当目录名用，不消毒成别的值——那会让两个不同租户撞进
    同一个目录。这里只做校验，不合法就 400 拒掉。
    """
    if not tenant_id or _UNSAFE_NAME_CHARS.search(tenant_id) or set(tenant_id) <= {"."}:
        raise HTTPException(status_code=400, detail="tenant_id 含非法字符")
    return tenant_id


class DocumentsListResponse(BaseModel):
    documents: list[dict]
    pending_jobs: list[dict]


class UploadResponse(BaseModel):
    job_id: str


async def _run_pending_jobs(
    ingestion_conn: aiosqlite.Connection,
    embedding_registry: EmbeddingRegistry,
    vector_store: VectorStore,
) -> None:
    """后台任务：入队后立即处理一批，不等外部 cron。"""
    await process_pending_jobs(
        ingestion_conn,
        embedding_registry=embedding_registry,
        embedding_provider_name=deps.DEFAULT_EMBEDDING_PROVIDER_NAME,
        vector_store=vector_store,
    )


@router.post("", response_model=UploadResponse)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    tenant_id: str = Form(...),
    build_graph: bool = Form(False),
    upload_dir: Path = Depends(deps.get_upload_dir),
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
    embedding_registry: EmbeddingRegistry = Depends(deps.get_embedding_registry),
    vector_store: VectorStore = Depends(deps.get_vector_store),
) -> UploadResponse:
    # tenant_id/build_graph 必须显式标 Form(...)：混用 UploadFile 和裸标量参数时
    # FastAPI 默认把裸标量参数当 query 参数解析，不会去读 multipart body 里
    # 同名的表单字段——前端是把这两个值和文件一起放进同一个 FormData 提交的
    # （见 Task 8 DocumentsPage.tsx），不标 Form(...) 会导致后端读到 422。
    contents = await file.read()
    if len(contents) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 100MB 上限")

    tenant_dir = upload_dir / _validate_tenant_id(tenant_id)
    tenant_dir.mkdir(parents=True, exist_ok=True)
    dest_path = tenant_dir / f"{uuid.uuid4().hex}_{_sanitize_filename(file.filename)}"
    dest_path.write_bytes(contents)

    content_hash = compute_file_hash(dest_path)
    job_id = await enqueue_ingestion_job(
        ingestion_conn,
        tenant_id=tenant_id,
        file_path=str(dest_path),
        content_hash=content_hash,
        action="ingest",
        build_graph=build_graph,
    )
    background_tasks.add_task(
        _run_pending_jobs, ingestion_conn, embedding_registry, vector_store
    )
    return UploadResponse(job_id=job_id)


@router.get("", response_model=DocumentsListResponse)
async def list_documents(
    tenant_id: str,
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
) -> DocumentsListResponse:
    documents = await list_tracked_files(ingestion_conn, tenant_id=tenant_id)
    all_pending = await list_pending_jobs(ingestion_conn, limit=50)
    pending_jobs = [job for job in all_pending if job["tenant_id"] == tenant_id]
    return DocumentsListResponse(documents=documents, pending_jobs=pending_jobs)


@router.delete("")
async def delete_document(
    tenant_id: str,
    file_path: str,
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
    vector_store: VectorStore = Depends(deps.get_vector_store),
) -> dict[str, bool]:
    await vector_store.delete_by_source(source=file_path, tenant_id=tenant_id)
    await remove_tracked_file(ingestion_conn, tenant_id=tenant_id, file_path=file_path)
    return {"deleted": True}
