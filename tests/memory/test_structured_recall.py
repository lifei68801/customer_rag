from datetime import datetime, timedelta, timezone

import aiosqlite

from app.memory.schema import ensure_schema
from app.memory.session_window import append_turn
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


from app.memory.structured_recall import search_turns_by_keyword_and_window


async def test_keyword_filter_narrows_down_window_results():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    await _insert_turn(
        conn, tenant_id="t1", session_id="s1", user_id="u1", role="user",
        content="错误码E502网关超时怎么解决", created_at="2026-07-28 10:00:00",
    )
    await _insert_turn(
        conn, tenant_id="t1", session_id="s2", user_id="u1", role="user",
        content="账号密码忘记了怎么办", created_at="2026-07-29 10:00:00",
    )

    results = await search_turns_by_keyword_and_window(
        conn, tenant_id="t1", user_id="u1",
        start=datetime(2026, 7, 27), end=datetime(2026, 8, 1),
        question="E502网关超时", top_k=5,
    )

    assert len(results) == 1
    assert "E502" in results[0]["content"]


async def test_finds_turn_saved_via_append_turn_in_local_today_window():
    """回归测试 Finding 1（UTC vs 本地时间 bug）：conversation_turns.created_at
    是 SQLite `datetime('now')` 默认值，即真实的 UTC 时间戳（这里特意走
    真正的 append_turn，不手写 created_at 字符串，确保测的是真实存库行为）。

    查询窗口按 resolve_time_window 同款语义构造：naive **本地**时间边界
    （见 app/agent/graph.py 的 reference_time=datetime.now()）。窗口边界
    不是直接取 datetime.now() 截断到当天——那样构造出的窗口只在本地时刻
    和 UTC 时刻恰好落在同一个公历日期时才会"意外"通过（比如本地 UTC+8
    时区、当前是本地下午/晚上时，本地日期和 UTC 日期相同，不转换也凑巧
    对得上，无法暴露 bug；只有本地凌晨 0-8 点运行时两个日期才不同，才会
    真正测到转换逻辑）。为了让这个回归测试不管在墙钟哪个时刻跑都能可靠
    捕获这个 bug，这里改成：先读出刚插入这条记录时 SQLite 实际写入的
    UTC created_at，反推出"如果这条记录是刚刚在本地时间发生的，本地时刻
    应该是几点"（把 UTC 时间戳按当前系统时区转换成本地时间），再拿这个
    本地时刻前后一分钟当查询窗口。这样任意时刻运行，不做 UTC 转换的旧
    实现都会因为直接拿本地时间字符串去比 UTC 存库值而系统性差了一个时区
    偏移量、查不到这条记录；做了转换的新实现则总能查到。
    """
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    await append_turn(
        conn, tenant_id="t1", session_id="s1", user_id="u1",
        role="user", content="今天的问题",
    )

    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT created_at FROM conversation_turns WHERE session_id = 's1'"
    )
    row = await cursor.fetchone()
    stored_utc = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    # 转成 naive 本地时间——和 resolve_time_window 产出的窗口边界同一种
    # "naive 代表本地时间"的语义。
    local_equivalent = stored_utc.astimezone().replace(tzinfo=None)

    start = local_equivalent - timedelta(minutes=1)
    end = local_equivalent + timedelta(minutes=1)

    results = await query_turns_in_window(
        conn, tenant_id="t1", user_id="u1", start=start, end=end,
    )

    assert [row["content"] for row in results] == ["今天的问题"]


async def test_keyword_filter_returns_empty_when_window_empty():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)

    results = await search_turns_by_keyword_and_window(
        conn, tenant_id="t1", user_id="u1",
        start=datetime(2026, 7, 27), end=datetime(2026, 8, 1),
        question="任意问题", top_k=5,
    )

    assert results == []
