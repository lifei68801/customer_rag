from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api import deps
from app.api.admin_session import AdminSession
from app.graphrag.tenant_personas_store import get_personas
from app.graphrag.tenants_store import list_tenants

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
