from __future__ import annotations

from typing import Literal

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.api.admin_session import AdminSession
from app.api.tenant_guard import require_active_tenant_or_404
from app.graphrag.guided_questions import GraphUnavailable, generate_questions
from app.graphrag.neo4j_client import GraphWriteProtocol
from app.graphrag.question_validation import find_unmatched_questions
from app.graphrag.tenant_personas_store import (
    get_persona,
    get_personas,
    get_questions,
    get_questions_setting,
    set_questions,
    upsert_persona,
)
from app.graphrag.tenants_store import list_tenants
from app.graphrag.terms_store import list_terms_merged

router = APIRouter(
    prefix="/api/admin/personas", dependencies=[Depends(deps.require_admin_session)]
)
# 不挂 require_admin_role：右栏对 member 同样要出现。它是这个账号体系里
# 少数几个「所有角色都能调」的端点之一，而它安全的理由是返回内容本身就按
# list_accessible_tenant_ids 过滤过——member 拿到的只有他自己那几个。
#
# 也不挂 require_tenant_access：路径里没有 {tenant_id} 段（见
# tests/api/test_admin_route_shapes.py 的 _NON_TENANT_PREFIXES），挂上去
# FastAPI 会把 tenant_id 当成必填 query 参数，请求直接 422。


class Persona(BaseModel):
    tenant_id: str
    name: str
    avatar: str
    tagline: str


class PersonaListResponse(BaseModel):
    personas: list[Persona]
    current_tenant_id: str | None


@router.get("", response_model=PersonaListResponse)
async def list_my_personas(
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    session: AdminSession = Depends(deps.require_admin_session),
) -> PersonaListResponse:
    """这个账号能访问的数字人。前台右栏和后台租户切换器共用它。

    只列启用中的租户：停用的切过去之后所有写操作都是 404，而用户不知道
    为什么——把它摆在可点击的位置上等于埋一个陷阱。

    没配过脸的租户照样出现，name 退回租户名、avatar/tagline 为空。不出现的
    话，管理员新建租户并授权之后用户看不见它，而没有任何地方告诉他还差一步。

    不带 questions：右栏一次要列 N 个数字人，逐个跑一遍 generate_questions
    就是 N 次图查询。前台只对当前这一个数字人请求引导问题详情，见下面的
    persona_router::get_my_persona。
    """
    accessible = await deps.list_accessible_tenant_ids(review_conn, session)
    # list_tenants() 不传参数 = 只列 status='active' 的（tenants_store.py 的
    # 默认值），正是这个端点要的——不能传 include_disabled=True，见上面的
    # docstring。
    active = await list_tenants(review_conn)
    if accessible is not None:
        allowed = set(accessible)
        active = [t for t in active if t["tenant_id"] in allowed]
    faces = await get_personas(review_conn, [t["tenant_id"] for t in active])
    return PersonaListResponse(
        personas=[
            Persona(
                tenant_id=t["tenant_id"],
                name=t["name"],
                avatar=faces.get(t["tenant_id"], {}).get("avatar", ""),
                tagline=faces.get(t["tenant_id"], {}).get("tagline", ""),
            )
            for t in active
        ],
        current_tenant_id=session.current_tenant_id,
    )


# 租户内路径，跟上面 /api/admin/personas（非租户）分开一个 router：prefix 里
# 带上资源名（persona），照 admin_terms_routes.py 的写法。挂到 app/main.py 的
# tenant_scoped 之下，那里统一挂了 require_tenant_access（main.py:175）。
#
# 下面每个端点用普通路径参数取 tenant_id，跟其余八个租户内 router 一致，
# **不**再在端点上写一次 Depends(deps.require_tenant_access)。曾经写过，
# 理由是"两处缺一个都会失去保护"——那句话不成立：
# tests/api/test_admin_route_shapes.py::test_every_tenant_scoped_route_checks_tenant_access
# 会在挂载被移走时直接变红。而多写的那一份有实际代价：它让"挂载被移走"
# 这个变异在本文件的用例里看不出来，守卫因此少了一层可验证性。
persona_router = APIRouter(
    prefix="/api/admin/{tenant_id}/persona",
    dependencies=[Depends(deps.require_admin_session)],
)


class PersonaDetail(BaseModel):
    tenant_id: str
    name: str
    avatar: str
    tagline: str
    questions: list[str]
    #: `questions` 这一批是手写的、自动兜底的，还是"本该自动兜底但图谱不通"。
    #:
    #: 两者在界面上长得一模一样，行为却不同：手写的固定不变，自动的会随
    #: 本体变化。编辑页分不清的话，它会把自动兜底那批显示成管理员自己配
    #: 的，一按保存就 `set_questions` 落库成手写、从此不再更新——而界面
    #: 全程没说过这件事。
    questions_source: Literal["handwritten", "generated", "unavailable"]


class PersonaWriteRequest(BaseModel):
    avatar: str
    tagline: str
    questions: list[str]


