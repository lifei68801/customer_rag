from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class AdminSession:
    """一个登录中的管理员身份。

    tenant_id 为 None 表示 admin——它不属于任何租户，可以访问全部。
    """

    username: str
    role: str  # "admin" | "member"
    tenant_id: str | None
    expires_at: float
    # 当前正在操作的租户。前台问答与后台页面共用这一个值——前端不再自己
    # 在 sessionStorage 里记，否则它会按标签页隔离、和 Cookie 会话不同步。
    #
    # 与 tenant_id 的区别：tenant_id 是"你属于哪个租户"（member 固定、
    # admin 为 None），current_tenant_id 是"你现在在看哪个租户"。member
    # 两者恒等；admin 的 tenant_id 永远是 None，current_tenant_id 才是
    # 他切到的那个。
    current_tenant_id: str | None = None


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
            current_tenant_id=tenant_id,
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

    def set_current_tenant(self, token: str, tenant_id: str) -> AdminSession | None:
        """切换这个会话的当前租户。

        AdminSession 是 frozen 的，改不了字段——用 replace 生成新实例并
        写回字典。只 replace 不写回的话，下次 get_session 拿到的还是旧值，
        界面上表现为"切了租户但没切"。

        不做权限判断：谁能切到哪个租户由路由层的 require_tenant_access
        决定，这里只负责存。
        """
        session = self.get_session(token)
        if session is None:
            return None
        updated = replace(session, current_tenant_id=tenant_id)
        self._sessions[token] = updated
        return updated
