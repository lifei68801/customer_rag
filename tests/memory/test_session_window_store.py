import aiosqlite

from app.memory.schema import ensure_schema
from app.memory.session_window_store import SQLiteSessionWindowStore


async def test_sqlite_store_appends_and_reads_back_turns():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    store = SQLiteSessionWindowStore(conn)

    await store.append_turn(tenant_id="t1", session_id="s1", user_id="u1", role="user", content="你好")
    await store.append_turn(tenant_id="t1", session_id="s1", user_id="u1", role="assistant", content="您好，有什么可以帮您")

    turns = await store.get_recent_turns(tenant_id="t1", session_id="s1", limit=10)

    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["content"] == "你好"


async def test_sqlite_store_scoped_to_session():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    store = SQLiteSessionWindowStore(conn)

    await store.append_turn(tenant_id="t1", session_id="s1", user_id="u1", role="user", content="会话1")
    await store.append_turn(tenant_id="t1", session_id="s2", user_id="u1", role="user", content="会话2")

    turns = await store.get_recent_turns(tenant_id="t1", session_id="s1", limit=10)

    assert len(turns) == 1
    assert turns[0]["content"] == "会话1"


from app.memory.session_window_store import RedisSessionWindowStore


class FakeRedisClient:
    """纯 Python 字典实现的假 Redis 客户端，只实现本次用到的 4 个命令，
    不需要真实 Redis 服务。"""

    def __init__(self) -> None:
        self._lists: dict[str, list[str]] = {}
        self.expire_calls: list[tuple[str, int]] = []

    async def rpush(self, key: str, value: str) -> None:
        self._lists.setdefault(key, []).append(value)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        values = self._lists.get(key, [])
        # Redis LTRIM 语义：end=-1 表示到末尾，start 为负数表示从末尾数
        length = len(values)
        normalized_start = start if start >= 0 else max(length + start, 0)
        normalized_end = length - 1 if end == -1 else (end if end >= 0 else length + end)
        self._lists[key] = values[normalized_start : normalized_end + 1]

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        values = self._lists.get(key, [])
        if end == -1:
            return values[start:]
        return values[start : end + 1]

    async def expire(self, key: str, ttl_seconds: int) -> None:
        self.expire_calls.append((key, ttl_seconds))


async def test_redis_store_appends_and_reads_back_turns():
    client = FakeRedisClient()
    store = RedisSessionWindowStore(client, max_turns=50, ttl_seconds=86400)

    await store.append_turn(tenant_id="t1", session_id="s1", user_id="u1", role="user", content="你好")
    await store.append_turn(tenant_id="t1", session_id="s1", user_id="u1", role="assistant", content="您好")

    turns = await store.get_recent_turns(tenant_id="t1", session_id="s1", limit=10)

    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["content"] == "你好"
    assert client.expire_calls[-1] == ("session_turns:t1:s1", 86400)


async def test_redis_store_trims_to_max_turns():
    client = FakeRedisClient()
    store = RedisSessionWindowStore(client, max_turns=2, ttl_seconds=86400)

    for i in range(5):
        await store.append_turn(
            tenant_id="t1", session_id="s1", user_id="u1", role="user", content=f"消息{i}"
        )

    turns = await store.get_recent_turns(tenant_id="t1", session_id="s1", limit=10)

    assert len(turns) == 2
    assert [t["content"] for t in turns] == ["消息3", "消息4"]


async def test_redis_store_respects_get_recent_turns_limit():
    client = FakeRedisClient()
    store = RedisSessionWindowStore(client, max_turns=50, ttl_seconds=86400)

    for i in range(5):
        await store.append_turn(
            tenant_id="t1", session_id="s1", user_id="u1", role="user", content=f"消息{i}"
        )

    turns = await store.get_recent_turns(tenant_id="t1", session_id="s1", limit=2)

    assert [t["content"] for t in turns] == ["消息3", "消息4"]
