import aiosqlite
import pytest
from pydantic import ValidationError

from app.memory.session_window_factory import build_session_window_store_from_settings
from app.memory.session_window_store import RedisSessionWindowStore, SQLiteSessionWindowStore
from tests.settings_factory import build_settings


async def test_defaults_to_sqlite_backend():
    conn = await aiosqlite.connect(":memory:")
    settings = build_settings()

    store = build_session_window_store_from_settings(settings, memory_conn=conn)

    assert isinstance(store, SQLiteSessionWindowStore)


async def test_uses_redis_backend_when_configured():
    conn = await aiosqlite.connect(":memory:")
    settings = build_settings(session_window_backend="redis", redis_url="redis://localhost:6379/0")

    store = build_session_window_store_from_settings(settings, memory_conn=conn)

    assert isinstance(store, RedisSessionWindowStore)


async def test_raises_immediately_when_redis_backend_missing_url():
    conn = await aiosqlite.connect(":memory:")
    settings = build_settings(session_window_backend="redis", redis_url=None)

    try:
        build_session_window_store_from_settings(settings, memory_conn=conn)
        assert False, "应该在构建时就报错，而不是等到运行时"
    except ValueError:
        pass


def test_rejects_unrecognized_session_window_backend_at_settings_construction():
    """回归测试 Finding 7：session_window_backend 之前是裸 str 类型，拼写
    错误（比如 "Redis"/"redsi"）不会在 Settings 构造时报错，而是被
    session_window_factory.py 里精确匹配 "redis" 的 if 分支悄悄当成
    "非 redis"，静默退化成 sqlite 默认行为——本该在启动时就暴露的配置
    错误被吞掉了。改成 Literal["sqlite", "redis"] 后，pydantic 应该在
    构造 SessionWindowSettings 时就校验失败（ValidationError 从
    build_settings() 内部的子模型构造往上抛，跟改造前直接构造 Settings()
    报的是同一种异常类型）。"""
    with pytest.raises(ValidationError):
        build_settings(session_window_backend="redsi")
