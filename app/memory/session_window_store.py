from __future__ import annotations

import json
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


class RedisClientProtocol(Protocol):
    async def rpush(self, key: str, value: str) -> None: ...
    async def ltrim(self, key: str, start: int, end: int) -> None: ...
    async def lrange(self, key: str, start: int, end: int) -> list[str]: ...
    async def expire(self, key: str, ttl_seconds: int) -> None: ...


class RedisSessionWindowStore:
    """会话滑窗 Redis 实现：key = f"session_turns:{tenant_id}:{session_id}"，
    每条轮次序列化为 JSON 存进一个 Redis List，RPUSH 追加 + LTRIM 只保留
    最近 max_turns 条 + EXPIRE 每次写入都刷新滑动过期时间。
    """

    def __init__(
        self, redis_client: RedisClientProtocol, *, max_turns: int = 50, ttl_seconds: int = 86400
    ) -> None:
        self._client = redis_client
        self._max_turns = max_turns
        self._ttl_seconds = ttl_seconds

    def _key(self, *, tenant_id: str, session_id: str) -> str:
        return f"session_turns:{tenant_id}:{session_id}"

    async def append_turn(
        self, *, tenant_id: str, session_id: str, user_id: str, role: str, content: str
    ) -> None:
        key = self._key(tenant_id=tenant_id, session_id=session_id)
        payload = json.dumps({"role": role, "content": content})
        await self._client.rpush(key, payload)
        await self._client.ltrim(key, -self._max_turns, -1)
        await self._client.expire(key, self._ttl_seconds)

    async def get_recent_turns(
        self, *, tenant_id: str, session_id: str, limit: int
    ) -> list[dict[str, Any]]:
        # limit=0 时 -limit 也是 0（不是负数），直接传给 lrange(key, 0, -1)
        # 会返回整个存储窗口，而不是零条——和 SQLiteSessionWindowStore 底层
        # `LIMIT 0` 的语义（正确返回零行）不一致。这里显式短路，保证两个
        # 实现在 limit<=0 时行为一致。
        if limit <= 0:
            return []
        key = self._key(tenant_id=tenant_id, session_id=session_id)
        raw_values = await self._client.lrange(key, -limit, -1)
        return [json.loads(value) for value in raw_values]
