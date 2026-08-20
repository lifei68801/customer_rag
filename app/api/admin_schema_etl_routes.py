from __future__ import annotations

import csv
import io
import json
import logging
import re
import shutil
import uuid
import zipfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api import deps
from app.graphrag.etl_runs_store import (
    EtlRunAlreadyRunningError,
    EtlRunNotFoundError,
    create_etl_run,
    get_etl_run,
    list_etl_runs,
    mark_etl_run_completed,
    mark_etl_run_failed,
)
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology_categories import list_term_types
from app.graphrag.ontology_constraints import list_allowed_combinations
from app.graphrag.ontology_lifecycle import is_ontology_confirmed
from app.graphrag.schema_etl import run_schema_etl
from app.graphrag.schema_etl_config import load_schema_etl_config
from app.graphrag.schema_etl_sample import (
    EmptySchemaError,
    SampleFile,
    generate_schema_etl_sample_files,
)
from app.graphrag.tenants_store import TenantNotFoundError, require_active_tenant

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/{tenant_id}/schema-etl", dependencies=[Depends(deps.require_admin_session)]
)

# 上传的 CSV 文件必须以原始文件名落盘——run_schema_etl 靠 data_dir /
# mapping.source_file 按 YAML 里声明的确切文件名去读文件，这里不能像
# admin_document_routes.py::_sanitize_filename 那样加 uuid 前缀（那是为了
# 避免同名文件互相覆盖，这里每次跑批都有自己独立的 run_id 目录，天然不会
# 撞名，加前缀反而会让文件名和 YAML 里的 source_file 对不上）。只消毒路径
# 分隔符等危险字符，防止用文件名逃出 run_dir。
_UNSAFE_NAME_CHARS = re.compile(r"[^\w.\-]", re.UNICODE)


def _sanitize_data_filename(filename: str) -> str:
    sanitized = _UNSAFE_NAME_CHARS.sub("_", filename) or "unnamed"
    # 上面的正则只剥掉路径分隔符等危险字符，"." 和 ".." 本身不含任何被剥掉的
    # 字符，会原样穿透——拼进 run_dir / sanitized 之后分别指向 run_dir 自己
    # 和它的父目录（父目录必然已存在，是 run_dir.mkdir(parents=True) 顺带建
    # 出来的），对着一个已存在的目录 write_bytes() 会抛 IsADirectoryError，
    # 不是"逃出 run_dir"式的任意路径穿越，但仍然违反了本函数自己的契约
    # （防止用文件名逃出 run_dir），必须单独兜底这两个纯点的特殊值。
    if sanitized in (".", ".."):
        sanitized = f"_{sanitized}"
    return sanitized


class StatusResponse(BaseModel):
    ontology_confirmed: bool


class StartRunResponse(BaseModel):
    run_id: str


class RunSummaryResponse(BaseModel):
    run_id: str
    status: str
    started_at: str
    finished_at: str | None


class RunListResponse(BaseModel):
    runs: list[RunSummaryResponse]


class RunDetailResponse(BaseModel):
    run_id: str
    status: str
    started_at: str
    finished_at: str | None
    report: dict | None
    error: str | None


class SampleFileResponse(BaseModel):
    filename: str
    content: str


class SampleResponse(BaseModel):
    files: list[SampleFileResponse]


async def _build_sample_files(
    tenant_id: str, review_conn: aiosqlite.Connection
) -> list[SampleFile]:
    if not await is_ontology_confirmed(review_conn, tenant_id):
        raise HTTPException(
            status_code=400, detail=f"租户 {tenant_id!r} 的本体 schema 还没有确认"
        )
    term_types = await list_term_types(review_conn, tenant_id, status="confirmed")
    allowed_combinations = await list_allowed_combinations(review_conn, tenant_id, status="confirmed")
    try:
        return generate_schema_etl_sample_files(
            tenant_id=tenant_id, term_types=term_types, allowed_combinations=allowed_combinations,
        )
    except EmptySchemaError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/status", response_model=StatusResponse)
