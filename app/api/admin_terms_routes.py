from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

import aiosqlite

from app.api import deps
from app.api.tenant_guard import require_active_tenant_or_404
from app.graphrag.duplicate_detection import find_similar_terms
from app.graphrag.neo4j_client import GraphWriteProtocol
from app.graphrag.ontology import Term
from app.graphrag.term_edits_store import (
    FIELD_CREATED,
    FIELD_DELETED,
    FIELD_EXTRA_PROPERTIES,
    delete_term_edit,
    upsert_term_edit,
)
from app.graphrag.terms_store import (
    InvalidExtraPropertyTypeError,
    TermNotFoundError,
    UnknownCategoryError,
    count_terms_merged,
    get_term_by_node_key,
    get_term_merged_by_node_key,
    is_tombstoned,
    list_terms_merged,
    validate_term_categories,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/{tenant_id}/terms", dependencies=[Depends(deps.require_admin_session)])

# Task 4：这三个写入端点都在 require_admin_session 之下，但那个依赖只校验
# Authorization: Bearer <token> 是否是有效的管理员 session（app/api/deps.py），
# 不返回任何身份标识（返回值是 None）。本设计不做编辑历史/审计流水
# （term_edits 每个 (node_key, field) 只保留当前值，见
# docs/superpowers/specs/2026-08-30-manual-edits-layer-design.md 非目标），
# edited_by 目前只是可观测性字段，固定写 "admin"。
_EDITED_BY = "admin"


class TermResponse(BaseModel):
    node_key: str
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
    # None（字段缺席）和 {}（显式传空对象）在更新路径上语义不同：缺席表示
    # "这次请求不涉及属性值，保留原样"，空对象才是"把属性值清空"。PUT 的
    # 全量替换语义对 standard_name/aliases 是对的，但对属性值太危险——只提交
    # 名字和别名的编辑表单会静默抹掉整条术语的属性值，而 ETL 建模把度量列
    # （金额、数量、日期）放在属性字段里，那等于一次编辑丢掉一整行业务数据。
    # 新增路径上两者没有区别，都落成空属性。
    extra_properties: dict[str, Any] | None = None
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
        node_key=term.node_key,
        standard_name=term.standard_name,
        aliases=term.aliases,
        term_type=term.term_type,
        extra_properties=term.extra_properties,
        source=term.source,
        similar_terms=similar_terms,
    )


class TermRelation(BaseModel):
    direction: Literal["in", "out"]
    relation_type: str
    node_key: str
    standard_name: str
    term_type: str | None = None


class TermDetailResponse(TermResponse):
    #: None 表示关系拉取失败，[] 表示确实没有关系。两者必须分开：孤立实体
    #: 对检索基本无用，是个真实且重要的状态，不能跟「Neo4j 挂了」混为一谈。
    relations: list[TermRelation] | None = None


