"""数据库导入：测连通、预览、存数据源、重新同步（spec D3）。

**密码不落库**。存下来的只有连接信息、SQL 和列映射——它们是重跑时最贵的
部分（配一次列映射要十几分钟）。密码每次现给。

拉到行之后**走的是跟表格导入完全同一条管线**：同一份校验、同一份报告、
同一套跳过行记录。另写一条的话，同样的坏数据在两个入口下会有两种表现，
而用户会拿这两处互相印证。
"""
from __future__ import annotations

import csv
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.api import deps
from app.api.tenant_guard import require_active_tenant_or_404
from app.graphrag.db_sources_store import (
    create_db_source,
    delete_db_source,
    get_db_source,
    list_db_sources,
    touch_last_sync,
)
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.schema_etl import run_schema_etl
from app.graphrag.schema_etl_config import (
    InvalidSchemaETLConfigError,
    SchemaETLConfig,
    _parse_entity_mapping,
)
from app.ingestion.db_connector import (
    DbConnectionError,
    DbConnectionSpec,
    UnsafeQueryError,
    UnsupportedDriverError,
    check_connection,
    fetch_rows,
)

logger = logging.getLogger(__name__)

#: 这一组路由的路径前缀。`redact_validation_error` 用它判断"这个 422 是不是
#: 从数据库导入这里出来的"。
ROUTE_PREFIX = "/db-import/"


