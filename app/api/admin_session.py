from __future__ import annotations

import secrets
import time


class AdminSessionStore:
    """进程内管理员 session 存取，token -> 过期时间戳（epoch seconds）。

    不做持久化——管理员 session 本来就设计成短期有效（默认 8 小时），
    进程重启导致所有人重新登录是可接受的代价，换来不用额外引入
    JWT 签名/SQLite 表这些复杂度。
    """

    def __init__(self) -> None:
        self._sessions: dict[str, float] = {}

    def create_session(self, *, ttl_seconds: int = 28800) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = time.time() + ttl_seconds
        return token

    def verify_session(self, token: str) -> bool:
        expires_at = self._sessions.get(token)
        if expires_at is None:
            return False
        if time.time() >= expires_at:
            del self._sessions[token]
            return False
        return True

    def revoke_session(self, token: str) -> None:
        self._sessions.pop(token, None)