@router.get("/{node_key:path}", response_model=TermDetailResponse)
async def get_term_detail(
    tenant_id: str,
    node_key: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: GraphWriteProtocol = Depends(deps.get_graph_client),
) -> TermDetailResponse:
    """实体详情：属性 + 它在图谱里连着什么。

    关系这块是详情页存在的理由——一个实体有没有用，取决于它连着谁，而这
    在列表行里放不下。

    Neo4j 挂掉不让整页打不开：属性存在 SQLite 里，照样能看能改，关系那块
    单独标成拉取失败。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        term = await get_term_merged_by_node_key(review_conn, tenant_id, node_key)
    except TermNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    relations: list[TermRelation] | None
    try:
        rows = await graph_client.list_term_relations(tenant_id=tenant_id, node_key=node_key)
        relations = [TermRelation(**row) for row in rows]
    except Exception:
        logger.exception("读取实体 %r 的图谱关系失败", node_key)
        relations = None

    return TermDetailResponse(**_to_response(term).model_dump(), relations=relations)


@router.get("", response_model=TermListResponse)
async def list_all_terms(
    tenant_id: str,
    page: int | None = None,
    page_size: int | None = None,
    source: str | None = None,
    q: str | None = None,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> TermListResponse:
    # page/page_size 都不传（比如 termsApi.ts 里不分页的 fetchTerms()，
    # GraphReviewsPage.tsx 的标准名自动补全用它拉全量数据做前端过滤）时，
    # 不加 limit/offset 地调用 list_terms()——它自己的默认值就是"返回全部"，
    # 保持这个分页 query 参数引入之前的行为不变。只要任意一个参数被显式
    # 传入（管理后台自己的分页列表 fetchTermsPage() 两个参数总是一起传），
    # 才按分页语义处理。
    # q（搜索）走一条独立路径：**必须在合并视图上过滤，不能在 SQL 里过滤**。
    # 人工改过展示名的术语，如果按 terms 表的原始值搜，只能用旧名字找到、
    # 用界面上看到的新名字反而搜不到——正好反了。所以先取全量合并结果、
    # 在内存里过滤，再切分页。
    #
    # 代价是搜索时要把该租户的术语全量载入。可以接受：list_terms 本来就在
    # 别的路径上（agent 每轮消歧、摄取管线）以全量方式被调用，这不是新引入
    # 的量级。真成为瓶颈时再考虑把编辑层的展示名物化成可索引的列。
    if q is not None and q.strip():
        needle = q.strip().casefold()
        matched = [
            t
            for t in await list_terms_merged(review_conn, tenant_id, source=source)
            if needle in t.standard_name.casefold()
            or any(needle in alias.casefold() for alias in t.aliases)
        ]
        effective_page_size = page_size or 20
        offset = ((page or 1) - 1) * effective_page_size
        return TermListResponse(
            terms=[
                _to_response(term)
                for term in matched[offset : offset + effective_page_size]
            ],
            # 搜索路径下 total 是命中数，跟列表内容一致——不走下面那个
            # count_terms（它数的是 terms 原始表，见函数末尾的说明）。
            total=len(matched),
        )
    if page is None and page_size is None:
        terms = await list_terms_merged(review_conn, tenant_id, source=source)
    else:
        effective_page = page or 1
        effective_page_size = page_size or 20
        offset = (effective_page - 1) * effective_page_size
        terms = await list_terms_merged(
            review_conn, tenant_id, limit=effective_page_size, offset=offset, source=source
        )
    # 用合并视图的计数，不是 count_terms——后者数的是 terms 原始表，跟列表
    # 内容对不上（人工删除的仍在表里但不显示、纯编辑层创建的反之），分页器
    # 会撒谎。见 count_terms_merged 的说明。
    total = await count_terms_merged(review_conn, tenant_id, source=source)
    return TermListResponse(terms=[_to_response(term) for term in terms], total=total)


@router.post("", response_model=TermResponse)
async def create_new_term(
    tenant_id: str,
    payload: TermWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: GraphWriteProtocol = Depends(deps.get_graph_client),
) -> TermResponse:
    """新增术语。Task 4 起改写编辑层：不再往 terms 表插入新行，而是给
    node_key 写一条 __created__ 编辑——terms 表在 ETL 产出同 node_key 的行
    之前永远没有这一行（见 term_merge._synthesize_created，合并视图会把
    它合成出来，source 固定标 "review"）。

    这个端点是全库唯一的 create_term 生产调用点——"知识图谱审核"页
    （GraphReviewsPage）批准关系时现场创建端点实体，走的就是这里。

    不再做名字冲突检查（原 create_term 内部的 _check_name_conflict）。
    这是刻意的：standard_name 早已不是身份键（2026-08-30 起同一 term_type
    下允许重名），编辑层路径上"名字撞了"不再是数据完整性问题，也不在这里
    重建这道检查。如果这次创建的 node_key 恰好和已有的一行（不管是 ETL
    产出的还是别的编辑层创建的）相同，合并视图会把 __created__ 的字段
    降级成对那一行的普通字段级编辑（见 term_merge.apply_edits），不报错、
    也不会产生第二条记录。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)
    # 创建前先跟同租户、同类型的现有术语比一遍相似度，供管理员在提交后
    # 直接看到"这个新名字是不是已经有一个很像的术语了"——查询范围限定在
    # 同 term_type，避免不同类型之间凑巧撞名字的噪声提示。走合并视图，
    # 这样刚被人工编辑过（改名/属性）的术语也能算进相似度比对。
    existing_terms = await list_terms_merged(review_conn, tenant_id, source=None)
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
    extra_properties = payload.extra_properties or {}
    node_key = f"{payload.term_type}:{payload.standard_name}"

    # 祖父豁免：按 node_key 查是否已有该实体（terms 表中可能有、也可能没有）。
    # 新的合并语义下，POST 写的 __created__ 编辑如果 node_key 撞上已有实体，
    # 实际上会降级成对那一行的普通字段级编辑（见 term_merge.apply_edits），
    # 这时应该豁免已有属性键的校验（它们可能因 term_type 声明变更而成为"废弃字段"）。
    # 用 get_term_by_node_key（查 terms 表原始行）而不是合并视图，因为
    # 祖父豁免关心的是"这个实体上在 terms 表里实际存在的属性键"。
    existing_extra_property_keys = frozenset()
    try:
        existing_term = await get_term_by_node_key(review_conn, tenant_id=tenant_id, node_key=node_key)
        existing_extra_property_keys = frozenset(existing_term.extra_properties)
    except TermNotFoundError:
        # 查不到原始行 = 纯新建，无需豁免
        pass

    try:
        await validate_term_categories(
            review_conn, tenant_id=tenant_id, term_type=payload.term_type,
            extra_properties=extra_properties,
            existing_extra_property_keys=existing_extra_property_keys,
        )
    except UnknownCategoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except InvalidExtraPropertyTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await upsert_term_edit(
        review_conn,
        tenant_id=tenant_id,
        node_key=node_key,
        field=FIELD_CREATED,
        value={
            "standard_name": payload.standard_name,
            "term_type": payload.term_type,
            "aliases": payload.aliases,
            "extra_properties": extra_properties,
        },
        edited_by=_EDITED_BY,
    )
    # 人工重建一个曾被人工删除的 node_key：撤掉那条 __deleted__ 编辑，让它
    # 重新可见。这不违反"人工删除不可被恢复"——那条规矩的准确表述是
    # Foundry 的「Deletions aren't reversible by datasource updates」，
    # 禁的是**数据源更新**把人删掉的东西带回来（ETL 重跑仍然做不到，
    # 见 term_merge.apply_edits 里 FIELD_DELETED 的短路），而不是禁止人
    # 自己撤销自己的删除。
    #
    # 顺序：先写 __created__ 再撤 __deleted__。反过来的话，中间一步失败会
    # 让实体带着删除前的旧值重新可见；现在这个顺序下中间失败则维持删除
    # 状态不变，是安全的那一侧。
    #
    # 不这样做的后果不是"静默成功"而是 500：下面那句
    # get_term_merged_by_node_key 会因 __deleted__ 抛 TermNotFoundError，
    # 路由没有捕获它——一次合法的重建操作变成不透明的服务端错误。
    await delete_term_edit(
        review_conn, tenant_id=tenant_id, node_key=node_key, field=FIELD_DELETED
    )
    # 写完编辑层后从合并视图取回同步进图谱——图谱应当是合并结果的投影。
    # 响应体用原来的逻辑（payload.source）保持兼容性。
    merged_term = await get_term_merged_by_node_key(review_conn, tenant_id=tenant_id, node_key=node_key)
    term_to_return = Term(
        tenant_id=tenant_id,
        node_key=node_key,
        standard_name=payload.standard_name,
        aliases=payload.aliases,
        term_type=payload.term_type,
        extra_properties=extra_properties,
        source=payload.source,
    )
    # 新增成功后立即同步进图谱（属性+别名节点），不留图谱异步落后的窗口。
    try:
        await graph_client.sync_term(merged_term)
    except Exception:
        logger.exception(
            "术语 %r（租户 %r）已写入 SQLite 但同步进图谱失败——两侧数据已不一致，需要人工核对",
            term_to_return.standard_name, tenant_id,
        )
        raise
    return _to_response(term_to_return, similar_terms=similar_terms_payload)


