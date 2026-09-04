from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

import aiosqlite

from app.api import deps
from app.api.admin_session import AdminSession
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
    count_terms_merged_by_term_type,
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

#: 删除被挡住时，消息里最多点名几条关系边。跟 ontology_categories 的
#: _IN_USE_SAMPLE_SIZE 同一个取舍：3 条够用户认出是哪批数据，再多没人读，
#: 剩下的用总数兜底。两处故意各自定义——它们数的不是同一种东西，
#: 将来任一边想调都不该被另一边绑住。
_IN_USE_SAMPLE_SIZE = 3


def _format_samples(samples: list[str], total: int) -> str:
    """把样本拼成“a、b、c 等共 12 条”。样本已经是全部时不加尾巴——
    “2 条（a、b 等共 2 条）”读起来像还有别的没列出来。"""
    listed = "、".join(samples)
    if total > len(samples):
        return f"{listed} 等共 {total} 条"
    return listed


def _describe_relation(row: dict[str, Any], term_standard_name: str) -> str:
    """一条关系边渲染成“主语 -类型-> 宾语”。方向必须照实还原：
    list_term_relations 的 direction 是相对于当前术语说的（"out" = 这个术语
    是主语），照抄成固定顺序会把关系的角色说反，用户按反的方向去找那条边
    会找不到。"""
    other = row.get("standard_name") or row.get("node_key") or "?"
    relation_type = row.get("relation_type", "?")
    if row.get("direction") == "out":
        return f"{term_standard_name} -{relation_type}-> {other}"
    return f"{other} -{relation_type}-> {term_standard_name}"


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


class InconsistentTermRelation(BaseModel):
    """一条租户标记异常的关系边。

    node_key/standard_name/term_type/other_tenant_id 可空：跨租户的边上，
    对端节点是另一个租户的数据，只有平台管理员看得到它的身份（见
    list_inconsistent_term_relations）。
    """

    direction: Literal["in", "out"]
    relation_type: str
    node_key: str | None
    standard_name: str | None
    term_type: str | None
    other_tenant_id: str | None
    edge_tenant_id: str | None
    #: edge_tenant_mismatch = 两端节点都在本租户，边自己标着别的租户；
    #: cross_tenant = 两端节点分属不同租户，隔离本身已经破了。
    category: Literal["edge_tenant_mismatch", "cross_tenant"]
    #: 当前登录者能不能删这一条。前端据此决定是否给删除按钮——不给出这个
    #: 字段的话，member 只能靠点一下撞 403 才知道自己删不了。
    deletable: bool


class InconsistentTermRelationListResponse(BaseModel):
    relations: list[InconsistentTermRelation]


class TermDetailResponse(TermResponse):
    #: None 表示关系拉取失败，[] 表示确实没有关系。两者必须分开：孤立实体
    #: 对检索基本无用，是个真实且重要的状态，不能跟「Neo4j 挂了」混为一谈。
    relations: list[TermRelation] | None = None


class TermTypeGroup(BaseModel):
    term_type: str
    total: int


class TermSummaryResponse(BaseModel):
    groups: list[TermTypeGroup]


