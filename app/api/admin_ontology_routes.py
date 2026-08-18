from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import aiosqlite

from app.api import deps
from app.graphrag.ontology_categories import (
    CategoryInUseError,
    CategoryNameConflictError,
    CategoryNotFoundError,
    ExtraFieldSpec,
    InvalidExtraFieldTypeError,
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
    RelationTypeNameConflictError,
    RelationTypeNotFoundError,
    create_relation_type,
    delete_relation_type,
    list_relation_types,
    update_relation_type,
)
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.tenants_store import TenantNotFoundError, require_active_tenant

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/ontology", dependencies=[Depends(deps.require_admin_session)])


class ExtraFieldSpecRequest(BaseModel):
    name: str
    value_type: str


class TermTypeWriteRequest(BaseModel):
    value: str
    extra_fields: list[ExtraFieldSpecRequest] = []


def _to_extra_field_specs(items: list[ExtraFieldSpecRequest]) -> list[ExtraFieldSpec]:
    return [ExtraFieldSpec(name=item.name, value_type=item.value_type) for item in items]


def _extra_field_spec_to_dict(spec: ExtraFieldSpec) -> dict:
    return {"name": spec.name, "value_type": spec.value_type}


class ProductLineWriteRequest(BaseModel):
    value: str


@router.get("/{tenant_id}/term-types")
async def list_term_type_categories(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    result = await list_term_types(review_conn, tenant_id)
    return {
        "term_types": [
            {
                "value": t.value,
                "extra_fields": [_extra_field_spec_to_dict(f) for f in t.extra_fields],
            }
            for t in result
        ]
    }


@router.post("/{tenant_id}/term-types")
async def create_term_type_category(
    tenant_id: str,
    payload: TermTypeWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> dict:
    try:
        await require_active_tenant(review_conn, tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    extra_field_specs = _to_extra_field_specs(payload.extra_fields)
    try:
        await create_term_type(
            review_conn, tenant_id, value=payload.value,
            extra_fields=extra_field_specs,
        )
    except CategoryNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except InvalidExtraFieldTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # SQLite 侧的分类声明此时已经成功提交——Neo4j 索引只是查询性能优化，不是
    # 正确性前提，失败不能反向把这个已成功的声明变成 500：客户端看到 500 后
    # 天然会重试，而重试会撞上（已经写成功的）SQLite 记录报 400"已存在"，
    # 把一次可恢复的性能降级放大成一个看起来无解的死循环。
    try:
        await graph_client.ensure_extra_field_indexes(
            tenant_id=tenant_id, term_type=payload.value, extra_fields=extra_field_specs,
        )
    except Exception:
        logger.exception(
            "term_type %r（租户 %r）的 SQLite 声明已成功，但 Neo4j 索引创建失败——"
            "查询性能会受影响，不阻塞声明本身，需要人工核查 Neo4j 连通性",
            payload.value, tenant_id,
        )
    return payload.model_dump()


@router.put("/{tenant_id}/term-types/{value}")
async def update_term_type_category(
    tenant_id: str,
    value: str,
    payload: TermTypeWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> dict:
    try:
        await require_active_tenant(review_conn, tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    extra_field_specs = _to_extra_field_specs(payload.extra_fields)
    try:
        await update_term_type(
            review_conn, tenant_id, value=value, new_value=payload.value,
            extra_fields=extra_field_specs,
        )
    except CategoryNotFoundError:
        raise HTTPException(status_code=404, detail="分类不存在")
    except CategoryNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except InvalidExtraFieldTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # 同 create_term_type_category：索引创建失败不阻塞已经成功的 SQLite 更新。
    try:
        await graph_client.ensure_extra_field_indexes(
            tenant_id=tenant_id, term_type=payload.value, extra_fields=extra_field_specs,
        )
    except Exception:
        logger.exception(
            "term_type %r（租户 %r）的 SQLite 声明已成功，但 Neo4j 索引创建失败——"
            "查询性能会受影响，不阻塞声明本身，需要人工核查 Neo4j 连通性",
            payload.value, tenant_id,
        )
    return payload.model_dump()


@router.delete("/{tenant_id}/term-types/{value}")
async def delete_term_type_category(
    tenant_id: str, value: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> dict:
    try:
        await require_active_tenant(review_conn, tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    try:
        await delete_term_type(review_conn, tenant_id, value)
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


class RelationTypeWriteRequest(BaseModel):
    relation_type: str
    example_phrase: str
    description: str = ""
    allow_chain_query: bool = False


class ConstraintWriteRequest(BaseModel):
    subject_term_type: str
    relation_type: str
    object_term_type: str


class MigrateRelationTypeRequest(BaseModel):
    old_type: str
    new_type: str


def _relation_type_to_dict(item) -> dict:
    return {
        "relation_type": item.relation_type,
        "example_phrase": item.example_phrase,
        "description": item.description,
        "allow_chain_query": item.allow_chain_query,
        "source": item.source,
    }


@router.get("/{tenant_id}/relation-types")
async def list_tenant_relation_types(
    tenant_id: str, status: str = "draft",
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    result = await list_relation_types(review_conn, tenant_id, status=status)
    return {"relation_types": [_relation_type_to_dict(r) for r in result]}


@router.post("/{tenant_id}/relation-types")
async def create_tenant_relation_type(
    tenant_id: str, payload: RelationTypeWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await require_active_tenant(review_conn, tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    try:
        await create_relation_type(
            review_conn, tenant_id, relation_type=payload.relation_type,
            example_phrase=payload.example_phrase, description=payload.description,
            allow_chain_query=payload.allow_chain_query,
        )
    except InvalidRelationTypeNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RelationTypeNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return payload.model_dump()


@router.put("/{tenant_id}/relation-types/{relation_type}")
async def update_tenant_relation_type(
    tenant_id: str, relation_type: str, payload: RelationTypeWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await require_active_tenant(review_conn, tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    try:
        await update_relation_type(
            review_conn, tenant_id, relation_type=relation_type,
            new_relation_type=payload.relation_type,
            example_phrase=payload.example_phrase, description=payload.description,
            allow_chain_query=payload.allow_chain_query,
        )
    except InvalidRelationTypeNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RelationTypeNotFoundError:
        raise HTTPException(status_code=404, detail="关系类型不存在")
    except RelationTypeNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return payload.model_dump()


@router.delete("/{tenant_id}/relation-types/{relation_type}")
async def delete_tenant_relation_type(
    tenant_id: str, relation_type: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await require_active_tenant(review_conn, tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    await delete_relation_type(review_conn, tenant_id, relation_type)
    return {"deleted": True}


@router.post("/{tenant_id}/relation-types/migrate")
async def migrate_tenant_relation_type(
    tenant_id: str, payload: MigrateRelationTypeRequest,
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await require_active_tenant(review_conn, tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    try:
        count = await graph_client.migrate_relation_type_edges(
            tenant_id=tenant_id, old_type=payload.old_type, new_type=payload.new_type
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"migrated_count": count}


@router.get("/{tenant_id}/constraints")
async def list_tenant_constraints(
    tenant_id: str, status: str = "draft",
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    result = await list_allowed_combinations(review_conn, tenant_id, status=status)
    return {
        "constraints": [
            {
                "subject_term_type": c.subject_term_type,
                "relation_type": c.relation_type,
                "object_term_type": c.object_term_type,
            }
            for c in result
        ]
    }


@router.post("/{tenant_id}/constraints")
async def add_tenant_constraint(
    tenant_id: str, payload: ConstraintWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await require_active_tenant(review_conn, tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    try:
        await add_allowed_combination(
            review_conn, tenant_id, subject_term_type=payload.subject_term_type,
            relation_type=payload.relation_type, object_term_type=payload.object_term_type,
        )
    except (ConstraintUnknownCategoryError, UnknownRelationTypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return payload.model_dump()


@router.delete("/{tenant_id}/constraints")
async def remove_tenant_constraint(
    tenant_id: str, payload: ConstraintWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await require_active_tenant(review_conn, tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    await remove_allowed_combination(
        review_conn, tenant_id, subject_term_type=payload.subject_term_type,
        relation_type=payload.relation_type, object_term_type=payload.object_term_type,
    )
    return {"deleted": True}


@router.post("/{tenant_id}/checkout")
async def checkout_tenant_ontology_draft(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await require_active_tenant(review_conn, tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    await checkout_draft(review_conn, tenant_id)
    return {"checked_out": True}


@router.post("/{tenant_id}/confirm")
async def confirm_tenant_ontology(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await require_active_tenant(review_conn, tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    await confirm_ontology(review_conn, tenant_id)
    return {"confirmed": True}


@router.get("/{tenant_id}/status")
async def get_tenant_ontology_status(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    return {"confirmed": await is_ontology_confirmed(review_conn, tenant_id)}
