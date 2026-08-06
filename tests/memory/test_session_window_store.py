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
