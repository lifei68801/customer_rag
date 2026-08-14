from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import aiosqlite

from app.api import deps
from app.graphrag.ontology_categories import (
    CategoryInUseError,
    CategoryNameConflictError,
    CategoryNotFoundError,
    create_product_line,
    create_term_type,
    delete_product_line,
    delete_term_type,
    list_product_lines,
    list_term_types,
    update_product_line,
    update_term_type,
)
from app.graphrag.ontology_constraints import (
    UnknownCategoryError as ConstraintUnknownCategoryError,
    UnknownRelationTypeError,
    add_allowed_combination,
    list_allowed_combinations,
    remove_allowed_combination,
)
from app.graphrag.ontology_lifecycle import checkout_draft, confirm_ontology, is_ontology_confirmed
from app.graphrag.ontology_relations import (
    InvalidRelationTypeNameError,
    RelationTypeNotFoundError,
    create_relation_type,
    delete_relation_type,
    list_relation_types,
    update_relation_type,
)
from app.graphrag.neo4j_client import Neo4jGraphClient

router = APIRouter(prefix="/api/admin/ontology", dependencies=[Depends(deps.require_admin_session)])


class TermTypeWriteRequest(BaseModel):
    value: str
    extra_fields: list[str] = []


class ProductLineWriteRequest(BaseModel):
    value: str


@router.get("/term-types")
async def list_term_type_categories(
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    result = await list_term_types(review_conn)
    return {"term_types": [{"value": t.value, "extra_fields": t.extra_fields} for t in result]}


@router.post("/term-types")
async def create_term_type_category(
    payload: TermTypeWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await create_term_type(review_conn, value=payload.value, extra_fields=payload.extra_fields)
    except CategoryNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"value": payload.value, "extra_fields": payload.extra_fields}


@router.put("/term-types/{value}")
async def update_term_type_category(
    value: str,
    payload: TermTypeWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await update_term_type(
            review_conn, value=value, new_value=payload.value, extra_fields=payload.extra_fields
        )
    except CategoryNotFoundError:
        raise HTTPException(status_code=404, detail="分类不存在")
    except CategoryNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"value": payload.value, "extra_fields": payload.extra_fields}


@router.delete("/term-types/{value}")
async def delete_term_type_category(
    value: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> dict:
    try:
        await delete_term_type(review_conn, value)
    except CategoryInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"deleted": True}


@router.get("/product-lines")
async def list_product_line_categories(
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    return {"product_lines": await list_product_lines(review_conn)}


@router.post("/product-lines")
async def create_product_line_category(
    payload: ProductLineWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await create_product_line(review_conn, value=payload.value)
    except CategoryNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"value": payload.value}


@router.put("/product-lines/{value}")
async def update_product_line_category(
    value: str,
    payload: ProductLineWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await update_product_line(review_conn, value=value, new_value=payload.value)
    except CategoryNotFoundError:
        raise HTTPException(status_code=404, detail="产品线不存在")
    except CategoryNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"value": payload.value}


@router.delete("/product-lines/{value}")
async def delete_product_line_category(
    value: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> dict:
    try:
        await delete_product_line(review_conn, value)
    except CategoryInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"deleted": True}
