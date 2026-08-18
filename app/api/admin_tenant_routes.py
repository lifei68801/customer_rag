from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

import aiosqlite

from app.api import deps
from app.tenancy import is_valid_tenant_id
from app.graphrag.tenants_store import (
    TenantAlreadyExistsError,
    TenantNotFoundError,
    create_tenant,
    list_tenants,
    set_tenant_status,
)

router = APIRouter(prefix="/api/admin/tenants", dependencies=[Depends(deps.require_admin_session)])


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


@router.get("", response_model=TenantListResponse)
async def list_all_tenants(
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> TenantListResponse:
    tenants = await list_tenants(review_conn)
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