async def _tenant_name(review_conn: aiosqlite.Connection, tenant_id: str) -> str:
    """租户展示名，查不到时退回 tenant_id 本身，跟 GET /api/admin/personas
    的退回口径一致。"""
    tenants = await list_tenants(review_conn, include_disabled=True)
    for t in tenants:
        if t["tenant_id"] == tenant_id:
            return t["name"]
    return tenant_id


@persona_router.get("", response_model=PersonaDetail)
async def get_my_persona(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: GraphWriteProtocol = Depends(deps.get_graph_client),
) -> PersonaDetail:
    """当前这一个数字人的完整信息，含引导问题。

    只在这里跑一次 generate_questions——右栏那份 GET /api/admin/personas
    绝不能带 questions：右栏一次要列 N 个数字人，逐个跑一遍
    generate_questions 就是 N 份这样的开销。前台只对当前这一个数字人请求
    这份详情，切换数字人时重新请求，开销因此是一个数字人的份，不是 N 个的。

    **一个数字人的份不等于一次图查询**：generate_questions 会对每个已确认
    的关系组合各探一次图，串行、无缓存（见 guided_questions.py）。有 M 个
    组合就是 M 次往返，`limit` 是在全部探完之后才截断的。手写过引导问题的
    租户走不到这条路（`handwritten is not None` 就直接返回），所以这笔开销
    只落在还没配过的租户身上——也就是每一个新租户的首屏。

    手写优先，一条都没有时才自动兜底：两档并列显示的话用户分不清哪条是
    人写的，而这两者的可信度差很多（spec D2）。

    不校验租户是否启用：这是读路由，app/api/tenant_guard.py 的模块 docstring
    说这条守卫只加在写路由上、读操作零例外都不挂——跟同一个 router 里的
    list_stale_questions 保持一致，两个读端点不能一个查一个不查。
    """
    name = await _tenant_name(review_conn, tenant_id)
    persona = await get_persona(review_conn, tenant_id)
    # `is None` 而不是 `or`：显式存下来的空列表意思是"我不要引导问题"，
    # 兜底必须让路。用 `or` 的话管理员刚删光的内容会原样冒回前台，而他
    # 唯一的出路是留一条自己不想要的问题。
    handwritten = await get_questions_setting(review_conn, tenant_id)
    if handwritten is None:
        source: Literal["handwritten", "generated", "unavailable"] = "generated"
        try:
            questions = await generate_questions(
                review_conn, graph_client, tenant_id=tenant_id
            )
        except GraphUnavailable:
            # 前台照样是空引导区（诚实），但后台要能看出这是故障而不是
            # "本体里还没东西可问"——管理员是唯一能去修图谱连接的人。
            questions = []
            source = "unavailable"
    else:
        questions = handwritten
        source = "handwritten"
    return PersonaDetail(
        tenant_id=tenant_id,
        name=name,
        avatar=(persona or {}).get("avatar", ""),
        tagline=(persona or {}).get("tagline", ""),
        questions=questions,
        questions_source=source,
    )


@persona_router.put("", response_model=PersonaDetail)
async def write_my_persona(
    payload: PersonaWriteRequest,
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> PersonaDetail:
    """写数字人的脸和引导问题。

    租户校验由 app/main.py 的 tenant_scoped 挂载统一提供：读端点按 accessible
    过滤，写端点不校验的话，member 能改别人数字人的脸。

    校验不通过时**一条都不存**，并且**点名是哪几条**。只说「保存失败」的话，
    配了六条的人得自己一条条试出来是哪条有问题。校验必须排在两次写入
    （upsert_persona/set_questions）之前——先写后校验的话，校验失败时
    界面上已经出现了一组用户从没确认过的问题。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)
    terms = await list_terms_merged(review_conn, tenant_id)
    unmatched = find_unmatched_questions(payload.questions, terms)
    if unmatched:
        raise HTTPException(
            status_code=400,
            detail=(
                "这几条引导问题在当前本体里一个已知名字都没提到，"
                f"点了大概率答不出来：{'、'.join(unmatched)}"
            ),
        )
    await upsert_persona(
        review_conn, tenant_id=tenant_id, avatar=payload.avatar, tagline=payload.tagline
    )
    await set_questions(review_conn, tenant_id=tenant_id, questions=payload.questions)
    name = await _tenant_name(review_conn, tenant_id)
    return PersonaDetail(
        tenant_id=tenant_id,
        name=name,
        avatar=payload.avatar,
        tagline=payload.tagline,
        questions=payload.questions,
        # 刚存进去的就是手写的，哪怕存的内容原本是从自动那批复制过来的
        # ——用户按了保存，这批就归他了。
        questions_source="handwritten",
    )


@persona_router.get("/stale-questions")
async def list_stale_questions(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, list[str]]:
    """当前本体下已经不再命中的手写引导问题。

    保存时校验过不代表永远有效——本体后来改了、实体被删了，那条问题就
    失效了。它不该默默消失（用户会以为自己没配过），而是在看板上变成一条
    待办（spec 前台硬规矩之二）。
    """
    handwritten = await get_questions(review_conn, tenant_id)
    terms = await list_terms_merged(review_conn, tenant_id)
    return {"stale": find_unmatched_questions(handwritten, terms)}
