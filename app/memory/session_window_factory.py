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

    !!! 生产环境慎用 "redis" !!! 目前只有这个 store 的写入路径接入了
    memory_save_node（app/agent/graph.py）；读取路径——
    app/memory/context_injection.py 的 get_recent_turns 和
    app/memory/structured_recall.py 的 query_turns_in_window——都还在
    直接查 SQLite 的 conversation_turns 表，没有经过这层抽象。也就是说
    选了 "redis" 之后，新对话轮次只会写进 Redis，而两个读取路径永远查
    的是 SQLite 里那张不再更新的表：近期会话上下文和结构化历史检索会
    静默返回空结果，不会有任何异常或日志提示。这是刻意的分阶段迁移
    （读路径迁移留给后续任务），在读路径完成迁移之前，请勿在生产环境
    启用 "redis"。
    """
    if settings.session_window.backend == "redis":
        if not settings.redis_url:
            raise ValueError(
                "session_window_backend='redis' 时必须配置 redis_url"
            )
        import redis.asyncio as redis

        client = redis.from_url(settings.redis_url)
        return RedisSessionWindowStore(client)
    return SQLiteSessionWindowStore(memory_conn)
