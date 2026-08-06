from __future__ import annotations

import aiosqlite

from app.config.settings import Settings
from app.memory.session_window_store import (
    RedisSessionWindowStore,
    SessionWindowStore,
    SQLiteSessionWindowStore,
)


def build_session_window_store_from_settings(
    settings: Settings, *, memory_conn: aiosqlite.Connection
) -> SessionWindowStore:
    """session_window_backend="redis" 时需要 redis_url，缺失就立即报错
    （构建时失败，不拖到运行时某次 append_turn 才暴露配置问题）；
    默认（或任何非 "redis" 的值）走 SQLiteSessionWindowStore，复用同一个
    memory_conn，不引入额外连接。
    """
    if settings.session_window_backend == "redis":
        if not settings.redis_url:
            raise ValueError(
                "session_window_backend='redis' 时必须配置 redis_url"
            )
        import redis.asyncio as redis

        client = redis.from_url(settings.redis_url)
        return RedisSessionWindowStore(client)
    return SQLiteSessionWindowStore(memory_conn)
