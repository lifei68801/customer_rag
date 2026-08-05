import aiosqlite

from app.memory.context_injection import inject_memory_context
from app.memory.memory_store import upsert_memory_item
from app.memory.schema import ensure_schema
from app.memory.session_window import append_turn


async def _connect():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    return conn


async def test_injects_recent_turns_and_active_memory_items():
    conn = await _connect()
    await append_turn(
        conn, tenant_id="t1", session_id="s1", user_id="u1", role="user", content="你好"
    )
    await append_turn(
        conn,
        tenant_id="t1",
        session_id="s1",
        user_id="u1",
        role="assistant",
        content="您好",
    )
    await upsert_memory_item(
        conn, memory_id="m1", tenant_id="t1", user_id="u1", text="客户使用企业版套餐"
    )

    messages = await inject_memory_context(
        conn, tenant_id="t1", session_id="s1", user_id="u1", recent_turn_limit=10
    )

    assert any("客户使用企业版套餐" in m["content"] for m in messages if m["role"] == "system")
    assert {"role": "user", "content": "你好"} in [
        {"role": m["role"], "content": m["content"]} for m in messages
    ]


async def test_compacts_turns_beyond_preserve_limit():
    conn = await _connect()
    for i in range(10):
        await append_turn(
            conn,
            tenant_id="t1",
            session_id="s1",
            user_id="u1",
            role="user",
            content=f"msg{i}",
        )

    messages = await inject_memory_context(
        conn,
        tenant_id="t1",
        session_id="s1",
        user_id="u1",
        recent_turn_limit=10,
        compaction_preserve_recent_messages=4,
    )

    turn_messages = [m for m in messages if m["role"] == "user"]
    assert len(turn_messages) == 4
    assert any("会话摘要" in m["content"] for m in messages if m["role"] == "system")
