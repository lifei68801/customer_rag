from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import aiosqlite

from app.api import deps
from app.api.tenant_guard import require_active_tenant_or_404
from app.graphrag.ontology_categories import (
    CategoryInUseError,
    CategoryNameConflictError,
    CategoryNotFoundError,
    ExtraFieldSpec,
    InvalidExtraFieldTypeError,
    create_term_type,
    delete_term_type,
    list_term_types,
    update_term_type,
)
from app.graphrag.ontology_constraints import (
    UnknownCategoryError as ConstraintUnknownCategoryError,
    UnknownRelationTypeError,
    add_allowed_combination,
    list_allowed_combinations,
    remove_allowed_combination,
)
from app.graphrag.ontology_etl_mapping import get_etl_mapping
from app.graphrag.ontology_lifecycle import (
    checkout_draft,
    confirm_ontology,
    is_ontology_confirmed,
    replace_draft,
)
from app.graphrag.ontology_relations import (
    InvalidRelationTypeNameError,
    RelationTypeNameConflictError,
    RelationTypeNotFoundError,
    create_relation_type,
    delete_relation_type,
    list_relation_types,
    update_relation_type,
)
from app.graphrag.neo4j_client import GraphWriteProtocol
from app.graphrag.terms_store import migrate_term_type
from app.graphrag.terms_store import count_terms_by_term_type

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/ontology", dependencies=[Depends(deps.require_admin_session)])


class ExtraFieldSpecRequest(BaseModel):
    name: str
    value_type: str


class TermTypeWriteRequest(BaseModel):
    value: str
    extra_fields: list[ExtraFieldSpecRequest] = []
    standard_name_value_type: str = "string"


def _to_extra_field_specs(items: list[ExtraFieldSpecRequest]) -> list[ExtraFieldSpec]:
    return [ExtraFieldSpec(name=item.name, value_type=item.value_type) for item in items]


def _extra_field_spec_to_dict(spec: ExtraFieldSpec) -> dict:
    return {"name": spec.name, "value_type": spec.value_type}


