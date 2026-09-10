from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import aiosqlite

from app.api import deps
from app.api.admin_session import AdminSession
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
from app.api.bulk_delete import (
    BulkDeleteBlocked,
    BulkDeleteResult,
    run_bulk_delete,
)
from app.graphrag.ontology_constraints import (
    UnknownCategoryError as ConstraintUnknownCategoryError,
    UnknownRelationTypeError,
    add_allowed_combination,
    combination_object_id,
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
    #: 显示名，可以是中文。允许缺省：既有的直连调用方不带这个键，缺省时
    #: 前端按内部名显示（见 ExtraFieldSpec.display_name）。
    label: str = ""


class TermTypeWriteRequest(BaseModel):
    value: str
    extra_fields: list[ExtraFieldSpecRequest] = []
    standard_name_value_type: str = "string"


def _to_extra_field_specs(items: list[ExtraFieldSpecRequest]) -> list[ExtraFieldSpec]:
    return [
        ExtraFieldSpec(name=item.name, value_type=item.value_type, label=item.label)
        for item in items
    ]


def _extra_field_spec_to_dict(spec: ExtraFieldSpec) -> dict:
    # label 原样回传（可能是空串），不在这里替调用方回退到 name：接口的
    # 职责是如实报出存了什么，回退是显示层的事（前端 fieldDisplayName）。
    # 在这里回退的话，前端的显示名输入框会被预填成内部名，用户看不出这个
    # 字段其实还没起显示名。
    return {"name": spec.name, "value_type": spec.value_type, "label": spec.label}


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
    session: AdminSession = Depends(deps.require_admin_session),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    extra_field_specs = _to_extra_field_specs(payload.extra_fields)
    try:
        await create_term_type(
            review_conn, tenant_id, value=payload.value,
            extra_fields=extra_field_specs,
            standard_name_value_type=payload.standard_name_value_type,
            actor=session.username,
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
    session: AdminSession = Depends(deps.require_admin_session),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    extra_field_specs = _to_extra_field_specs(payload.extra_fields)
    try:
        await update_term_type(
            review_conn, tenant_id, value=value, new_value=payload.value,
            extra_fields=extra_field_specs,
            standard_name_value_type=payload.standard_name_value_type,
            actor=session.username,
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
    tenant_id: str, value: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    session: AdminSession = Depends(deps.require_admin_session),
) -> Response:
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        await delete_term_type(review_conn, tenant_id, value, actor=session.username)
    except CategoryInUseError as exc:
        # 不用 HTTPException：它只能放一个 detail。detail 保持是一句人话
        # （既有前端和测试直接展示它），旁边再挂一份结构化的挡路术语，前端
        # 据此生成"去实体列表按这个类型筛出来"的链接——光有一句"仍被 1 条
        # 术语引用"，用户看得见却纠正不了，只能自己去列表里翻。
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "blocking_terms": {
                    "term_type": exc.term_type,
                    "total": exc.terms_count,
                    "node_keys": exc.blocking_term_node_keys,
                },
                "blocking_constraints_total": exc.allowlist_count,
            },
        )
    return JSONResponse(status_code=200, content={"deleted": True})


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
    session: AdminSession = Depends(deps.require_admin_session),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        await create_relation_type(
            review_conn, tenant_id, relation_type=payload.relation_type,
            example_phrase=payload.example_phrase, description=payload.description,
            allow_chain_query=payload.allow_chain_query, actor=session.username,
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
    session: AdminSession = Depends(deps.require_admin_session),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        await update_relation_type(
            review_conn, tenant_id, relation_type=relation_type,
            new_relation_type=payload.relation_type,
            example_phrase=payload.example_phrase, description=payload.description,
            allow_chain_query=payload.allow_chain_query, actor=session.username,
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
    session: AdminSession = Depends(deps.require_admin_session),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    await delete_relation_type(review_conn, tenant_id, relation_type, actor=session.username)
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
    session: AdminSession = Depends(deps.require_admin_session),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        await add_allowed_combination(
            review_conn, tenant_id, subject_term_type=payload.subject_term_type,
            relation_type=payload.relation_type, object_term_type=payload.object_term_type,
            actor=session.username,
        )
    except (ConstraintUnknownCategoryError, UnknownRelationTypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return payload.model_dump()


@router.delete("/{tenant_id}/constraints")
async def remove_tenant_constraint(
    tenant_id: str, payload: ConstraintWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    session: AdminSession = Depends(deps.require_admin_session),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    await remove_allowed_combination(
        review_conn, tenant_id, subject_term_type=payload.subject_term_type,
        relation_type=payload.relation_type, object_term_type=payload.object_term_type,
        actor=session.username,
    )
    return {"deleted": True}


# ---------------------------------------------------------------------------
# 批量删除（本体结构页的三张表）
#
# 跟实体明细页的批量删除共用 app/api/bulk_delete.py 的执行语义——能删的删掉、
# 挡住的逐条报出来、不整批回滚——但**只有一种请求模式**：这三张表都是一次性
# 全量渲染，没有分页也没有筛选，"选中的这些"和"筛选条件下的全部"在这里是
# 同一件事。所以请求体里直接给要删的那些，不走 resolve_bulk_delete_mode
# （那个函数是用来在两种模式之间做互斥判定的，这里没有第二种模式可判）。
#
# 三个端点都逐条复用各自的单条删除函数，不另写一条批量 SQL：守卫、草稿
# status 范围、以及每删掉一条写一行 ontology_change_log，都由那些函数负责，
# 绕过去就意味着批量删除和单条删除会慢慢长成两套规矩。
#
# "这一条已经不在了"算失败而不算删掉：批次里混着别人刚删掉的行时，把它计入
# deleted 会让用户以为自己删掉了一个其实早就不在的东西。
# ---------------------------------------------------------------------------

_TERM_TYPE_GONE = "实体类型不存在，可能已经被别人删掉了"
_RELATION_TYPE_GONE = "关系类型不存在，可能已经被别人删掉了"
_CONSTRAINT_GONE = "这条约束不存在，可能已经被别人删掉了"


class BulkDeleteTermTypesRequest(BaseModel):
    values: list[str]


class BulkDeleteRelationTypesRequest(BaseModel):
    relation_types: list[str]


class BulkDeleteConstraintsRequest(BaseModel):
    constraints: list[ConstraintWriteRequest]


@router.post("/{tenant_id}/term-types/bulk-delete")
async def bulk_delete_term_type_categories(
    tenant_id: str,
    payload: BulkDeleteTermTypesRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    session: AdminSession = Depends(deps.require_admin_session),
) -> BulkDeleteResult:
    await require_active_tenant_or_404(review_conn, tenant_id)

    async def delete_one(value: str) -> None:
        # 存在性每条现查而不是开头查一次存成集合：批次里出现两次同一个值时，
        # 快照会让第二次删到空气还照样记一行审计。这三张表都是十几行的量级，
        # 多查几次的代价可以忽略。
        draft_values = {t.value for t in await list_term_types(review_conn, tenant_id, status="draft")}
        if value not in draft_values:
            raise BulkDeleteBlocked(_TERM_TYPE_GONE)
        try:
            await delete_term_type(review_conn, tenant_id, value, actor=session.username)
        except CategoryInUseError as exc:
            # 原样透传单条删除的那句话：它点名了挡路的是哪几条术语/哪几条
            # 约束，换成"删除失败"用户就只剩自己去列表里翻这一条路。
            raise BulkDeleteBlocked(str(exc)) from exc

    return await run_bulk_delete(payload.values, delete_one)


@router.post("/{tenant_id}/relation-types/bulk-delete")
async def bulk_delete_tenant_relation_types(
    tenant_id: str,
    payload: BulkDeleteRelationTypesRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    session: AdminSession = Depends(deps.require_admin_session),
) -> BulkDeleteResult:
    await require_active_tenant_or_404(review_conn, tenant_id)

    async def delete_one(relation_type: str) -> None:
        draft_types = {
            r.relation_type
            for r in await list_relation_types(review_conn, tenant_id, status="draft")
        }
        if relation_type not in draft_types:
            raise BulkDeleteBlocked(_RELATION_TYPE_GONE)
        await delete_relation_type(
            review_conn, tenant_id, relation_type, actor=session.username
        )

    return await run_bulk_delete(payload.relation_types, delete_one)


@router.post("/{tenant_id}/constraints/bulk-delete")
async def bulk_delete_tenant_constraints(
    tenant_id: str,
    payload: BulkDeleteConstraintsRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    session: AdminSession = Depends(deps.require_admin_session),
) -> BulkDeleteResult:
    """约束没有单列的主键，请求体里给的是三元组，失败明细的 key 是
    "主语 -关系-> 宾语"——跟变更日志的 object_id 同一种写法。"""
    await require_active_tenant_or_404(review_conn, tenant_id)
    wanted = {
        combination_object_id(c.subject_term_type, c.relation_type, c.object_term_type): c
        for c in payload.constraints
    }

    async def delete_one(key: str) -> None:
        item = wanted[key]
        existing = {
            combination_object_id(c.subject_term_type, c.relation_type, c.object_term_type)
            for c in await list_allowed_combinations(review_conn, tenant_id, status="draft")
        }
        if key not in existing:
            raise BulkDeleteBlocked(_CONSTRAINT_GONE)
        await remove_allowed_combination(
            review_conn, tenant_id, subject_term_type=item.subject_term_type,
            relation_type=item.relation_type, object_term_type=item.object_term_type,
            actor=session.username,
        )

    return await run_bulk_delete(list(wanted), delete_one)


@router.post("/{tenant_id}/checkout")
async def checkout_tenant_ontology_draft(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    await checkout_draft(review_conn, tenant_id)
    return {"checked_out": True}


async def _assert_new_date_fields_have_clean_values(
    review_conn: aiosqlite.Connection, graph_client: GraphWriteProtocol, *, tenant_id: str,
) -> None:
    """确认之前，挡住「字段刚变成 date、但图里还躺着非 ISO 值」这种情况。

    图里的日期是字符串属性，范围过滤靠字典序。如果一个字段刚被改成 date
    类型，但图里还躺着 "2026/1/15" 这类没归一的值，这些实体在按时间过滤
    时会被静默漏掉——字典序把它们排到了十月之后，而没有任何地方会报错。

    只查**这次从非 date 变成 date** 的字段：
    - 没变的不查，否则每次确认本体都要扫一遍全图；
    - 已确认版本里不存在的 term_type 不查，那是新建的类型，图里一个节点
      都没有，扫它是白扫。

    这是「查询开始照着新类型跑」的唯一分界点：update_term_type 写的是草稿，
    只有 confirm 会把它提升成 confirmed。
    """
    draft = {t.value: t for t in await list_term_types(review_conn, tenant_id, status="draft")}
    if not draft:
        # confirm_ontology 对空草稿是 no-op（幂等），这里同样直接放行。
        return
    confirmed = {
        t.value: t for t in await list_term_types(review_conn, tenant_id, status="confirmed")
    }
    problems: list[str] = []
    for value, term_type in draft.items():
        previous = confirmed.get(value)
        if previous is None:
            continue
        was_date = {f.name for f in previous.extra_fields if f.value_type == "date"}
        for spec in term_type.extra_fields:
            if spec.value_type != "date" or spec.name in was_date:
                continue
            count, samples = await graph_client.count_non_iso_date_values(
                tenant_id=tenant_id, term_type=value, field=spec.name,
            )
            if count:
                sample_text = "、".join(repr(s) for s in samples)
                problems.append(
                    f"{value}.{spec.name} 还有 {count} 个实体的值不是 YYYY-MM-DD，"
                    f"例如 {sample_text}"
                )
    if problems:
        raise HTTPException(
            status_code=409,
            detail="；".join(problems)
            + "。改成日期类型之前先重新导入这份数据，否则这些实体在按时间过滤时会被静默漏掉。",
        )


@router.post("/{tenant_id}/confirm")
async def confirm_tenant_ontology(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: GraphWriteProtocol = Depends(deps.get_graph_client),
    session: AdminSession = Depends(deps.require_admin_session),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    await _assert_new_date_fields_have_clean_values(
        review_conn, graph_client, tenant_id=tenant_id,
    )
    await confirm_ontology(review_conn, tenant_id, actor=session.username)
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
    session: AdminSession = Depends(deps.require_admin_session),
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
            actor=session.username,
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
