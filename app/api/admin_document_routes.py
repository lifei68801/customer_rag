from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from pydantic import BaseModel

import aiosqlite

from app.api import deps
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.ingestion.ingestion_queue import (
    SUPPORTED_SUFFIXES,
    enqueue_ingestion_job,
    list_pending_jobs,
    process_pending_jobs,
)
from app.ingestion.tracking import compute_file_hash, list_tracked_files, remove_tracked_file
from app.providers.embedding import EmbeddingRegistry
from app.providers.registry import ProviderRegistry
from app.retrieval.vector_store import VectorStore
from app.tenancy import is_valid_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/documents", dependencies=[Depends(deps.require_admin_session)]
)

_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100MB

# file.filename 会被拼进落盘路径，来自请求体（可被调用方控制），必须先消毒
# 再拼路径，否则 "../../x" 这种值能让文件写到 upload_dir 之外。允许的字符集：
# Unicode 字母/数字/下划线 + 点 + 连字符，路径分隔符（/ \）和盘符冒号都不在其中。
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

    校验规则必须和 app/tenancy.py（= Milvus 过滤表达式那层用的同一份）
    完全一致：只在这里放宽（比如允许中文/点号）会让请求拿到 200 + job_id、
    文件已落盘，然后在后台任务或 DELETE 里撞上 Milvus 的严格校验，表现为
    没有干净错误信息的后台失败或裸 500。
    """
    if not is_valid_tenant_id(tenant_id):
        raise HTTPException(
            status_code=400, detail="tenant_id 只能包含字母、数字、下划线和连字符"
        )
    return tenant_id


def _validate_upload_suffix(filename: str | None) -> None:
    """扩展名不在摄取管线支持范围内就同步 400，不落盘、不入队。"""
    suffix = Path(filename or "").suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        supported = "/".join(sorted(SUPPORTED_SUFFIXES))
        raise HTTPException(
            status_code=400, detail=f"不支持的文件类型 {suffix or '(无扩展名)'}，支持：{supported}"
        )


class DocumentsListResponse(BaseModel):
    documents: list[dict]
    pending_jobs: list[dict]


class UploadResponse(BaseModel):
    job_id: str


async def _run_pending_jobs(
    ingestion_conn: aiosqlite.Connection,
    embedding_registry: EmbeddingRegistry,
    vector_store: VectorStore,
    graph_llm_registry: ProviderRegistry,
    graph_terms: list[Term],
    graph_client: Neo4jGraphClient,
    graph_review_conn: aiosqlite.Connection | None,
) -> None:
    """后台任务：入队后立即处理一批，不等外部 cron。

    图谱资源无条件传入：process_pending_jobs() 内部按每条任务自己的
    build_graph 列决定用不用（见 ingestion_queue.py），所以这里不能因为
    "本次上传没勾选建图"就传 None——同一批里别的任务可能勾了。
    """
    await process_pending_jobs(
        ingestion_conn,
        embedding_registry=embedding_registry,
        embedding_provider_name=deps.DEFAULT_EMBEDDING_PROVIDER_NAME,
        vector_store=vector_store,
        graph_llm_registry=graph_llm_registry,
        graph_llm_provider_name=deps.DEFAULT_LLM_PROVIDER_NAME,
        graph_terms=graph_terms,
        graph_client=graph_client,
        graph_review_conn=graph_review_conn,
    )


def _reject_oversized_by_content_length(request: Request) -> None:
    """在把整个请求体读进内存之前，先用 Content-Length 头挡掉超大上传。

    注意范围：FastAPI 解析 multipart 表单是在进入本函数之前发生的，
    Starlette 超过 1MB 会 spool 到临时文件而不是常驻内存，所以这里挡下的是
    后面那句 `await file.read()`（一次性把全部字节读进进程内存）。要在字节
    真正到达进程之前就拒绝，需要 ASGI 中间件层按 Content-Length 提前 413，
    那是比本次修复更大的改动，暂未做。

    头缺失或撒谎时这里什么都不做，交给下面读完之后的实际长度校验兜底。
    """
    raw_length = request.headers.get("content-length")
    if raw_length is None:
        return
    try:
        declared = int(raw_length)
    except ValueError:
        return
    if declared > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 100MB 上限")


@router.post("", response_model=UploadResponse)
async def upload_document(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile,
    tenant_id: str = Form(...),
    build_graph: bool = Form(False),
    upload_dir: Path = Depends(deps.get_upload_dir),
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
    embedding_registry: EmbeddingRegistry = Depends(deps.get_embedding_registry),
    vector_store: VectorStore = Depends(deps.get_vector_store),
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
    terms: list[Term] = Depends(deps.get_terms),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> UploadResponse:
    # tenant_id/build_graph 必须显式标 Form(...)：混用 UploadFile 和裸标量参数时
    # FastAPI 默认把裸标量参数当 query 参数解析，不会去读 multipart body 里
    # 同名的表单字段——前端是把这两个值和文件一起放进同一个 FormData 提交的
    # （见 Task 8 DocumentsPage.tsx），不标 Form(...) 会导致后端读到 422。
    _reject_oversized_by_content_length(request)
    _validate_tenant_id(tenant_id)
    _validate_upload_suffix(file.filename)

    contents = await file.read()
    if len(contents) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 100MB 上限")

    tenant_dir = upload_dir / tenant_id
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
        _run_pending_jobs,
        ingestion_conn,
        embedding_registry,
        vector_store,
        llm_registry,
        terms,
        graph_client,
        review_conn,
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


def _unlink_uploaded_file(file_path: str, upload_dir: Path) -> None:
    """删掉后台上传落盘的原始文件，避免删除文档后磁盘上的副本永久残留。

    只删 upload_dir 之内的文件：追踪表里的 file_path 理论上都是本系统自己
    写进去的，但同一张表也记录 CLI 摄取（app/ingestion/main.py）扫描的
    任意目录，那些文件不归后台管理，误删会毁掉用户的原始语料。所以这里
    先 resolve 再确认它确实在 upload_dir 底下，否则静默跳过。
    """
    try:
        resolved = Path(file_path).resolve()
        root = upload_dir.resolve()
    except OSError:  # pragma: no cover - 路径本身非法（比如带 NUL 字符）
        return
    if not resolved.is_relative_to(root):
        logger.info("删除文档：%s 不在上传目录内，仅清理索引，不动磁盘文件", file_path)
        return
    resolved.unlink(missing_ok=True)


@router.delete("")
async def delete_document(
    tenant_id: str,
    file_path: str,
    upload_dir: Path = Depends(deps.get_upload_dir),
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
    vector_store: VectorStore = Depends(deps.get_vector_store),
) -> dict[str, bool]:
    _validate_tenant_id(tenant_id)
    await vector_store.delete_by_source(source=file_path, tenant_id=tenant_id)
    await remove_tracked_file(ingestion_conn, tenant_id=tenant_id, file_path=file_path)
    # 磁盘文件放在最后删：向量/追踪记录任一步失败都会抛出、不会执行到这里，
    # 保证不会出现"文件已删但索引还在"的不可恢复状态。
    _unlink_uploaded_file(file_path, upload_dir)
    return {"deleted": True}
