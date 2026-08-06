import aiosqlite

from app.config.settings import Settings
from app.memory.session_window_factory import build_session_window_store_from_settings
from app.memory.session_window_store import RedisSessionWindowStore, SQLiteSessionWindowStore


def _base_kwargs() -> dict:
    return dict(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=1024,
    )


async def test_defaults_to_sqlite_backend():
    conn = await aiosqlite.connect(":memory:")
    settings = Settings(**_base_kwargs())

    store = build_session_window_store_from_settings(settings, memory_conn=conn)

    assert isinstance(store, SQLiteSessionWindowStore)


async def test_uses_redis_backend_when_configured():
    conn = await aiosqlite.connect(":memory:")
    settings = Settings(
        **_base_kwargs(), session_window_backend="redis", redis_url="redis://localhost:6379/0"
    )

    store = build_session_window_store_from_settings(settings, memory_conn=conn)

    assert isinstance(store, RedisSessionWindowStore)


async def test_raises_immediately_when_redis_backend_missing_url():
    conn = await aiosqlite.connect(":memory:")
    settings = Settings(**_base_kwargs(), session_window_backend="redis", redis_url=None)

    try:
        build_session_window_store_from_settings(settings, memory_conn=conn)
        assert False, "应该在构建时就报错，而不是等到运行时"
    except ValueError:
        pass
