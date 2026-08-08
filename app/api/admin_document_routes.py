from __future__ import annotations

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

router = APIRouter(prefix="/admin/documents", dependencies=[Depends(deps.require_admin_session)])

_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100MB


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

    tenant_dir = upload_dir / tenant_id
    tenant_dir.mkdir(parents=True, exist_ok=True)
    dest_path = tenant_dir / f"{uuid.uuid4().hex}_{file.filename}"
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
