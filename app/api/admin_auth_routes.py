from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.config.settings import Settings

router = APIRouter(prefix="/admin/auth")


class LoginRequest(BaseModel):
    admin_token: str


class LoginResponse(BaseModel):
    session_token: str


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    settings: Settings = Depends(deps.get_settings),
    session_store: AdminSessionStore = Depends(deps.get_admin_session_store),
) -> LoginResponse:
    if not settings.admin_token or payload.admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="管理员 token 不正确")
    session_token = session_store.create_session()
    return LoginResponse(session_token=session_token)


@router.get("/whoami", dependencies=[Depends(deps.require_admin_session)])
async def whoami() -> dict[str, bool]:
    return {"authenticated": True}
