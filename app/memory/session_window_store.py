from __future__ import annotations

from typing import Any, Protocol

import aiosqlite

from app.memory.session_window import append_turn, get_recent_turns


class SessionWindowStore(Protocol):
    """协议定义会话窗口存储接口，支持多个后端实现（SQLite、Redis 等）。"""

    async def append_turn(
        self, *, tenant_id: str, session_id: str, user_id: str, role: str, content: str
    ) -> None:
        """追加一条对话轮次。"""
        ...

    async def get_recent_turns(
        self, *, tenant_id: str, session_id: str, limit: int
    ) -> list[dict[str, Any]]:
        """获取最近的 limit 条对话轮次，按时间正序排列。"""
        ...


class SQLiteSessionWindowStore:
    """薄封装现有 app/memory/session_window.py 自由函数的 SQLite 实现。
    零行为变化——仅代理调用，默认实现，不配置 Redis 时走这条路径。
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def append_turn(
        self, *, tenant_id: str, session_id: str, user_id: str, role: str, content: str
    ) -> None:
        await append_turn(
            self._conn,
            tenant_id=tenant_id,
            session_id=session_id,
            user_id=user_id,
            role=role,
            content=content,
        )

    async def get_recent_turns(
        self, *, tenant_id: str, session_id: str, limit: int
    ) -> list[dict[str, Any]]:
        return await get_recent_turns(
            self._conn, tenant_id=tenant_id, session_id=session_id, limit=limit
        )