def redact_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse | None:
    """把 422 响应里回显的请求体去掉——**那里面有密码**。

    pydantic v2 + FastAPI 的默认 422 会把每个错误对应的 `input` 原样回显。
    对请求体级别的错误（比如 `extra="forbid"` 拒掉的那个 `password` 字段），
    `input` 就是**整个请求体**，密码就在里面；它会被写进任何一层访问/错误
    日志，也会被截图。有专门的用例钉着这一点。

    只处理本路由组的请求，别的路由不动：它们的 422 回显 input 是有用的
    调试信息，而且不含密码。不是本组的请求返回 None，交回给默认处理。

    `app/main.py` 在启动时把它挂成全局 RequestValidationError 处理器。
    """
    if ROUTE_PREFIX not in request.url.path:
        return None
    errors = [
        {key: value for key, value in error.items() if key != "input"}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


router = APIRouter(
    prefix="/api/admin/{tenant_id}/db-import",
    dependencies=[Depends(deps.require_admin_session)],
)

#: 预览最多取多少行。
#:
#: 不限制的话，「预览一下」会把两千万行拉进内存然后序列化成 JSON——那一下
#: 就是一次自己打自己的拒绝服务。
PREVIEW_LIMIT = 100

#: 同步时临时 CSV 的文件名。
#:
#: 落一份临时文件是为了复用表格导入那条管线：它的入口
#: （`etl_staging.read_table_rows`）只吃文件路径。给它加一个"行序列"入口会
#: 动到已经在线的表格导入路径，风险不对等。文件写在系统临时目录里、用完即删，
#: **绝不写进上传目录**——那里的东西会被当成用户上传的源文件列出来。
_SYNC_CSV_NAME = "db_sync.csv"


class ConnectionFields(BaseModel):
    """连接信息。**password 单独作为字段出现在需要它的请求里**，不在这里。"""

    model_config = ConfigDict(extra="forbid")

    driver: str
    host: str
    port: int
    database: str
    username: str

    def to_spec(self) -> DbConnectionSpec:
        return DbConnectionSpec(
            driver=self.driver,
            host=self.host,
            port=self.port,
            database=self.database,
            username=self.username,
        )


class TestConnectionRequest(ConnectionFields):
    password: str


class PreviewRequest(ConnectionFields):
    password: str
    query: str


class PreviewResponse(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    #: 是不是被 PREVIEW_LIMIT 截断了。不说的话用户会对着 100 行下
    #: 「这张表就这么大」的结论。
    truncated: bool


class SourceCreateRequest(ConnectionFields):
    """建数据源。

    **`extra="forbid"`（继承自 ConnectionFields）是这里的要害**：请求体里
    多出一个 `password` 会直接被拒，不是静默忽略。静默忽略的话，前端某次
    改动不小心把密码发上来我们会一直不知道——而它已经进了访问日志。
    """

    name: str
    query: str
    mapping: dict[str, Any]


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 密码不在库里，所以每次同步必须现给。
    #:
    #: 缺了要报错而不是拿空密码去连——空密码在某些配置下真的能连上，
    #: 那会连到一个错误的账号下，而用户以为同步的是他配的那个库。
    password: str


class DbSourceSummary(BaseModel):
    source_id: str
    name: str
    driver: str
    host: str
    port: int
    database: str
    username: str
    query: str
    mapping: dict[str, Any]
    #: 从没同步过时是 None。编一个 0 或当前时间出来的话，列表上会显示
    #: 「刚刚同步 · 0 行」，而它其实一次都没跑过。
    last_sync_at: str | None
    last_sync_rows: int | None


class DbSourceList(BaseModel):
    items: list[DbSourceSummary]


class SourceCreatedResponse(BaseModel):
    source_id: str


class SyncResponse(BaseModel):
    row_count: int
    entities_written: int
    entities_skipped: int


def _to_summary(source: dict[str, Any]) -> DbSourceSummary:
    return DbSourceSummary(
        source_id=source["source_id"],
        name=source["name"],
        driver=source["driver"],
        host=source["host"],
        port=source["port"],
        database=source["database"],
        username=source["username"],
        query=source["query"],
        mapping=source["mapping"],
        last_sync_at=source["last_sync_at"],
        last_sync_rows=source["last_sync_rows"],
    )


def _as_http_error(exc: Exception) -> HTTPException:
    """连接器的异常翻成 400。

    直接把 `str(exc)` 转给用户是安全的：`db_connector` 保证它的异常消息里
    没有密码（那是它最重要的一条约束，有专门的用例钉着）。这里**不再包一层
    自己的措辞**——「连不上 10.0.0.5:3306」比「连接失败」有用得多。
    """
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/test-connection")
async def test_db_connection(
    tenant_id: str,
    payload: TestConnectionRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        await check_connection(payload.to_spec(), payload.password)
    except (UnsupportedDriverError, DbConnectionError) as exc:
        raise _as_http_error(exc) from None
    return {"ok": True}


@router.post("/preview", response_model=PreviewResponse)
async def preview_db_query(
    tenant_id: str,
    payload: PreviewRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> PreviewResponse:
    """跑一次查询看前 100 行。列名一起给出来——列映射那一步照着它配。"""
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        columns, rows = await fetch_rows(
            payload.to_spec(), payload.password, payload.query, limit=PREVIEW_LIMIT
        )
    except (UnsupportedDriverError, UnsafeQueryError, DbConnectionError) as exc:
        raise _as_http_error(exc) from None
    return PreviewResponse(
        columns=columns,
        rows=[list(row) for row in rows],
        row_count=len(rows),
        truncated=len(rows) >= PREVIEW_LIMIT,
    )


@router.post("/sources", response_model=SourceCreatedResponse)
async def create_source(
    tenant_id: str,
    payload: SourceCreateRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> SourceCreatedResponse:
    await require_active_tenant_or_404(review_conn, tenant_id)
    source_id = uuid.uuid4().hex
    await create_db_source(
        review_conn,
        tenant_id=tenant_id,
        source_id=source_id,
        name=payload.name,
        spec=payload.to_spec(),
        query=payload.query,
        mapping=payload.mapping,
    )
    return SourceCreatedResponse(source_id=source_id)


@router.get("/sources", response_model=DbSourceList)
async def list_sources(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> DbSourceList:
    sources = await list_db_sources(review_conn, tenant_id=tenant_id)
    return DbSourceList(items=[_to_summary(s) for s in sources])


@router.delete("/sources/{source_id}")
async def remove_source(
    tenant_id: str,
    source_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    await require_active_tenant_or_404(review_conn, tenant_id)
    await delete_db_source(review_conn, tenant_id=tenant_id, source_id=source_id)
    return {"deleted": True}


@router.post("/sources/{source_id}/sync", response_model=SyncResponse)
async def sync_source(
    tenant_id: str,
    source_id: str,
    payload: SyncRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_neo4j_graph_client),
) -> SyncResponse:
    """按存下来的 SQL 和映射再跑一次。

    **用的是存下来的那份，不是请求里现给的。** 允许请求覆盖的话，「重新同步」
    这个动作的语义就不再是「再跑一次同样的」了——而用户点它的时候以为是。

    失败时不更新 `last_sync_at`：刷了的话列表上显示「上次同步 2 分钟前」，
    而那次同步一行都没导进去。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)
    source = await get_db_source(review_conn, tenant_id=tenant_id, source_id=source_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"数据源不存在：{source_id}")

    spec = DbConnectionSpec(
        driver=source["driver"], host=source["host"], port=source["port"],
        database=source["database"], username=source["username"],
    )
    try:
        columns, rows = await fetch_rows(spec, payload.password, source["query"])
    except (UnsupportedDriverError, UnsafeQueryError, DbConnectionError) as exc:
        raise _as_http_error(exc) from None

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        with (data_dir / _SYNC_CSV_NAME).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            writer.writerows(rows)

        try:
            entity_mapping = _parse_entity_mapping({**source["mapping"], "source_file": _SYNC_CSV_NAME})
        except (InvalidSchemaETLConfigError, KeyError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"这个数据源的列映射读不出来（{exc}）。请重新配置它的列映射。",
            ) from None

        try:
            report = await run_schema_etl(
                conn=review_conn,
                graph_client=graph_client,
                config=SchemaETLConfig(
                    tenant_id=tenant_id, entities=[entity_mapping], relations=[]
                ),
                data_dir=data_dir,
                # run_id 让这次同步跳掉的行在报错明细里聚成一组，跟别的跑批分得开。
                run_id=f"db-sync-{source_id}-{uuid.uuid4().hex[:8]}",
            )
        except Exception as exc:
            # 本体没确认、安全阀拦下、图谱挂了……都是用户要看到原因才知道
            # 下一步的事。不接的话它们冲成一个 500，响应体只有
            # 「Internal Server Error」。这条路径上的异常来自 ETL 管线，
            # 不经过数据库驱动，消息里不会有密码。
            logger.warning("数据源 %r（租户 %r）同步失败", source_id, tenant_id, exc_info=True)
            raise HTTPException(status_code=400, detail=f"同步没成功：{exc}") from None

    await touch_last_sync(
        review_conn, tenant_id=tenant_id, source_id=source_id, row_count=len(rows)
    )
    return SyncResponse(
        row_count=len(rows),
        entities_written=report.entities_written,
        entities_skipped=report.entities_skipped,
    )