async def get_schema_etl_status(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> StatusResponse:
    return StatusResponse(ontology_confirmed=await is_ontology_confirmed(review_conn, tenant_id))


async def _run_schema_etl_job(
    *,
    conn: aiosqlite.Connection,
    graph_client: Neo4jGraphClient,
    run_id: str,
    tenant_id: str,
    config_path: Path,
    data_dir: Path,
) -> None:
    """后台异步执行的实际跑批体——run_schema_etl 自己会再检查一次
    is_ontology_confirmed（本路由的 /runs 入口已经提前检查过一次，这里的
    检查是它自己的兜底，不是重复劳动），所以 SchemaETLNotConfirmedError
    在这里仍然是一个合法的、需要捕获的失败分支（例如页面检查通过后、
    真正跑批前，租户 schema 被别人改成未确认）。"""
    try:
        config = load_schema_etl_config(config_path)
        report = await run_schema_etl(conn=conn, graph_client=graph_client, config=config, data_dir=data_dir)
        await mark_etl_run_completed(
            conn,
            run_id=run_id,
            finished_at=datetime.now().isoformat(),
            report_json=json.dumps(asdict(report), ensure_ascii=False),
        )
    except Exception as exc:
        logger.exception("ETL 跑批 %r（租户 %r）执行失败", run_id, tenant_id)
        await mark_etl_run_failed(conn, run_id=run_id, finished_at=datetime.now().isoformat(), error=str(exc))


@router.post("/runs", response_model=StartRunResponse)
async def start_schema_etl_run(
    tenant_id: str,
    background_tasks: BackgroundTasks,
    config: UploadFile,
    data_files: list[UploadFile] = [],
    upload_dir: Path = Depends(deps.get_upload_dir),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> StartRunResponse:
    try:
        await require_active_tenant(review_conn, tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    if not await is_ontology_confirmed(review_conn, tenant_id):
        raise HTTPException(status_code=400, detail=f"租户 {tenant_id!r} 的本体 schema 还没有确认")

    run_id = uuid.uuid4().hex
    run_dir = upload_dir / "schema-etl" / tenant_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    config_path = run_dir / "config.yaml"
    config_path.write_bytes(await config.read())

    # 配置文件里声明的 tenant_id 才是 run_schema_etl 真正写入数据时用的租户——
    # 必须和 URL 路径上的 tenant_id（用来做并发防护、schema 确认预检查、
    # 历史记录归属的那个）一致，否则并发防护形同虚设：往两个不同路径
    # tenant_id 提交、但 YAML 里都声明同一个真实 tenant_id，能绕开下面的
    # 部分唯一索引保护，破坏 etl_stable_code_registry 的串行执行假设。
    # 解析失败（格式错误的 YAML）也在这里提前暴露成 400，而不是让跑批悄悄
    # 创建一条 running 记录、后台任务里才发现配置读不出来。
    try:
        parsed_config = load_schema_etl_config(config_path)
    except Exception as exc:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"配置文件解析失败：{exc}")
    if parsed_config.tenant_id != tenant_id:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise HTTPException(
            status_code=400,
            detail=f"配置文件里的 tenant_id {parsed_config.tenant_id!r} 与当前操作的租户 {tenant_id!r} 不一致",
        )

    for data_file in data_files:
        if not data_file.filename:
            continue
        dest = run_dir / _sanitize_data_filename(data_file.filename)
        dest.write_bytes(await data_file.read())

    started_at = datetime.now().isoformat()
    try:
        await create_etl_run(review_conn, run_id=run_id, tenant_id=tenant_id, started_at=started_at)
    except EtlRunAlreadyRunningError as exc:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise HTTPException(status_code=409, detail=str(exc))

    background_tasks.add_task(
        _run_schema_etl_job,
        conn=review_conn,
        graph_client=graph_client,
        run_id=run_id,
        tenant_id=tenant_id,
        config_path=config_path,
        data_dir=run_dir,
    )
    return StartRunResponse(run_id=run_id)


@router.get("/runs", response_model=RunListResponse)
async def list_schema_etl_runs(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> RunListResponse:
    runs = await list_etl_runs(review_conn, tenant_id)
    return RunListResponse(
        runs=[
            RunSummaryResponse(
                run_id=r.run_id, status=r.status, started_at=r.started_at, finished_at=r.finished_at
            )
            for r in runs
        ]
    )


@router.get("/runs/{run_id}", response_model=RunDetailResponse)
async def get_schema_etl_run(
    tenant_id: str, run_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> RunDetailResponse:
    try:
        detail = await get_etl_run(review_conn, tenant_id=tenant_id, run_id=run_id)
    except EtlRunNotFoundError:
        raise HTTPException(status_code=404, detail="跑批不存在")
    return RunDetailResponse(
        run_id=detail.run_id, status=detail.status, started_at=detail.started_at,
        finished_at=detail.finished_at, report=detail.report, error=detail.error,
    )


@router.get("/runs/{run_id}/report.csv")
async def download_schema_etl_report_csv(
    tenant_id: str, run_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> StreamingResponse:
    try:
        detail = await get_etl_run(review_conn, tenant_id=tenant_id, run_id=run_id)
    except EtlRunNotFoundError:
        raise HTTPException(status_code=404, detail="跑批不存在")
    if detail.report is None:
        raise HTTPException(status_code=404, detail="该跑批没有可下载的报告")

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["label", "source_file", "row_number", "reason"])
    for row in detail.report.get("skipped_rows", []):
        writer.writerow([row["label"], row["source_file"], row["row_number"], row["reason"]])
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{run_id}_skipped_rows.csv"'},
    )


@router.get("/sample", response_model=SampleResponse)
async def get_schema_etl_sample(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> SampleResponse:
    files = await _build_sample_files(tenant_id, review_conn)
    return SampleResponse(files=[SampleFileResponse(filename=f.filename, content=f.content) for f in files])


@router.get("/sample.zip")
async def download_schema_etl_sample_zip(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> StreamingResponse:
    files = await _build_sample_files(tenant_id, review_conn)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in files:
            zf.writestr(file.filename, file.content)
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{tenant_id}_schema_etl_sample.zip"'},
    )