@router.put("/{node_key}", response_model=TermResponse)
async def update_existing_term(
    tenant_id: str,
    node_key: str,
    payload: TermWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: GraphWriteProtocol = Depends(deps.get_graph_client),
) -> TermResponse:
    """编辑术语。Task 4 起改写编辑层：不再 UPDATE terms 表那一行，而是
    按提交的字段写 term_edits——standard_name/aliases/term_type 各一条，
    terms 表那一行原样不动，重跑 ETL 不会丢掉这次编辑之外的字段。

    extra_properties 的语义跟 TermWriteRequest 文档字符串、
    frontend/src/admin/termsApi.ts 明文的约定保持一致，也是
    docs/superpowers/specs/2026-08-30-manual-edits-layer-design.md 之后
    的一次修正（原实现误把"缺席=保留"当成了"显式 {}=保留"）：

    - payload.extra_properties 缺席（None）：不写任何属性编辑——这次
      请求不涉及属性值，保留原样，字段级不碰这个字段。
    - payload.extra_properties 非 None（含显式的 {}）：写一条
      FIELD_EXTRA_PROPERTIES 整字典编辑，值就是提交的字典，整体接管
      这个字段——提交 {} 因此真正清空全部属性值。这不是退回整行覆盖：
      只有属性这一个字段被冻结，standard_name/aliases/term_type 仍然
      各自独立按字段级编辑，只改名字和别名（extra_properties 缺席）时
      属性值继续完全跟随 ETL，spec 要防的"人工只改了展示名却导致金额
      再也不跟着数据源更新"没有被破坏。见 term_merge._apply_field_edits
      里 FIELD_EXTRA_PROPERTIES 和 EXTRA_PROPERTY_PREFIX 单键编辑共存
      时的合并顺序。

    不再做名字冲突检查（原 update_term 内部的 _check_name_conflict），
    理由同 create_new_term 的文档字符串：standard_name 不再是身份键，
    编辑层路径上不重建这道检查。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        existing_before_update = await get_term_merged_by_node_key(review_conn, tenant_id, node_key)
    except TermNotFoundError:
        raise HTTPException(status_code=404, detail="术语不存在")
    try:
        await validate_term_categories(
            review_conn, tenant_id=tenant_id, term_type=payload.term_type,
            extra_properties=payload.extra_properties or {},
            existing_extra_property_keys=frozenset(existing_before_update.extra_properties),
        )
    except UnknownCategoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except InvalidExtraPropertyTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await upsert_term_edit(
        review_conn, tenant_id=tenant_id, node_key=node_key, field="standard_name",
        value=payload.standard_name, edited_by=_EDITED_BY,
    )
    await upsert_term_edit(
        review_conn, tenant_id=tenant_id, node_key=node_key, field="aliases",
        value=payload.aliases, edited_by=_EDITED_BY,
    )
    await upsert_term_edit(
        review_conn, tenant_id=tenant_id, node_key=node_key, field="term_type",
        value=payload.term_type, edited_by=_EDITED_BY,
    )
    if payload.extra_properties is not None:
        # 整字典编辑：提交了（哪怕是空字典）就整体接管这个字段。
        await upsert_term_edit(
            review_conn, tenant_id=tenant_id, node_key=node_key,
            field=FIELD_EXTRA_PROPERTIES, value=payload.extra_properties,
            edited_by=_EDITED_BY,
        )
    if payload.standard_name != existing_before_update.standard_name:
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
                existing_before_update.standard_name, payload.standard_name, tenant_id,
            )
            raise
    # 写完编辑层后从合并视图取回——图谱应当是合并结果的投影。
    term = await get_term_merged_by_node_key(review_conn, tenant_id=tenant_id, node_key=node_key)
    try:
        await graph_client.sync_term(term)
    except Exception:
        logger.exception(
            "术语 %r（租户 %r）已写入 SQLite 但同步进图谱失败——两侧数据已不一致，需要人工核对",
            term.standard_name, tenant_id,
        )
        raise
    return _to_response(term)


@router.delete("/{node_key}")
async def delete_existing_term(
    tenant_id: str,
    node_key: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: GraphWriteProtocol = Depends(deps.get_graph_client),
) -> dict[str, bool]:
    """删除术语。Task 4 起改写编辑层：不再 DELETE terms 表那一行，只写
    一条 __deleted__ 编辑——terms 表那一行（如果存在）继续留着，ETL 还在
    维护它，只是对所有读路径（含这个管理后台自己的列表接口）不可见，
    重跑 ETL 也不会让它复活（Global Constraints"人工删除不可被 ETL
    恢复"）。图谱侧仍然真删（delete_term_node），因为 Neo4j 没有"对部分
    读路径隐藏"这种中间状态，图上不该留一个 SQLite 侧已经不可见的节点。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)
    # 先确认术语本身存在——404 的优先级要在 409 之前：一个根本不存在的
    # 名字不该因为图谱里凑巧有同名孤儿边就返回"已在图谱中使用"这种
    # 误导性的错误。走合并视图：已经被人工删除过的实体（__deleted__）
    # 和只存在于编辑层的实体（纯 __created__，terms 表没有对应行）都要能
    # 被这一步正确识别。确认存在之后再查图谱：这个术语已经被真实关系边
    # 使用的话拒绝删除，避免"词表说不存在了，但图谱边还在用它"的不
    # 一致状态——这一步必须在写 __deleted__ 编辑之前，不能标记完删除
    # 才发现图谱不允许删。
    try:
        term = await get_term_merged_by_node_key(review_conn, tenant_id, node_key)
    except TermNotFoundError:
        raise HTTPException(status_code=404, detail="术语不存在")
    edge_count = await graph_client.count_relation_edges_for_term(
        tenant_id=tenant_id, node_key=term.node_key
    )
    if edge_count > 0:
        raise HTTPException(status_code=409, detail="该术语已在图谱中使用，无法删除")
    await upsert_term_edit(
        review_conn, tenant_id=tenant_id, node_key=term.node_key, field=FIELD_DELETED,
        value=None, edited_by=_EDITED_BY,
    )
    try:
        await graph_client.delete_term_node(tenant_id=tenant_id, node_key=term.node_key)
    except Exception:
        logger.exception(
            "术语 %r（租户 %r）已从 SQLite 删除，但图谱节点删除失败——SQLite 记录已不存在，"
            "图谱节点仍然存在且对管理后台不可见，需要人工核对",
            term.standard_name, tenant_id,
        )
        raise
    return {"deleted": True}
