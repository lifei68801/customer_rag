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


# ---- 会话属于哪张脸（ADR-0004 解耦之后）----
#
# spec 裁决补充「一个会话属于一个数字人」。persona == tenant 时这是免费的；
# 解耦之后要显式维护。**persona_id 在这里是「跟谁聊的」，不是「能看什么」**
# ——它不是权限判据（计划 Ruling P7-1）。


async def test_a_session_belongs_to_one_face():
    """会话记下它属于哪张脸。"""
    conn = await _connect()
    try:
        await touch_session(
            conn, tenant_id="muji", session_id="s1", user_id="u1",
            first_message="有无香料洗发水吗", now=datetime(2026, 9, 10, 10, 0, 0),
            persona_id="daogou",
        )

        rows = await list_sessions(conn, tenant_id="muji", user_id="u1", persona_id="daogou")
        assert [r["session_id"] for r in rows] == ["s1"]
    finally:
        await conn.close()


async def test_listing_sessions_is_filtered_by_face():
    """切到另一张脸，左栏列的是那张脸的会话。

    同一个租户下两张脸各聊各的——不过滤的话，用户切到「店务老张」会看到
    一屏跟小美聊的历史，而那些对话的语境完全不同。
    """
    conn = await _connect()
    try:
        for pid, sid in (("daogou", "s1"), ("dianwu", "s2")):
            await touch_session(
                conn, tenant_id="muji", session_id=sid, user_id="u1",
                first_message=f"问题 {sid}", now=datetime(2026, 9, 10, 10, 0, 0),
                persona_id=pid,
            )

        assert [r["session_id"] for r in await list_sessions(
            conn, tenant_id="muji", user_id="u1", persona_id="daogou")] == ["s1"]
        assert [r["session_id"] for r in await list_sessions(
            conn, tenant_id="muji", user_id="u1", persona_id="dianwu")] == ["s2"]
    finally:
        await conn.close()


async def test_existing_sessions_backfill_to_the_default_face():
    """这一列之前写进来的会话回填成 'default'。

    它们本来就属于那个租户唯一的那张脸——这是准确的回填，不是猜的。
    回填成 NULL 的话，每个读取方都要处理一个不存在的状态，而且左栏会
    整个空掉：按 default 过滤时一条都匹配不上。
    """
    conn = await aiosqlite.connect(":memory:")
    try:
        # 造一张"加列之前"的表
        await conn.execute(
            "CREATE TABLE chat_sessions (tenant_id TEXT NOT NULL, session_id TEXT NOT NULL,"
            " user_id TEXT NOT NULL, title TEXT NOT NULL,"
            " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
            " updated_at TEXT NOT NULL DEFAULT (datetime('now')),"
            " PRIMARY KEY (tenant_id, session_id))"
        )
        await conn.execute(
            "INSERT INTO chat_sessions (tenant_id, session_id, user_id, title)"
            " VALUES ('muji','old','u1','老会话')"
        )
        await conn.commit()

        await ensure_schema(conn)

        rows = await list_sessions(conn, tenant_id="muji", user_id="u1", persona_id="default")
        assert [r["session_id"] for r in rows] == ["old"], "存量会话必须落在 default 那张脸下"
    finally:
        await conn.close()


async def test_the_default_face_is_what_existing_callers_get():
    """不传 persona_id 的既有调用方读写的都是 default 那张脸。"""
    conn = await _connect()
    try:
        await touch_session(
            conn, tenant_id="muji", session_id="s1", user_id="u1",
            first_message="问题", now=datetime(2026, 9, 10, 10, 0, 0),
        )

        assert [r["session_id"] for r in await list_sessions(
            conn, tenant_id="muji", user_id="u1")] == ["s1"]
    finally:
        await conn.close()


async def test_a_session_does_not_move_to_another_face_mid_conversation():
    """一条会话跟谁聊的，在它被创建的那一刻就定了。

    touch_session 每轮对话都调一次。persona_id 进 DO UPDATE 的话，用户在
    对话进行中切了一次脸，这条会话会整个跳到另一张脸的历史里——他切回去
    就找不到刚才聊的东西了，而那些内容并没有消失，只是挂到了别处。
    """
    conn = await _connect()
    try:
        await touch_session(
            conn, tenant_id="muji", session_id="s1", user_id="u1",
            first_message="第一轮", now=datetime(2026, 9, 10, 10, 0, 0),
            persona_id="daogou",
        )
        # 同一条会话的第二轮，调用方传了另一张脸（切脸时的竞态，或者调用方写错）。
        await touch_session(
            conn, tenant_id="muji", session_id="s1", user_id="u1",
            first_message="第二轮", now=datetime(2026, 9, 10, 10, 5, 0),
            persona_id="dianwu",
        )

        assert [r["session_id"] for r in await list_sessions(
            conn, tenant_id="muji", user_id="u1", persona_id="daogou")] == ["s1"]
        assert await list_sessions(
            conn, tenant_id="muji", user_id="u1", persona_id="dianwu") == []
    finally:
        await conn.close()
