from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

import aiosqlite

from app.api import deps
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.terms_store import (
    TermNameConflictError,
    TermNotFoundError,
    create_term,
    delete_term,
    get_term,
    list_terms,
    update_term,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/terms", dependencies=[Depends(deps.require_admin_session)])


class TermResponse(BaseModel):
    standard_name: str
    aliases: list[str]
    term_type: str
    product_line: str


class TermListResponse(BaseModel):
    terms: list[TermResponse]


class TermWriteRequest(BaseModel):
    standard_name: str
    aliases: list[str]
    term_type: str
    product_line: str

    @field_validator("standard_name")
    @classmethod
    def _validate_standard_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("standard_name 不能为空")
        if "/" in stripped:
            raise ValueError("standard_name 不能包含 /")
        return stripped

    @field_validator("term_type", "product_line")
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


def _to_response(term: Term) -> TermResponse:
    return TermResponse(
        standard_name=term.standard_name,
        aliases=term.aliases,
        term_type=term.term_type,
        product_line=term.product_line,
    )


@router.get("", response_model=TermListResponse)
async def list_all_terms(
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> TermListResponse:
    terms = await list_terms(review_conn)
    return TermListResponse(terms=[_to_response(term) for term in terms])


@router.post("", response_model=TermResponse)
async def create_new_term(
    payload: TermWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> TermResponse:
    try:
        await create_term(
            review_conn,
            standard_name=payload.standard_name,
            aliases=payload.aliases,
            term_type=payload.term_type,
            product_line=payload.product_line,
        )
    except TermNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    term = Term(
        standard_name=payload.standard_name,
        aliases=payload.aliases,
        term_type=payload.term_type,
        product_line=payload.product_line,
    )
    # 新增成功后立即同步进图谱（属性+别名节点），不留图谱异步落后的窗口。
    try:
        await graph_client.sync_term(term)
    except Exception:
        logger.exception(
            "术语 %r 已写入 SQLite 但同步进图谱失败——两侧数据已不一致，需要人工核对",
            term.standard_name,
        )
        raise
    return _to_response(term)


@router.put("/{standard_name}", response_model=TermResponse)
async def update_existing_term(
    standard_name: str,
    payload: TermWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> TermResponse:
    try:
        await update_term(
            review_conn,
            standard_name=standard_name,
            new_standard_name=payload.standard_name,
            aliases=payload.aliases,
            term_type=payload.term_type,
            product_line=payload.product_line,
        )
    except TermNotFoundError:
        raise HTTPException(status_code=404, detail="术语不存在")
    except TermNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if payload.standard_name != standard_name:
        # 改名：先对同一个图节点做属性级联更新（保留已有关系边），再用
        # sync_term 刷新 type/product_line/别名——顺序不能反过来，
        # sync_term 是按"当前"standard_name MERGE 匹配节点的。
        try:
            await graph_client.rename_term_node(
                old_name=standard_name, new_name=payload.standard_name
            )
        except Exception:
            logger.exception(
                "术语 %r 重命名为 %r 已写入 SQLite 但图谱改名失败——两侧数据已不一致，需要人工核对",
                standard_name,
                payload.standard_name,
            )
            raise
    term = Term(
        standard_name=payload.standard_name,
        aliases=payload.aliases,
        term_type=payload.term_type,
        product_line=payload.product_line,
    )
    try:
        await graph_client.sync_term(term)
    except Exception:
        logger.exception(
            "术语 %r 已写入 SQLite 但同步进图谱失败——两侧数据已不一致，需要人工核对",
            term.standard_name,
        )
        raise
    return _to_response(term)


@router.delete("/{standard_name}")
async def delete_existing_term(
    standard_name: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> dict[str, bool]:
    # 先确认术语本身存在——404 的优先级要在 409 之前：一个根本不存在的
    # 名字不该因为图谱里凑巧有同名孤儿边就返回"已在图谱中使用"这种
    # 误导性的错误。确认存在之后再查图谱：这个术语已经被真实关系边
    # 使用的话拒绝删除，避免"词表说不存在了，但图谱边还在用它"的不
    # 一致状态——这一步必须在 delete_term() 之前，不能删完 SQLite 记录
    # 才发现图谱不允许删。
    try:
        await get_term(review_conn, standard_name)
    except TermNotFoundError:
        raise HTTPException(status_code=404, detail="术语不存在")
    edge_count = await graph_client.count_relation_edges_for_term(standard_name)
    if edge_count > 0:
        raise HTTPException(status_code=409, detail="该术语已在图谱中使用，无法删除")
    await delete_term(review_conn, standard_name)
    try:
        await graph_client.delete_term_node(standard_name)
    except Exception:
        logger.exception(
            "术语 %r 已从 SQLite 删除，但图谱节点删除失败——SQLite 记录已不存在，"
            "图谱节点仍然存在且对管理后台不可见，需要人工核对",
            standard_name,
        )
        raise
    return {"deleted": True}
