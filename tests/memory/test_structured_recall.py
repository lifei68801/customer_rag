from datetime import datetime

import aiosqlite

from app.memory.schema import ensure_schema
from app.memory.structured_recall import query_turns_in_window


async def _insert_turn(conn, *, tenant_id, session_id, user_id, role, content, created_at):
    await conn.execute(
        "INSERT INTO conversation_turns (tenant_id, session_id, user_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tenant_id, session_id, user_id, role, content, created_at),
    )
    await conn.commit()


async def test_finds_turns_within_window_across_sessions():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    await _insert_turn(
        conn, tenant_id="t1", session_id="s1", user_id="u1", role="user",
        content="上周的问题A", created_at="2026-07-28 10:00:00",
    )
    await _insert_turn(
        conn, tenant_id="t1", session_id="s2", user_id="u1", role="user",
        content="另一个会话里上周的问题B", created_at="2026-07-29 10:00:00",
    )
    await _insert_turn(
        conn, tenant_id="t1", session_id="s1", user_id="u1", role="user",
        content="太早之前的问题", created_at="2026-07-01 10:00:00",
    )

    results = await query_turns_in_window(
        conn, tenant_id="t1", user_id="u1",
        start=datetime(2026, 7, 27), end=datetime(2026, 8, 1),
    )

    contents = {row["content"] for row in results}
    assert contents == {"上周的问题A", "另一个会话里上周的问题B"}


async def test_excludes_other_tenant_and_other_user():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    await _insert_turn(
        conn, tenant_id="t2", session_id="s1", user_id="u1", role="user",
        content="别的租户的问题", created_at="2026-07-28 10:00:00",
    )
    await _insert_turn(
        conn, tenant_id="t1", session_id="s1", user_id="u2", role="user",
        content="别的用户的问题", created_at="2026-07-28 10:00:00",
    )

    results = await query_turns_in_window(
        conn, tenant_id="t1", user_id="u1",
        start=datetime(2026, 7, 27), end=datetime(2026, 8, 1),
    )

    assert results == []


async def test_returns_empty_list_when_nothing_in_window():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)

    results = await query_turns_in_window(
        conn, tenant_id="t1", user_id="u1",
        start=datetime(2026, 7, 27), end=datetime(2026, 8, 1),
    )

    assert results == []
