from datetime import datetime, timedelta

import aiosqlite

from app.memory.clarification import (
    clear_pending_clarification,
    ensure_clarification_schema,
    get_pending_clarification,
    looks_like_a_time_reply,
    merge_clarification_reply,
    set_pending_clarification,
)


async def _connect():
    conn = await aiosqlite.connect(":memory:")
    await ensure_clarification_schema(conn)
    return conn


async def test_set_then_get_returns_the_pending_clarification():
    conn = await _connect()

    await set_pending_clarification(
        conn,
        tenant_id="t1",
        session_id="s1",
        original_question="上次那个报错",
        clarification_prompt="您想查询哪一天的记录？",
        now=datetime(2026, 8, 5, 10, 0, 0),
    )

    pending = await get_pending_clarification(
        conn, tenant_id="t1", session_id="s1", now=datetime(2026, 8, 5, 10, 1, 0)
    )

    assert pending is not None
    assert pending["original_question"] == "上次那个报错"
    assert pending["clarification_prompt"] == "您想查询哪一天的记录？"


async def test_get_returns_none_when_expired():
    conn = await _connect()
    await set_pending_clarification(
        conn,
        tenant_id="t1",
        session_id="s1",
        original_question="上次那个报错",
        clarification_prompt="您想查询哪一天的记录？",
        now=datetime(2026, 8, 5, 10, 0, 0),
        ttl_seconds=300,
    )

    pending = await get_pending_clarification(
        conn,
        tenant_id="t1",
        session_id="s1",
        now=datetime(2026, 8, 5, 10, 0, 0) + timedelta(seconds=301),
    )

    assert pending is None


async def test_get_returns_none_when_no_pending_clarification():
    conn = await _connect()

    pending = await get_pending_clarification(
        conn, tenant_id="t1", session_id="s1", now=datetime(2026, 8, 5, 10, 0, 0)
    )

    assert pending is None


async def test_set_overwrites_previous_pending_clarification_for_the_same_session():
    conn = await _connect()
    await set_pending_clarification(
        conn,
        tenant_id="t1",
        session_id="s1",
        original_question="第一个问题",
        clarification_prompt="第一次追问",
        now=datetime(2026, 8, 5, 10, 0, 0),
    )
    await set_pending_clarification(
        conn,
        tenant_id="t1",
        session_id="s1",
        original_question="第二个问题",
        clarification_prompt="第二次追问",
        now=datetime(2026, 8, 5, 10, 1, 0),
    )

    pending = await get_pending_clarification(
        conn, tenant_id="t1", session_id="s1", now=datetime(2026, 8, 5, 10, 2, 0)
    )

    assert pending["original_question"] == "第二个问题"


async def test_clear_removes_the_pending_clarification():
    conn = await _connect()
    await set_pending_clarification(
        conn,
        tenant_id="t1",
        session_id="s1",
        original_question="上次那个报错",
        clarification_prompt="您想查询哪一天的记录？",
        now=datetime(2026, 8, 5, 10, 0, 0),
    )

    await clear_pending_clarification(conn, tenant_id="t1", session_id="s1")

    pending = await get_pending_clarification(
        conn, tenant_id="t1", session_id="s1", now=datetime(2026, 8, 5, 10, 1, 0)
    )
    assert pending is None


def test_looks_like_a_time_reply_matches_short_date_expressions():
    assert looks_like_a_time_reply("上周五") is True
    assert looks_like_a_time_reply("昨天") is True
    assert looks_like_a_time_reply("8月3号") is True


def test_looks_like_a_time_reply_rejects_full_new_questions():
    assert looks_like_a_time_reply("我想问一下另外一个完全不相关的产品问题") is False
    assert looks_like_a_time_reply("网络连不上怎么办？") is False


def test_merge_clarification_reply_combines_original_question_and_reply():
    merged = merge_clarification_reply(
        original_question="上次那个报错", reply_text="上周五"
    )
    assert "上次那个报错" in merged
    assert "上周五" in merged
