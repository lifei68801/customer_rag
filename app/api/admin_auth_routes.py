from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.config.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/auth")


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
    # 常量时间比较：普通的 != 会在第一个不同字节处提前返回，攻击者可以按
    # 响应耗时逐字节爆破这个 token——而它是全租户唯一的写权限凭证（上传、
    # 删除、把关系批准进 Neo4j）。compare_digest 对 str 只接受 ASCII，
    # 统一编码成 bytes 比较，避免管理员 token 含非 ASCII 字符时抛 TypeError。
    if not settings.admin_token or not secrets.compare_digest(
        payload.admin_token.encode("utf-8"), settings.admin_token.encode("utf-8")
    ):
        # 只记录"发生了失败登录"，绝不记录尝试的 token 值——日志本身通常比
        # 环境变量更容易泄露。目前没有限流/锁定，这条日志是唯一的审计线索。
        logger.warning(
            "管理员登录失败：admin_token 不匹配（admin_token 未配置=%s）",
            not settings.admin_token,
        )
        raise HTTPException(status_code=401, detail="管理员 token 不正确")
    session_token = session_store.create_session()
    return LoginResponse(session_token=session_token)


@router.get("/whoami", dependencies=[Depends(deps.require_admin_session)])
async def whoami() -> dict[str, bool]:
    return {"authenticated": True}
