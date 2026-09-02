from __future__ import annotations

import secrets
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class AdminSession:
    """一个登录中的管理员身份。

    tenant_id 为 None 表示 admin——它不属于任何租户，可以访问全部。
    """

    username: str
    role: str  # "admin" | "member"
    tenant_id: str | None
    expires_at: float


class AdminSessionStore:
    """进程内管理员 session 存取，token -> AdminSession。

    不做持久化——管理员 session 本来就设计成短期有效（默认 8 小时），
    进程重启导致所有人重新登录是可接受的代价，换来不用额外引入
    JWT 签名/SQLite 表这些复杂度。

    不改用 JWT 的另一个理由：JWT 签发后撤销不了，「禁用账号立即生效」
    就做不到，而维护黑名单等于又回到有状态，白付签名验签的复杂度。
    """

    def __init__(self) -> None:
        self._sessions: dict[str, AdminSession] = {}

    def create_session(
        self,
        *,
        username: str,
        role: str,
        tenant_id: str | None,
        ttl_seconds: int = 28800,
    ) -> str:
        self._sweep_expired()
        token = secrets.token_urlsafe(32)
        self._sessions[token] = AdminSession(
            username=username,
            role=role,
            tenant_id=tenant_id,
            expires_at=time.time() + ttl_seconds,
        )
        return token

    def _sweep_expired(self) -> None:
        """顺手清掉已过期但从未被查询过的 session。

        不引入后台定时任务/线程——管理员场景登录频率很低，"每次新登录时
        顺便扫一遍"足够避免字典无限增长，不需要额外的调度基础设施。
        """
        now = time.time()
        expired = [token for token, session in self._sessions.items() if now >= session.expires_at]
        for token in expired:
            del self._sessions[token]

    def get_session(self, token: str) -> AdminSession | None:
        session = self._sessions.get(token)
        if session is None:
            return None
        if time.time() >= session.expires_at:
            del self._sessions[token]
            return None
        return session

    def revoke_session(self, token: str) -> None:
        self._sessions.pop(token, None)
