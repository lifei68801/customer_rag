import aiosqlite

from app.memory.schema import ensure_schema
from app.memory.session_window import append_turn, get_recent_turns


async def _connect(tmp_path):
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    return conn


async def test_get_recent_turns_returns_appended_turns_in_order(tmp_path):
    conn = await _connect(tmp_path)

    await append_turn(
        conn,
        tenant_id="t1",
        session_id="s1",
        user_id="u1",
        role="user",
        content="你好",
    )
    await append_turn(
        conn,
        tenant_id="t1",
        session_id="s1",
        user_id="u1",
        role="assistant",
        content="有什么可以帮您？",
    )

    turns = await get_recent_turns(conn, tenant_id="t1", session_id="s1", limit=10)

    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert [t["content"] for t in turns] == ["你好", "有什么可以帮您？"]


async def test_get_recent_turns_only_returns_matching_session(tmp_path):
    conn = await _connect(tmp_path)

    await append_turn(
        conn, tenant_id="t1", session_id="s1", user_id="u1", role="user", content="A"
    )
    await append_turn(
        conn, tenant_id="t1", session_id="s2", user_id="u1", role="user", content="B"
    )

    turns = await get_recent_turns(conn, tenant_id="t1", session_id="s1", limit=10)

    assert [t["content"] for t in turns] == ["A"]


async def test_get_recent_turns_respects_limit_keeping_most_recent(tmp_path):
    conn = await _connect(tmp_path)

    for i in range(5):
        await append_turn(
            conn,
            tenant_id="t1",
            session_id="s1",
            user_id="u1",
            role="user",
            content=f"msg{i}",
        )

    turns = await get_recent_turns(conn, tenant_id="t1", session_id="s1", limit=2)

    assert [t["content"] for t in turns] == ["msg3", "msg4"]


async def test_get_recent_turns_does_not_return_another_tenants_turns(tmp_path):
    conn = await _connect(tmp_path)

    await append_turn(
        conn, tenant_id="t1", session_id="s1", user_id="u1", role="user", content="A"
    )
    await append_turn(
        conn, tenant_id="t2", session_id="s1", user_id="u1", role="user", content="B"
    )

    turns = await get_recent_turns(conn, tenant_id="t1", session_id="s1", limit=10)

    assert [t["content"] for t in turns] == ["A"]
