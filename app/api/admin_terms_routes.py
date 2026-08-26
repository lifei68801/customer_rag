from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

import aiosqlite

from app.api import deps
from app.graphrag.duplicate_detection import find_similar_terms
from app.graphrag.duplicate_review_queue import is_tombstoned
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.tenants_store import TenantNotFoundError, require_active_tenant
from app.graphrag.terms_store import (
    InvalidExtraPropertyTypeError,
    TermNameConflictError,
    TermNotFoundError,
    UnknownCategoryError,
    count_terms,
    create_term,
    delete_term,
    get_term,
    list_terms,
    update_term,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/{tenant_id}/terms", dependencies=[Depends(deps.require_admin_session)])


class TermResponse(BaseModel):
    standard_name: str
    aliases: list[str]
    term_type: str
    extra_properties: dict[str, Any] = {}
    source: str
    similar_terms: list[dict[str, Any]] | None = None


class TermListResponse(BaseModel):
    terms: list[TermResponse]
    total: int


class TermWriteRequest(BaseModel):
    standard_name: str
    aliases: list[str]
    term_type: str
    extra_properties: dict[str, Any] = {}
    source: Literal["manual", "etl", "review", "unknown"] = "manual"

    @field_validator("standard_name")
    @classmethod
    def _validate_standard_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("standard_name 不能为空")
        if "/" in stripped:
            raise ValueError("standard_name 不能包含 /")
        return stripped

    @field_validator("term_type")
    @classmethod
    def _validate_required_field(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("字段不能为空")
        return stripped

    @field_validator("aliases")
    @classmethod
    def _clean_aliases(cls, value: list[str]) -> list[str]:
        return [alias.strip() for alias in value if alias.strip()]


def _to_response(term: Term, *, similar_terms: list[dict[str, Any]] | None = None) -> TermResponse:
    return TermResponse(
        standard_name=term.standard_name,
        aliases=term.aliases,
        term_type=term.term_type,
        extra_properties=term.extra_properties,
        source=term.source,
        similar_terms=similar_terms,
    )


@router.get("", response_model=TermListResponse)
async def list_all_terms(
    tenant_id: str,
    page: int | None = None,
    page_size: int | None = None,
    source: str | None = None,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> TermListResponse:
    # page/page_size 都不传（比如 termsApi.ts 里不分页的 fetchTerms()，
    # GraphReviewsPage.tsx 的标准名自动补全用它拉全量数据做前端过滤）时，
    # 不加 limit/offset 地调用 list_terms()——它自己的默认值就是"返回全部"，
    # 保持这个分页 query 参数引入之前的行为不变。只要任意一个参数被显式
    # 传入（管理后台自己的分页列表 fetchTermsPage() 两个参数总是一起传），
    # 才按分页语义处理。
    if page is None and page_size is None:
        terms = await list_terms(review_conn, tenant_id, source=source)
    else:
        effective_page = page or 1
        effective_page_size = page_size or 20
        offset = (effective_page - 1) * effective_page_size
        terms = await list_terms(
            review_conn, tenant_id, limit=effective_page_size, offset=offset, source=source
        )
    total = await count_terms(review_conn, tenant_id, source=source)
    return TermListResponse(terms=[_to_response(term) for term in terms], total=total)


@router.post("", response_model=TermResponse)
async def create_new_term(
    tenant_id: str,
    payload: TermWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> TermResponse:
    try:
        await require_active_tenant(review_conn, tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    # 创建前先跟同租户、同类型的现有术语比一遍相似度，供管理员在提交后
    # 直接看到"这个新名字是不是已经有一个很像的术语了"——查询范围限定在
    # 同 term_type，避免不同类型之间凑巧撞名字的噪声提示。
    existing_terms = await list_terms(review_conn, tenant_id, source=None)
    # 已经被合并过的墓碑行（duplicate_review_queue.approve_duplicate_suggestion
    # 打上的标记）排除在外——它的 standard_name 字面包含被合并前的原名，不该
    # 被当成"这个新名字看起来很像"的提示对象，见 is_tombstoned() 的说明。
    same_type_terms = [
        t for t in existing_terms
        if t.term_type == payload.term_type and not is_tombstoned(t)
    ]
    similar = find_similar_terms(payload.standard_name, same_type_terms)
    similar_terms_payload = [
        {"node_key": term.node_key, "standard_name": term.standard_name, "similarity_score": score}
        for term, score in similar
    ]
    try:
        await create_term(
            review_conn,
            tenant_id=tenant_id,
            standard_name=payload.standard_name,
            aliases=payload.aliases,
            term_type=payload.term_type,
            extra_properties=payload.extra_properties,
            source=payload.source,
        )
    except TermNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except UnknownCategoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except InvalidExtraPropertyTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    term = Term(
        tenant_id=tenant_id,
        node_key=f"{payload.term_type}:{payload.standard_name}",
        standard_name=payload.standard_name,
        aliases=payload.aliases,
        term_type=payload.term_type,
        extra_properties=payload.extra_properties,
        source=payload.source,
    )
    # 新增成功后立即同步进图谱（属性+别名节点），不留图谱异步落后的窗口。
    try:
        await graph_client.sync_term(term)
    except Exception:
        logger.exception(
            "术语 %r（租户 %r）已写入 SQLite 但同步进图谱失败——两侧数据已不一致，需要人工核对",
            term.standard_name, tenant_id,
        )
        raise
    return _to_response(term, similar_terms=similar_terms_payload)


@router.put("/{standard_name}", response_model=TermResponse)
async def update_existing_term(
    tenant_id: str,
    standard_name: str,
    payload: TermWriteRequest,
    term_type: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> TermResponse:
    try:
        await require_active_tenant(review_conn, tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    try:
        existing_before_update = await get_term(review_conn, tenant_id, standard_name, term_type)
        await update_term(
            review_conn,
            tenant_id=tenant_id,
            standard_name=standard_name,
            new_standard_name=payload.standard_name,
            aliases=payload.aliases,
            term_type=payload.term_type,
            extra_properties=payload.extra_properties,
            current_term_type=term_type,
        )
    except TermNotFoundError:
        raise HTTPException(status_code=404, detail="术语不存在")
    except TermNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except UnknownCategoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except InvalidExtraPropertyTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    node_key = existing_before_update.node_key
    if payload.standard_name != standard_name:
        # 改名：先对同一个图节点做属性级联更新（保留已有关系边），再用
        # sync_term 刷新 type/别名。sync_term 现在按
        # {tenant_id, node_key}（创建时固定的身份键，改名后不变——
        # ADR-0003）MERGE 匹配节点，不再依赖 standard_name，两次调用的
        # 顺序其实已经不影响"匹配到同一个节点"这件事本身；这里保留
        # rename_term_node 在前的顺序只是让图谱里的 standard_name 尽快
        # 反映新值，不是绕开某个必须先后执行的匹配逻辑。
        try:
            await graph_client.rename_term_node(
                tenant_id=tenant_id, node_key=node_key, new_standard_name=payload.standard_name
            )
        except Exception:
            logger.exception(
                "术语 %r 重命名为 %r（租户 %r）已写入 SQLite 但图谱改名失败——两侧数据已不一致，需要人工核对",
                standard_name, payload.standard_name, tenant_id,
            )
            raise
    term = Term(
        tenant_id=tenant_id,
        node_key=node_key,
        standard_name=payload.standard_name,
        aliases=payload.aliases,
        term_type=payload.term_type,
        extra_properties=payload.extra_properties,
        source=existing_before_update.source,
    )
    try:
        await graph_client.sync_term(term)
    except Exception:
        logger.exception(
            "术语 %r（租户 %r）已写入 SQLite 但同步进图谱失败——两侧数据已不一致，需要人工核对",
            term.standard_name, tenant_id,
        )
        raise
    return _to_response(term)


@router.delete("/{standard_name}")
async def delete_existing_term(
    tenant_id: str,
    standard_name: str,
    term_type: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> dict[str, bool]:
    try:
        await require_active_tenant(review_conn, tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    # 先确认术语本身存在——404 的优先级要在 409 之前：一个根本不存在的
    # 名字不该因为图谱里凑巧有同名孤儿边就返回"已在图谱中使用"这种
    # 误导性的错误。确认存在之后再查图谱：这个术语已经被真实关系边
    # 使用的话拒绝删除，避免"词表说不存在了，但图谱边还在用它"的不
    # 一致状态——这一步必须在 delete_term() 之前，不能删完 SQLite 记录
    # 才发现图谱不允许删。
    try:
        term = await get_term(review_conn, tenant_id, standard_name, term_type)
    except TermNotFoundError:
        raise HTTPException(status_code=404, detail="术语不存在")
    edge_count = await graph_client.count_relation_edges_for_term(
        tenant_id=tenant_id, node_key=term.node_key
    )
    if edge_count > 0:
        raise HTTPException(status_code=409, detail="该术语已在图谱中使用，无法删除")
    await delete_term(review_conn, tenant_id, standard_name, term_type)
    try:
        await graph_client.delete_term_node(tenant_id=tenant_id, node_key=term.node_key)
    except Exception:
        logger.exception(
            "术语 %r（租户 %r）已从 SQLite 删除，但图谱节点删除失败——SQLite 记录已不存在，"
            "图谱节点仍然存在且对管理后台不可见，需要人工核对",
            standard_name, tenant_id,
        )
        raise
    return {"deleted": True}