@router.get("/summary", response_model=TermSummaryResponse)
async def get_terms_summary(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> TermSummaryResponse:
    """按实体类型分组的条数。

    实体列表默认第一页永远是按 standard_name 排序的前 50 个——在一个
    20000 条订单号 + 17 条维度实体的租户里，那一页对任何任务都没用。分组
    之后，大基数类型折叠成一行、小基数直接列全部。

    大类型排前面：一条 ETL 映射规则错了就是上万条错，那是最可能出问题的
    地方；小类型人扫一眼就看完了。

    走合并视图：摘要行的数字必须跟点进去看到的条数对得上，对不上的话用户
    会以为自己漏看了几条。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)
    counts = await count_terms_merged_by_term_type(review_conn, tenant_id)
    groups = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return TermSummaryResponse(
        groups=[TermTypeGroup(term_type=t, total=n) for t, n in groups]
    )


def _to_inconsistent_relation(
    row: dict[str, Any], *, tenant_id: str, is_platform_admin: bool
) -> InconsistentTermRelation:
    """一行脏边转成响应对象，并决定当前登录者能看到多少、能不能删。

    对端租户为空（历史上没回填过 tenant_id 的节点）也按跨租户处理：那种
    节点的归属无从判断，按更严的那一档处理不会造成越权。启动时的节点回填
    跑过之后不该再出现这种行。
    """
    other_tenant_id = row.get("other_tenant_id")
    is_cross_tenant = other_tenant_id != tenant_id
    masked = is_cross_tenant and not is_platform_admin
    return InconsistentTermRelation(
        # 方向和关系类型说的是"本租户这个节点身上挂着什么"，不是对端的
        # 信息，遮蔽时也保留——否则那一行什么都没说，用户看了也不知道
        # 该找谁。
        direction=row.get("direction", "out"),
        relation_type=row.get("relation_type", "?"),
        node_key=None if masked else row.get("node_key"),
        standard_name=None if masked else row.get("standard_name"),
        term_type=None if masked else row.get("term_type"),
        other_tenant_id=None if masked else other_tenant_id,
        edge_tenant_id=None if masked else row.get("edge_tenant_id"),
        category="cross_tenant" if is_cross_tenant else "edge_tenant_mismatch",
        deletable=is_platform_admin or not is_cross_tenant,
    )


# 必须排在 GET /{node_key:path} 前面：那条的 path 参数会把
# "xxx/relations/inconsistent" 整个吞掉，反过来这条永远走不到。
@router.get(
    "/{node_key}/relations/inconsistent",
    response_model=InconsistentTermRelationListResponse,
)
async def list_inconsistent_term_relations(
    tenant_id: str,
    node_key: str,
    session: AdminSession = Depends(deps.require_admin_session),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: GraphWriteProtocol = Depends(deps.get_graph_client),
) -> InconsistentTermRelationListResponse:
    """这个实体身上租户标记异常的关系边——详情页那份关系清单看不到的那些。

    它们的处境是本项目最反对的那种：既不参与检索、也不参与实体删除守卫，
    却仍然挂在节点上挡着删除，而界面上一条都看不见。这个接口是它们唯一的
    出口。

    跨租户的那一类上，对端节点属于另一个租户：只有平台管理员能看到它的
    身份（node_key/标准名/租户），member 只被告知"这里挂着一条跨租户的
    边、需要平台管理员处理"。两端都在本租户、只是边标错了租户的那一类
    不遮蔽——那本来就是自己的数据。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)
    rows = await graph_client.list_inconsistent_relation_edges(
        tenant_id=tenant_id, node_key=node_key
    )
    is_platform_admin = session.role == "admin"
    return InconsistentTermRelationListResponse(
        relations=[
            _to_inconsistent_relation(
                row, tenant_id=tenant_id, is_platform_admin=is_platform_admin
            )
            for row in rows
        ]
    )


