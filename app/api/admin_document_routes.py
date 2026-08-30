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
from fastapi.responses import FileResponse
from pydantic import BaseModel

import aiosqlite

from app.api import deps
from app.api.tenant_guard import require_active_tenant_or_404
from app.config.settings import Settings
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.terms_store import list_terms
from app.ingestion.ingestion_queue import (
    SUPPORTED_SUFFIXES,
    JobNotDeadError,
    JobNotFoundError,
    delete_job,
    enqueue_ingestion_job,
    list_dead_jobs,
    list_pending_jobs,
    process_pending_jobs,
    retry_job,
)
from app.ingestion.ocr_parser import OcrFunction
from app.ingestion.table_extraction import TableExtractionFunction
from app.ingestion.tracking import (
    compute_file_hash,
    count_tracked_files,
    list_tracked_files,
    remove_tracked_file,
)
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
    total: int
    pending_jobs: list[dict]
    dead_jobs: list[dict]


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
    ocr: OcrFunction | None,
    ocr_render_dpi: int,
    ocr_max_concurrency: int,
    table_extractor: TableExtractionFunction | None,
    table_extraction_max_concurrency: int,
    job_concurrency: int,
) -> None:
    """后台任务：入队后立即处理一批，不等外部 cron。

    图谱资源无条件传入：process_pending_jobs() 内部按每条任务自己的
    build_graph 列决定用不用（见 ingestion_queue.py），所以这里不能因为
    "本次上传没勾选建图"就传 None——同一批里别的任务可能勾了。

    ocr 为 None 时（未配置 OCR_BASE_URL/OCR_API_KEY）行为不变：无文字层的
    页面/图片直接跳过，产出 0 chunk，不报错——见 deps.get_ocr_function。
    table_extractor 同理，为 None 时（未配置 TABLE_EXTRACTION_MODEL）
    保持 PyMuPDF 规则猜表头的老行为——见 deps.get_table_extractor。
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
        ocr=ocr,
        ocr_render_dpi=ocr_render_dpi,
        ocr_max_concurrency=ocr_max_concurrency,
        table_extractor=table_extractor,
        table_extraction_max_concurrency=table_extraction_max_concurrency,
        job_concurrency=job_concurrency,
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
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    ocr: OcrFunction | None = Depends(deps.get_ocr_function),
    table_extractor: TableExtractionFunction | None = Depends(deps.get_table_extractor),
    settings: Settings = Depends(deps.get_settings),
) -> UploadResponse:
    # tenant_id/build_graph 必须显式标 Form(...)：混用 UploadFile 和裸标量参数时
    # FastAPI 默认把裸标量参数当 query 参数解析，不会去读 multipart body 里
    # 同名的表单字段——前端是把这两个值和文件一起放进同一个 FormData 提交的
    # （见 Task 8 DocumentsPage.tsx），不标 Form(...) 会导致后端读到 422。
    _reject_oversized_by_content_length(request)
    _validate_tenant_id(tenant_id)
    await require_active_tenant_or_404(review_conn, tenant_id)
    _validate_upload_suffix(file.filename)
    # 这个路由自己已经有权威的 tenant_id（Form 字段），不用 deps.get_terms
    # 那套独立的 gateway_tenant_id 解析——两者在这条请求里可能不是同一个
    # 值，直接按本路由的 tenant_id 加载术语表，避免跨租户读到错的术语表。
    terms: list[Term] = await list_terms(review_conn, tenant_id)

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
        ocr,
        settings.ocr.render_dpi,
        settings.ocr.max_concurrency,
        table_extractor,
        settings.table_extraction.max_concurrency,
        settings.ingestion.job_concurrency,
    )
    return UploadResponse(job_id=job_id)


@router.get("", response_model=DocumentsListResponse)
async def list_documents(
    tenant_id: str,
    page: int | None = None,
    page_size: int | None = None,
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
) -> DocumentsListResponse:
    # 同 admin_terms_routes.py::list_all_terms 的 Fix：page/page_size 都不传
    # 时不加 limit/offset 地调用 list_tracked_files()——它自己的默认值
    # （limit=None）就是"返回全部"，保持分页 query 参数引入之前的行为不变。
    # 只要任意一个参数被显式传入，才按分页语义处理。目前 DocumentsPage.tsx
    # 是这个接口唯一的调用方且总是同时传两个参数，所以这里没有实际在生产
    # 中触发过的调用方受影响——这是为了不再复现 Task 8 那类"裸 GET 被
    # 悄悄截断成第一页"的 bug，保持跟术语接口一致的契约。
    if page is None and page_size is None:
        documents = await list_tracked_files(ingestion_conn, tenant_id=tenant_id)
    else:
        effective_page = page or 1
        effective_page_size = page_size or 20
        offset = (effective_page - 1) * effective_page_size
        documents = await list_tracked_files(
            ingestion_conn, tenant_id=tenant_id, limit=effective_page_size, offset=offset
        )
    total = await count_tracked_files(ingestion_conn, tenant_id=tenant_id)
    pending_jobs = await list_pending_jobs(ingestion_conn, limit=50, tenant_id=tenant_id)
    dead_jobs = await list_dead_jobs(ingestion_conn, limit=50, tenant_id=tenant_id)
    return DocumentsListResponse(
        documents=documents, total=total, pending_jobs=pending_jobs, dead_jobs=dead_jobs
    )


def _unlink_uploaded_file(file_path: str, upload_dir: Path, *, tenant_id: str) -> None:
    """删掉后台上传落盘的原始文件，避免删除文档后磁盘上的副本永久残留。

    只删 upload_dir/{tenant_id} 之内的文件——不是"只要在 upload_dir 内随便
    哪个子目录就删"：上传路径本身就是按租户分子目录落盘的（tenant_dir =
    upload_dir / tenant_id，见 upload_document()），只校验到 upload_dir
    这一级会放过"file_path 指向别的租户子目录、tenant_id 却填自己的"这种
    跨租户请求——向量库和追踪表两处的删除都会因为 tenant_id 不匹配而是
    空操作，唯独磁盘文件这一步如果不做同样的租户级别校验就会被删掉，
    造成跨租户越权删除。追踪表里的 file_path 理论上都是本系统自己写
    进去的，但同一张表也记录 CLI 摄取（app/ingestion/main.py）扫描的
    任意目录，那些文件不归后台管理，误删会毁掉用户的原始语料——所以除了
    租户目录校验，仍然保留"必须在 upload_dir 之内"这道前提。
    """
    try:
        resolved = Path(file_path).resolve()
        tenant_root = (upload_dir / tenant_id).resolve()
    except OSError:  # pragma: no cover - 路径本身非法（比如带 NUL 字符）
        return
    if not resolved.is_relative_to(tenant_root):
        logger.info(
            "删除文档：%s 不在租户 %s 的上传目录内，仅清理索引，不动磁盘文件",
            file_path, tenant_id,
        )
        return
    resolved.unlink(missing_ok=True)


@router.delete("")
async def delete_document(
    tenant_id: str,
    file_path: str,
    upload_dir: Path = Depends(deps.get_upload_dir),
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
    vector_store: VectorStore = Depends(deps.get_vector_store),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    _validate_tenant_id(tenant_id)
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        await vector_store.delete_by_source(source=file_path, tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001 - 转成对前端有意义的错误，不裸抛 500
        logger.warning("删除文档失败（向量库这一步）：file_path=%s error=%s", file_path, exc)
        raise HTTPException(status_code=502, detail=f"删除向量数据失败：{exc}") from exc
    try:
        await remove_tracked_file(ingestion_conn, tenant_id=tenant_id, file_path=file_path)
    except Exception as exc:  # noqa: BLE001
        # 向量已经删掉了，这里再失败会留下"向量没了但追踪记录还在"的不一致
        # 状态——不隐藏这个事实，报错文案里说清楚，让管理员知道要手动核实。
        logger.warning("删除文档失败（追踪记录这一步）：file_path=%s error=%s", file_path, exc)
        raise HTTPException(
            status_code=502,
            detail=f"向量数据已删除，但清理追踪记录失败，可能需要手动核实：{exc}",
        ) from exc
    # 磁盘文件放在最后删：向量/追踪记录任一步失败都会在上面抛出、不会执行
    # 到这里，保证不会出现"文件已删但索引还在"的不可恢复状态。
    _unlink_uploaded_file(file_path, upload_dir, tenant_id=tenant_id)
    return {"deleted": True}


class RetryJobResponse(BaseModel):
    retried: bool


class DeleteJobResponse(BaseModel):
    deleted: bool


@router.post("/jobs/{job_id}/retry", response_model=RetryJobResponse)
async def retry_ingestion_job(
    job_id: str,
    tenant_id: str,
    background_tasks: BackgroundTasks,
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
    embedding_registry: EmbeddingRegistry = Depends(deps.get_embedding_registry),
    vector_store: VectorStore = Depends(deps.get_vector_store),
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    ocr: OcrFunction | None = Depends(deps.get_ocr_function),
    table_extractor: TableExtractionFunction | None = Depends(deps.get_table_extractor),
    settings: Settings = Depends(deps.get_settings),
) -> RetryJobResponse:
    _validate_tenant_id(tenant_id)
    await require_active_tenant_or_404(review_conn, tenant_id)
    # 同上（upload_document）：用本路由自己的权威 tenant_id（路径/查询参数）
    # 加载术语表，不经 deps.get_terms 的独立 gateway_tenant_id 解析。
    terms: list[Term] = await list_terms(review_conn, tenant_id)
    try:
        await retry_job(ingestion_conn, job_id, tenant_id=tenant_id)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="任务不存在")
    except JobNotDeadError:
        raise HTTPException(status_code=409, detail="该任务当前不是失败状态、也不是疑似卡死的处理中状态，无法重试")
    # 重置成 pending 后立即触发一次处理，跟上传文档时的行为一致——不用等
    # 外部轮询/下一次上传才把这条任务捡起来。
    background_tasks.add_task(
        _run_pending_jobs,
        ingestion_conn,
        embedding_registry,
        vector_store,
        llm_registry,
        terms,
        graph_client,
        review_conn,
        ocr,
        settings.ocr.render_dpi,
        settings.ocr.max_concurrency,
        table_extractor,
        settings.table_extraction.max_concurrency,
        settings.ingestion.job_concurrency,
    )
    return RetryJobResponse(retried=True)


@router.delete("/jobs/{job_id}", response_model=DeleteJobResponse)
async def delete_ingestion_job(
    job_id: str,
    tenant_id: str,
    upload_dir: Path = Depends(deps.get_upload_dir),
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
    vector_store: VectorStore = Depends(deps.get_vector_store),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> DeleteJobResponse:
    _validate_tenant_id(tenant_id)
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        file_path = await delete_job(ingestion_conn, job_id, tenant_id=tenant_id)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="任务不存在")
    except JobNotDeadError:
        raise HTTPException(status_code=409, detail="该任务当前不是失败状态、也不是疑似卡死的处理中状态，无法删除")
    # 任务是失败状态或疑似卡死状态才会走到这一步，两种情况都可能发生在
    # 部分 chunk 已经写进向量库之后（_embed_and_upsert 分批 upsert，中途
    # 失败/中断不会回滚已经 upsert 的批次）；这些孤儿 chunk 没有对应的
    # ingested_documents 记录，普通的 delete_document() 流程找不到它们，
    # 必须在这里主动清理，否则会永久留在向量库里继续参与检索。retry 路径
    # 不需要这一步，因为 process_pending_jobs 重新处理前总会先调用同一个
    # delete_by_source()。
    await vector_store.delete_by_source(source=file_path, tenant_id=tenant_id)
    _unlink_uploaded_file(file_path, upload_dir, tenant_id=tenant_id)
    return DeleteJobResponse(deleted=True)


class ChunkResponse(BaseModel):
    text: str


class ChunksListResponse(BaseModel):
    chunks: list[ChunkResponse]
    total: int


_CHUNK_PREVIEW_LIMIT = 200


@router.get("/chunks", response_model=ChunksListResponse)
async def list_document_chunks(
    tenant_id: str,
    file_path: str,
    vector_store: VectorStore = Depends(deps.get_vector_store),
) -> ChunksListResponse:
    _validate_tenant_id(tenant_id)
    records = await vector_store.list_by_source(source=file_path, tenant_id=tenant_id)
    return ChunksListResponse(
        chunks=[ChunkResponse(text=r.text) for r in records[:_CHUNK_PREVIEW_LIMIT]],
        total=len(records),
    )


# PDF/图片浏览器能原生渲染，用 inline 直接在新标签页里打开看；其它格式
# （docx/csv/md）浏览器没法渲染，走 attachment 触发下载，不是打开一堆
# 乱码/纯文本。
_INLINE_SUFFIXES = frozenset({".pdf", ".png", ".jpg", ".jpeg"})
_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".csv": "text/csv",
    ".md": "text/markdown",
}


@router.get("/file")
async def download_document_file(
    tenant_id: str,
    file_path: str,
    upload_dir: Path = Depends(deps.get_upload_dir),
) -> FileResponse:
    _validate_tenant_id(tenant_id)
    try:
        resolved = Path(file_path).resolve()
        tenant_root = (upload_dir / tenant_id).resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="文件不存在") from None
    # 校验规则跟 _unlink_uploaded_file() 完全一致（同样必须落在
    # upload_dir/{tenant_id} 内），理由见那个函数的说明——这里额外要求
    # is_file()：目录本身也可能落在这个前缀下，不该被当成"文件"读出去。
    if not resolved.is_relative_to(tenant_root) or not resolved.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    suffix = resolved.suffix.lower()
    media_type = _MEDIA_TYPES.get(suffix, "application/octet-stream")
    return FileResponse(
        resolved,
        media_type=media_type,
        filename=resolved.name,
        content_disposition_type="inline" if suffix in _INLINE_SUFFIXES else "attachment",
    )
