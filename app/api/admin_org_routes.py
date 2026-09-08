"""组织管理。只有 admin 能用。

组织只是租户之上的一层归拢，不参与任何隔离判据（见
app/graphrag/organizations_store.py 的模块 docstring）——它存在的唯一理由
是让看板能按客户聚合。account 授权（哪个账号能访问哪个租户）不归这里，
那是 app/api/admin_account_routes.py 的 `/accounts/{username}/tenants`：
两者是正交的两件事，一个管"租户属于哪个客户"，一个管"账号能碰哪个租户"。

本文件只留组织相关的端点，router 前缀收窄到 `/api/admin/organizations`——
`/accounts/{username}/tenants` 已经落在既有的 admin_account_routes.py
里，不在这里重复。三个端点：建组织、列组织（带各自的 tenant_ids）、把
租户挂到组织下/移出组织。
"""
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.graphrag.organizations_store import (
    OrganizationAlreadyExistsError,
    OrganizationNotFoundError,
    assign_tenant_to_org,
    create_organization,
    list_organizations,
    list_tenants_in_org,
)
from app.graphrag.tenants_store import TenantNotFoundError

router = APIRouter(
    prefix="/api/admin/organizations", dependencies=[Depends(deps.require_admin_role)]
)
# member 能建组织的话，他就能把别人的租户挂到自己建的组织下——那条路本身
# 挂了 tenant_id 双重存在性校验（assign_tenant_to_org），但组织视图会把
# 一个 member 本无权碰的租户摆进一个他随手建的组织里，这不是隔离判据被
# 绕过（隔离仍然是 user_tenants 那张表管），但足以在管理界面上制造混乱，
# 所以整个组织管理面收在 admin 专属之下。


class OrganizationCreateRequest(BaseModel):
    org_id: str
    name: str


class OrganizationResponse(BaseModel):
    org_id: str
    name: str
    status: str
    tenant_ids: list[str]


class OrganizationListResponse(BaseModel):
    organizations: list[OrganizationResponse]


class TenantOrganizationRequest(BaseModel):
    org_id: str | None


@router.post("", status_code=201)
async def create_org(
    payload: OrganizationCreateRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, str]:
    try:
        await create_organization(review_conn, org_id=payload.org_id, name=payload.name)
    except OrganizationAlreadyExistsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"org_id": payload.org_id, "name": payload.name}


@router.get("", response_model=OrganizationListResponse)
async def list_orgs(
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> OrganizationListResponse:
    orgs = await list_organizations(review_conn)
    organizations = []
    for org in orgs:
        tenant_ids = await list_tenants_in_org(review_conn, org["org_id"])
        organizations.append(
            OrganizationResponse(
                org_id=org["org_id"],
                name=org["name"],
                status=org["status"],
                tenant_ids=tenant_ids,
            )
        )
    return OrganizationListResponse(organizations=organizations)


@router.put("/tenants/{tenant_id}")
async def set_tenant_organization(
    tenant_id: str,
    payload: TenantOrganizationRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, str | None]:
    """把这个租户挂到 org_id 下；org_id 传 null 是移出组织。

    两侧存在性都在 assign_tenant_to_org 里校验过（组织不存在、租户不存在
    分别抛不同的异常），这里只负责把异常翻译成 4xx——放行任何一侧不存在
    都会让这个租户从组织视图里静默消失，或者移出操作静默影响 0 行。
    """
    try:
        await assign_tenant_to_org(review_conn, tenant_id=tenant_id, org_id=payload.org_id)
    except OrganizationNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except TenantNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"tenant_id": tenant_id, "org_id": payload.org_id}
