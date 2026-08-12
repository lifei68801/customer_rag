from datetime import datetime, timedelta

import aiosqlite

from app.memory.chat_sessions import delete_session, list_sessions, touch_session
from app.memory.schema import ensure_schema


async def _connect():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    return conn


async def test_touch_session_creates_row_with_title_from_first_message():
    conn = await _connect()

    await touch_session(
        conn,
        tenant_id="t1",
        session_id="s1",
        user_id="u1",
        first_message="网络连不上怎么办？",
        now=datetime(2026, 8, 12, 10, 0, 0),
    )

    sessions = await list_sessions(conn, tenant_id="t1", user_id="u1")
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "s1"
    assert sessions[0]["title"] == "网络连不上怎么办？"


async def test_touch_session_truncates_long_first_message():
    conn = await _connect()

    long_question = "这是一个非常非常非常非常非常非常非常非常非常非常非常非常长的问题，超过三十个字符了"
    await touch_session(
        conn,
        tenant_id="t1",
        session_id="s1",
        user_id="u1",
        first_message=long_question,
        now=datetime(2026, 8, 12, 10, 0, 0),
    )

    sessions = await list_sessions(conn, tenant_id="t1", user_id="u1")
    assert sessions[0]["title"] == long_question[:30] + "…"


async def test_touch_session_keeps_original_title_on_later_turns():
    conn = await _connect()

    await touch_session(
        conn, tenant_id="t1", session_id="s1", user_id="u1",
        first_message="第一句问题", now=datetime(2026, 8, 12, 10, 0, 0),
    )
    await touch_session(
        conn, tenant_id="t1", session_id="s1", user_id="u1",
        first_message="第二句问题（不应该覆盖标题）", now=datetime(2026, 8, 12, 10, 5, 0),
    )

    sessions = await list_sessions(conn, tenant_id="t1", user_id="u1")
    assert len(sessions) == 1
    assert sessions[0]["title"] == "第一句问题"


async def test_touch_session_bumps_updated_at_on_later_turns():
    conn = await _connect()

    await touch_session(
        conn, tenant_id="t1", session_id="s1", user_id="u1",
        first_message="问题", now=datetime(2026, 8, 12, 10, 0, 0),
    )
    await touch_session(
        conn, tenant_id="t1", session_id="s1", user_id="u1",
        first_message="问题", now=datetime(2026, 8, 12, 10, 5, 0),
    )

    sessions = await list_sessions(conn, tenant_id="t1", user_id="u1")
    assert sessions[0]["updated_at"] == "2026-08-12 10:05:00"


async def test_list_sessions_orders_by_most_recently_active_first():
    conn = await _connect()

    await touch_session(
        conn, tenant_id="t1", session_id="old", user_id="u1",
        first_message="旧会话", now=datetime(2026, 8, 12, 9, 0, 0),
    )
    await touch_session(
        conn, tenant_id="t1", session_id="new", user_id="u1",
        first_message="新会话", now=datetime(2026, 8, 12, 11, 0, 0),
    )

    sessions = await list_sessions(conn, tenant_id="t1", user_id="u1")
    assert [s["session_id"] for s in sessions] == ["new", "old"]


async def test_list_sessions_only_returns_matching_tenant_and_user():
    conn = await _connect()
    now = datetime(2026, 8, 12, 10, 0, 0)

    await touch_session(
        conn, tenant_id="t1", session_id="s1", user_id="u1",
        first_message="A", now=now,
    )
    await touch_session(
        conn, tenant_id="t2", session_id="s2", user_id="u1",
        first_message="B", now=now,
    )
    await touch_session(
        conn, tenant_id="t1", session_id="s3", user_id="u2",
        first_message="C", now=now,
    )

    sessions = await list_sessions(conn, tenant_id="t1", user_id="u1")
    assert [s["session_id"] for s in sessions] == ["s1"]


async def test_delete_session_removes_metadata_and_turns():
    conn = await _connect()
    from app.memory.session_window import append_turn, get_recent_turns

    now = datetime(2026, 8, 12, 10, 0, 0)
    await touch_session(
        conn, tenant_id="t1", session_id="s1", user_id="u1",
        first_message="问题", now=now,
    )
    await append_turn(
        conn, tenant_id="t1", session_id="s1", user_id="u1",
        role="user", content="问题",
    )

    deleted = await delete_session(conn, tenant_id="t1", user_id="u1", session_id="s1")

    assert deleted is True
    assert await list_sessions(conn, tenant_id="t1", user_id="u1") == []
    assert await get_recent_turns(conn, tenant_id="t1", session_id="s1", limit=10) == []


async def test_delete_session_returns_false_when_not_found():
    conn = await _connect()

    deleted = await delete_session(conn, tenant_id="t1", user_id="u1", session_id="missing")

    assert deleted is False


async def test_delete_session_cannot_delete_another_users_session():
    conn = await _connect()
    now = datetime(2026, 8, 12, 10, 0, 0)
    await touch_session(
        conn, tenant_id="t1", session_id="s1", user_id="owner",
        first_message="问题", now=now,
    )

    deleted = await delete_session(conn, tenant_id="t1", user_id="attacker", session_id="s1")

    assert deleted is False
    sessions = await list_sessions(conn, tenant_id="t1", user_id="owner")
    assert len(sessions) == 1