@router.get("/{tenant_id}/term-types")
async def list_term_type_categories(
    tenant_id: str,
    status: str = "draft",
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    result = await list_term_types(review_conn, tenant_id, status=status)
    return {
        "term_types": [
            {
                "value": t.value,
                "extra_fields": [_extra_field_spec_to_dict(f) for f in t.extra_fields],
                "standard_name_value_type": t.standard_name_value_type,
            }
            for t in result
        ]
    }


@router.post("/{tenant_id}/term-types")
async def create_term_type_category(
    tenant_id: str,
    payload: TermTypeWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: GraphWriteProtocol = Depends(deps.get_graph_client),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    extra_field_specs = _to_extra_field_specs(payload.extra_fields)
    try:
        await create_term_type(
            review_conn, tenant_id, value=payload.value,
            extra_fields=extra_field_specs,
            standard_name_value_type=payload.standard_name_value_type,
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
    graph_client: GraphWriteProtocol = Depends(deps.get_graph_client),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    extra_field_specs = _to_extra_field_specs(payload.extra_fields)
    try:
        await update_term_type(
            review_conn, tenant_id, value=value, new_value=payload.value,
            extra_fields=extra_field_specs,
            standard_name_value_type=payload.standard_name_value_type,
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
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        await delete_term_type(review_conn, tenant_id, value)
    except CategoryInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"deleted": True}


class MigrateTermTypeRequest(BaseModel):
    old_type: str
    new_type: str


class MigrateTermTypeResponse(BaseModel):
    terms_migrated: int
    graph_nodes_migrated: int


@router.post("/{tenant_id}/term-types/migrate")
async def migrate_tenant_term_type(
    tenant_id: str,
    payload: MigrateTermTypeRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: GraphWriteProtocol = Depends(deps.get_graph_client),
) -> MigrateTermTypeResponse:
    await require_active_tenant_or_404(review_conn, tenant_id)
    terms_migrated = await migrate_term_type(
        review_conn, tenant_id, old_type=payload.old_type, new_type=payload.new_type
    )
    try:
        graph_nodes_migrated = await graph_client.migrate_term_type_nodes(
            tenant_id=tenant_id, old_type=payload.old_type, new_type=payload.new_type
        )
    except Exception:
        logger.exception(
            "term_type %r 迁移到 %r（租户 %r）已写入 SQLite（%d 条术语已迁移）但同步到图谱失败——"
            "两侧数据已不一致，需要人工核对；Neo4j 一侧的迁移操作是幂等的，可安全重试",
            payload.old_type, payload.new_type, tenant_id, terms_migrated,
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"实体类型已在 SQLite 中迁移（terms_migrated={terms_migrated}），"
                "但同步到 Neo4j 图谱失败，请检查 Neo4j 连通性后重试（该操作幂等，可安全重试）"
            ),
        )
    return MigrateTermTypeResponse(
        terms_migrated=terms_migrated, graph_nodes_migrated=graph_nodes_migrated
    )


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
    await require_active_tenant_or_404(review_conn, tenant_id)
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
    await require_active_tenant_or_404(review_conn, tenant_id)
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
    await require_active_tenant_or_404(review_conn, tenant_id)
    await delete_relation_type(review_conn, tenant_id, relation_type)
    return {"deleted": True}


@router.post("/{tenant_id}/relation-types/migrate")
async def migrate_tenant_relation_type(
    tenant_id: str, payload: MigrateRelationTypeRequest,
    graph_client: GraphWriteProtocol = Depends(deps.get_graph_client),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        count = await graph_client.migrate_relation_type_edges(
            tenant_id=tenant_id, old_type=payload.old_type, new_type=payload.new_type
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"migrated_count": count}


@router.get("/{tenant_id}/graph-overlay")
async def load_tenant_graph_overlay(
    tenant_id: str, status: str = "draft",
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: GraphWriteProtocol = Depends(deps.get_graph_client),
) -> dict:
    """本体图的叠加信息：每条约束的真实扇出度 + 每个实体类型的实体数量。

    两者合在一个接口里是因为它们都只服务本体图这一个消费方，而且都要在
    切到图视图时一次性取到——分成两个接口只是多一次往返。

    ---

    扇出：按已声明的约束逐条探测**真实数据**里的扇出度。

    为什么单独开这个接口：约束表只说明"这个组合被允许"，说不出实际数据里
    一个主语节点会连到几个宾语节点。而后者才是扇形陷阱的判据——沿一条
    1:N 的边做计数聚合会把归属放大（订单→产品→公司 这条两跳路径上，
    产品→公司 是 1:N，于是每笔订单都会通向全部 3 家公司，"某公司有多少
    订单"因此恒等于订单总数）。

    本体层看不出这件事：本体只声明了一条 `产品 SOLD_BY 公司`，是不是
    一对多要问图谱。

    逐条查询，约束数量通常是个位数到几十条（demo 是 5 条），不做批量优化。
    单条探测失败不中断整体——图谱可能正在重建、某个类型还没有任何节点，
    这时该退回"未知"而不是让整个视图报错。
    """
    entity_counts = await count_terms_by_term_type(review_conn, tenant_id)
    combinations = await list_allowed_combinations(review_conn, tenant_id, status=status)
    fanout: list[dict] = []
    for c in combinations:
        try:
            value = await graph_client.probe_relation_fanout(
                tenant_id=tenant_id,
                relation_type=c.relation_type,
                from_term_type=c.subject_term_type,
                to_term_type=c.object_term_type,
                direction="outgoing",
            )
        except Exception:
            logger.exception(
                "探测扇出失败：tenant=%r %s -%s-> %s",
                tenant_id, c.subject_term_type, c.relation_type, c.object_term_type,
            )
            value = None
        fanout.append(
            {
                "subject_term_type": c.subject_term_type,
                "relation_type": c.relation_type,
                "object_term_type": c.object_term_type,
                "fanout": value,
            }
        )
    return {"fanout": fanout, "entity_counts": entity_counts}


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
    await require_active_tenant_or_404(review_conn, tenant_id)
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
    await require_active_tenant_or_404(review_conn, tenant_id)
    await remove_allowed_combination(
        review_conn, tenant_id, subject_term_type=payload.subject_term_type,
        relation_type=payload.relation_type, object_term_type=payload.object_term_type,
    )
    return {"deleted": True}


@router.post("/{tenant_id}/checkout")
async def checkout_tenant_ontology_draft(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    await checkout_draft(review_conn, tenant_id)
    return {"checked_out": True}


@router.post("/{tenant_id}/confirm")
async def confirm_tenant_ontology(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    await confirm_ontology(review_conn, tenant_id)
    return {"confirmed": True}


@router.get("/{tenant_id}/status")
async def get_tenant_ontology_status(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    return {"confirmed": await is_ontology_confirmed(review_conn, tenant_id)}


class DraftTermTypePayload(BaseModel):
    value: str
    extra_fields: list[dict] = []
    standard_name_value_type: str = "string"


class DraftRelationTypePayload(BaseModel):
    relation_type: str
    example_phrase: str = ""
    description: str = ""
    allow_chain_query: bool = True


class DraftConstraintPayload(BaseModel):
    subject_term_type: str
    relation_type: str
    object_term_type: str


class DraftEtlMappingPayload(BaseModel):
    config_yaml: str
    source_file_name: str


class ReplaceDraftRequest(BaseModel):
    term_types: list[DraftTermTypePayload]
    relation_types: list[DraftRelationTypePayload]
    constraints: list[DraftConstraintPayload]
    etl_mapping: DraftEtlMappingPayload | None = None


@router.post("/{tenant_id}/draft/replace")
async def replace_ontology_draft(
    tenant_id: str,
    payload: ReplaceDraftRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    """整份替换草稿。引导页用它一次写入整套本体。

    没有对应的"增量"端点：引导每次提交的都是完整草案，增量合并会让用户
    删掉的东西留在库里。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        await replace_draft(
            review_conn,
            tenant_id,
            term_types=[t.model_dump() for t in payload.term_types],
            relation_types=[r.model_dump() for r in payload.relation_types],
            constraints=[c.model_dump() for c in payload.constraints],
            etl_mapping=payload.etl_mapping.model_dump() if payload.etl_mapping else None,
        )
    # replace_draft 内部除了引用未声明类型的 ConstraintUnknownCategoryError，
    # 还会对 extra_fields / standard_name_value_type / relation_type 做跟单条
    # 创建接口同样的格式校验，抛的是 InvalidExtraFieldTypeError /
    # InvalidRelationTypeNameError——这两个不是 ValueError 的子类，brief 示例
    # 里只捕获 (UnknownCategoryError, ValueError) 会漏掉它们，导致校验失败时
    # 变成裸 500 而不是 400。这里比示例多捕获这两个类型，跟本文件其它端点
    # （create_term_type_category / create_tenant_relation_type）的错误映射
    # 保持一致。
    except (
        ConstraintUnknownCategoryError,
        InvalidExtraFieldTypeError,
        InvalidRelationTypeNameError,
        ValueError,
    ) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"replaced": True}


@router.get("/{tenant_id}/etl-mapping")
async def get_ontology_etl_mapping(
    tenant_id: str,
    status: str = "draft",
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    """读该租户挂在本体上的 ETL 映射。表格导入页用它决定首屏形态。"""
    await require_active_tenant_or_404(review_conn, tenant_id)
    if status not in ("draft", "confirmed"):
        raise HTTPException(status_code=400, detail="status 只能是 draft 或 confirmed")
    mapping = await get_etl_mapping(review_conn, tenant_id, status=status)
    if mapping is None:
        return {"mapping": None}
    return {
        "mapping": {
            "config_yaml": mapping.config_yaml,
            "source_file_name": mapping.source_file_name,
            "created_at": mapping.created_at,
        }
    }
