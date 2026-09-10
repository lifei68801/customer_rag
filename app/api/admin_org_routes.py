"""组织管理。只有 admin 能用。

组织只是租户之上的一层归拢，不参与任何隔离判据（见
app/graphrag/organizations_store.py 的模块 docstring）——它存在的唯一理由
是让看板能按客户聚合。account 授权（哪个账号能访问哪个租户）不归这里，
那是 app/api/admin_account_routes.py 的 `/accounts/{username}/tenants`：
两者是正交的两件事，一个管"租户属于哪个客户"，一个管"账号能碰哪个租户"。

本文件只留「组织」自身的端点，router 前缀是 `/api/admin/organizations`：
建组织、列组织（带各自的 tenant_ids）。`/accounts/{username}/tenants`
落在既有的 admin_account_routes.py 里，不在这里重复。

把租户挂到组织下/移出组织（`PUT .../organization`）不在这里，在
app/api/admin_tenant_routes.py：那个端点改的是一行 tenants 记录
（`UPDATE tenants SET org_id`），跟"停用/启用租户"是同一类操作——按内聚性
应该跟它们放在一起，而不是因为路径里出现了 "organization" 字样就归进
这个文件。它的 `{tenant_id}` 是**被操作的对象**，不是租户作用域，跟
`/api/admin/tenants/{tenant_id}/disable` 同一个道理（见
tests/api/test_admin_route_shapes.py 的 `_NON_TENANT_PREFIXES` 注释）。
"""
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.graphrag.organizations_store import (
    InvalidOrganizationError,
    OrganizationAlreadyExistsError,
    create_organization,
    list_organizations,
    list_tenants_in_org,
)

router = APIRouter(
    prefix="/api/admin/organizations", dependencies=[Depends(deps.require_admin_role)]
)
# member 能建组织的话，组织视图上就会出现一个他随手起名建出来的组织——
# 这不是隔离判据被绕过（隔离仍然是 user_tenants 那张表管，组织本身不参与
# 判据，见上面模块 docstring），但足以在管理界面上制造混乱，所以整个组织
# 管理面收在 admin 专属之下。


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


@router.post("", status_code=201)
async def create_org(
    payload: OrganizationCreateRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, str]:
    try:
        await create_organization(review_conn, org_id=payload.org_id, name=payload.name)
    except (OrganizationAlreadyExistsError, InvalidOrganizationError) as exc:
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
