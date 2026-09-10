from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

import aiosqlite

from app.api import deps
from app.tenancy import is_valid_tenant_id
from app.graphrag.organizations_store import (
    OrganizationDisabledError,
    OrganizationNotFoundError,
    assign_tenant_to_org,
)
from app.graphrag.tenants_store import (
    TenantAlreadyExistsError,
    TenantNotFoundError,
    create_tenant,
    list_tenants,
    set_tenant_status,
)

# 新建/停用租户是 admin 专属。member 建了租户也进不去（新建的租户不在
# 它被授权的那几个里，也不是它 admin_users.tenant_id 的回退值），只会留下
# 一个没人能用的空租户；而停用租户对 member 更危险——按租户作用域校验的话
# （即 URL 里的 tenant_id 落在它能访问的范围内就放行），它对自己被授权的
# 那几个租户之一会通过校验，于是能把自己能访问的某个租户停掉。
router = APIRouter(prefix="/api/admin/tenants", dependencies=[Depends(deps.require_admin_role)])


class TenantResponse(BaseModel):
    tenant_id: str
    name: str
    status: str


class TenantListResponse(BaseModel):
    tenants: list[TenantResponse]


class TenantCreateRequest(BaseModel):
    tenant_id: str
    name: str

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, value: str) -> str:
        if not is_valid_tenant_id(value):
            raise ValueError("tenant_id 只能包含字母、数字、下划线和连字符")
        return value

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name 不能为空")
        return stripped


class TenantOrganizationRequest(BaseModel):
    org_id: str | None


@router.get("", response_model=TenantListResponse)
async def list_all_tenants(
    include_disabled: bool = False,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> TenantListResponse:
    """默认只返回启用中的租户。

    账号菜单的切换下拉框用的就是这个默认值——列出停用的租户会让用户切过去
    之后发现读得到、写全是 404（tenant_guard 那条"读放行、写不放行"的
    策略），那是最难查的一类状态：界面看着正常，操作却一个都不成功。

    只有租户管理页传 include_disabled=true：它要能看到停用的租户，否则就
    没法把它们启用回来。
    """
    tenants = await list_tenants(review_conn, include_disabled=include_disabled)
    return TenantListResponse(tenants=[TenantResponse(**t) for t in tenants])


@router.post("", response_model=TenantResponse, status_code=201)
async def create_new_tenant(
    payload: TenantCreateRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> TenantResponse:
    try:
        await create_tenant(review_conn, tenant_id=payload.tenant_id, name=payload.name)
    except TenantAlreadyExistsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return TenantResponse(tenant_id=payload.tenant_id, name=payload.name, status="active")


@router.post("/{tenant_id}/disable")
async def disable_tenant(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, str]:
    try:
        await set_tenant_status(review_conn, tenant_id, "disabled")
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在")
    return {"status": "disabled"}


@router.post("/{tenant_id}/enable")
async def enable_tenant(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, str]:
    try:
        await set_tenant_status(review_conn, tenant_id, "active")
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在")
    return {"status": "active"}


@router.put("/{tenant_id}/organization")
async def set_tenant_organization(
    tenant_id: str,
    payload: TenantOrganizationRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, str | None]:
    """把这个租户挂到 org_id 下；org_id 传 null 是移出组织。

    这个 tenant_id 是**被操作的对象**（要挂到组织下的是哪个租户），不是
    操作发生的租户作用域，跟上面 disable/enable 两个端点同一个道理——
    按租户作用域校验的话，member 对自己被授权的那几个租户之一会顺利通过，
    那不是这里想要的：组织管理整体是 admin 专属（本 router 的依赖），不看
    调用者跟这个租户是什么关系。

    两侧存在性都在 assign_tenant_to_org 里校验过（组织不存在、租户不存在
    分别抛不同的异常，见 app/graphrag/organizations_store.py），这里只
    负责把异常翻译成 4xx——放行任何一侧不存在都会让这个租户从组织视图里
    静默消失，或者移出操作静默影响 0 行。
    """
    try:
        await assign_tenant_to_org(review_conn, tenant_id=tenant_id, org_id=payload.org_id)
    except OrganizationNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OrganizationDisabledError as exc:
        # 409：不是请求写错了，是这个组织当前的状态不接收新租户。
        raise HTTPException(status_code=409, detail=str(exc))
    except TenantNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"tenant_id": tenant_id, "org_id": payload.org_id}