@router.delete("/{node_key}/relations/inconsistent")
async def delete_inconsistent_term_relation_edge(
    tenant_id: str,
    node_key: str,
    relation_type: str,
    other_node_key: str,
    other_tenant_id: str,
    direction: Literal["out", "in"],
    session: AdminSession = Depends(deps.require_admin_session),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: GraphWriteProtocol = Depends(deps.get_graph_client),
) -> dict[str, int]:
    """删掉一条租户标记异常的边，返回实际删掉的条数。

    授权判据是**两端节点各自的租户**，不是边自己标的那个——这条路径存在的
    理由正是边上那个值不可信。具体地：

    * 起点侧固定取 URL 里的 tenant_id，它已经过 require_tenant_access 校验，
      所以谁也不能借这条路径碰到自己无权的租户的节点；
    * 两端节点都在这个租户里（边只是标错了租户）时，member 就能删——判据
      完全落在他有权的范围内，而这条边正挡着他自己的实体删除；
    * 两端节点分属不同租户时只有平台管理员能删：删掉它同时改变了另一个
      租户的图谱，member 只对自己那一个租户有权，不能单方面替对面做这个
      决定。平台管理员对两个租户都有权，由他来判断。

    请求里自报的 other_tenant_id 不构成授权（它只是用来定位对端节点）：
    填错了只会一条都匹配不上、返回 404，Cypher 那侧仍然按两端节点各自的
    tenant_id 精确匹配。

    删不掉正常的边：底层语句只匹配违反租户不变式的边（见
    _DELETE_INCONSISTENT_RELATION_EDGE_QUERY）。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)
    if other_tenant_id != tenant_id and session.role != "admin":
        logger.warning(
            "拒绝跨租户删边：username=%s（租户 %s）想删 %s/%s 与 %s/%s 之间的 %s 边",
            session.username, session.tenant_id, tenant_id, node_key,
            other_tenant_id, other_node_key, relation_type,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "这条关系边的另一端属于其他租户，删掉它会同时改变那个租户的图谱——"
                "只有平台管理员能处理这一类边"
            ),
        )
    subject, obj = (
        ((tenant_id, node_key), (other_tenant_id, other_node_key))
        if direction == "out"
        else ((other_tenant_id, other_node_key), (tenant_id, node_key))
    )
    removed = await graph_client.delete_inconsistent_relation_edge(
        subject_tenant_id=subject[0],
        subject_node_key=subject[1],
        relation_type=relation_type,
        object_tenant_id=obj[0],
        object_node_key=obj[1],
    )
    if removed == 0:
        raise HTTPException(
            status_code=404,
            detail=(
                f"没有找到这条租户标记异常的关系（{subject[1]} -{relation_type}-> {obj[1]}），"
                "它可能已经被删掉了，或者它其实是一条正常的边（正常的边请在上面的"
                "关系列表里删）"
            ),
        )
    return {"deleted": removed}


# 注意：这条必须排在 /summary 后面。FastAPI 按定义顺序匹配，反过来的话
# "summary" 会被当成一个 node_key，摘要接口永远走不到。
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
    term_type: str | None = None,
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
    if term_type is not None:
        # 按类型筛不能走 SQL 分页：过滤发生在合并之后（人工改过类型的实体
        # 要出现在新类型下），而「公司」可能只有 3 条散落在 20000 条订单号
        # 里——先取一页再过滤会一条都取不到。
        #
        # 代价是全量载入，跟上面的搜索路径一样。可以接受：list_terms 本来
        # 就在别的路径上（agent 每轮消歧、摄取管线）以全量方式被调用。
        matched = await list_terms_merged(review_conn, tenant_id, source=source, term_type=term_type)
        effective_page_size = page_size or 20
        offset = ((page or 1) - 1) * effective_page_size
        return TermListResponse(
            terms=[_to_response(t) for t in matched[offset : offset + effective_page_size]],
            # total 是这个类型下的条数，跟摘要行上的数字同一口径——对不上的
            # 话用户会以为自己漏看了几条。
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


# 必须排在 DELETE /{node_key} 前面：FastAPI 按定义顺序匹配，反过来的话
# node_key 里带斜杠的实体（ETL 的 node_key 模板允许）会先命中那条。
@router.delete("/{node_key}/relations")
async def delete_term_relation_edge(
    tenant_id: str,
    node_key: str,
    relation_type: str,
    other_node_key: str,
    direction: Literal["out", "in"],
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: GraphWriteProtocol = Depends(deps.get_graph_client),
) -> dict[str, int]:
    """删掉这个术语参与的一条关系边，返回实际删掉的条数。

    边按业务键定位：起点 node_key + 关系类型 + 终点 node_key + 租户
    （Neo4j 内部 id 不稳定，不能当外部句柄，见 _DELETE_RELATION_EDGE_QUERY）。
    direction 是相对路径里这个术语说的，跟详情页 GET 返回的关系明细同一套
    口径："out" 表示它是主语，"in" 表示对端是主语——前端照它展示什么就传
    什么，不用自己推断谁是主语。

    删除是不可逆的：一条都没匹配上时报 404 而不是回 200。回 200 的话用户
    刷新后那条边还在，却没有任何地方提示他删的不是它。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)
    subject, obj = (
        (node_key, other_node_key) if direction == "out" else (other_node_key, node_key)
    )
    removed = await graph_client.delete_relation_edge(
        tenant_id=tenant_id,
        subject_node_key=subject,
        relation_type=relation_type,
        object_node_key=obj,
    )
    if removed == 0:
        raise HTTPException(
            status_code=404,
            detail=f"没有找到这条关系（{subject} -{relation_type}-> {obj}），它可能已经被删掉了",
        )
    return {"deleted": removed}


@router.delete("/{node_key}")
async def delete_existing_term(
    tenant_id: str,
    node_key: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: GraphWriteProtocol = Depends(deps.get_graph_client),
) -> Response:
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
        # 只说"已在图谱中使用"，用户得自己去猜是哪条边——而后台此刻手里
        # 就有这份明细（跟删分类那条 d2f1197 同构）。取明细失败不能把 409
        # 变成 500：守卫的结论已经拿到了，丢掉它反而让用户连"为什么删不掉"
        # 都不知道。
        try:
            rows = await graph_client.list_term_relations(
                tenant_id=tenant_id, node_key=term.node_key
            )
        except Exception:
            logger.exception(
                "术语 %r（租户 %r）删除被图谱边挡住，但取回挡路边的明细失败——"
                "只能报出条数，用户无法据此定位具体是哪几条",
                term.standard_name, tenant_id,
            )
            rows = []
        samples = rows[:_IN_USE_SAMPLE_SIZE]
        listed = _format_samples(
            [_describe_relation(row, term.standard_name) for row in samples], edge_count
        )
        detail = (
            f"该术语被 {edge_count} 条关系边使用（{listed}），无法删除"
            if listed
            else f"该术语被 {edge_count} 条关系边使用，无法删除"
        )
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"{detail}；请先在实体详情页删掉这些关系再删术语",
                "blocking_relations": {"total": edge_count, "edges": samples},
            },
        )
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
    return JSONResponse(status_code=200, content={"deleted": True})
