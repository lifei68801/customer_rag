from datetime import datetime, timedelta

import aiosqlite

from app.agent.create_ticket_tool import (
    create_ticket,
    ensure_ticket_schema,
    list_pending_tickets_created_before,
    list_stale_pending_tickets,
    mark_ticket_notified,
)


async def test_create_ticket_returns_a_ticket_id():
    result = await create_ticket(
        tenant_id="t1",
        customer_id="c1",
        question="登录不了怎么办",
        reason="检索结果不足，需人工介入",
    )

    assert result.ticket_id
    assert result.reason == "检索结果不足，需人工介入"


async def test_create_ticket_without_conn_does_not_persist():
    # 不传 conn 时保持纯 mock 行为，不建表不写库——兼容没有工单持久化需求的调用方
    result = await create_ticket(
        tenant_id="t1", customer_id="c1", question="问题", reason="原因"
    )

    assert result.ticket_id


async def test_create_ticket_with_conn_persists_ticket_queryable_via_list_stale():
    conn = await aiosqlite.connect(":memory:")
    now = datetime(2026, 8, 5, 10, 0, 0)

    result = await create_ticket(
        tenant_id="t1",
        customer_id="c1",
        question="登录不了怎么办",
        reason="检索结果不足，需人工介入",
        conn=conn,
        now=now - timedelta(hours=100),
    )

    stale = await list_stale_pending_tickets(
        conn, tenant_id="t1", older_than_seconds=72 * 3600, now=now
    )

    assert len(stale) == 1
    assert stale[0]["ticket_id"] == result.ticket_id
    assert stale[0]["customer_id"] == "c1"
    assert stale[0]["question"] == "登录不了怎么办"


async def test_list_stale_pending_tickets_excludes_recent_tickets():
    conn = await aiosqlite.connect(":memory:")
    now = datetime(2026, 8, 5, 10, 0, 0)

    await create_ticket(
        tenant_id="t1",
        customer_id="c1",
        question="刚提交的问题",
        reason="原因",
        conn=conn,
        now=now - timedelta(hours=1),
    )

    stale = await list_stale_pending_tickets(
        conn, tenant_id="t1", older_than_seconds=72 * 3600, now=now
    )

    assert stale == []


async def test_list_stale_pending_tickets_excludes_already_notified_tickets():
    conn = await aiosqlite.connect(":memory:")
    now = datetime(2026, 8, 5, 10, 0, 0)

    result = await create_ticket(
        tenant_id="t1",
        customer_id="c1",
        question="问题",
        reason="原因",
        conn=conn,
        now=now - timedelta(hours=100),
    )
    await mark_ticket_notified(conn, result.ticket_id, now=now)

    stale = await list_stale_pending_tickets(
        conn, tenant_id="t1", older_than_seconds=72 * 3600, now=now
    )

    assert stale == []


async def test_list_stale_pending_tickets_scoped_to_tenant():
    conn = await aiosqlite.connect(":memory:")
    now = datetime(2026, 8, 5, 10, 0, 0)

    await create_ticket(
        tenant_id="t1",
        customer_id="c1",
        question="租户1的问题",
        reason="原因",
        conn=conn,
        now=now - timedelta(hours=100),
    )

    stale = await list_stale_pending_tickets(
        conn, tenant_id="t2", older_than_seconds=72 * 3600, now=now
    )

    assert stale == []


async def test_list_pending_tickets_created_before_a_timestamp():
    conn = await aiosqlite.connect(":memory:")
    fixed_at = datetime(2026, 8, 5, 0, 0, 0)

    old_ticket = await create_ticket(
        tenant_id="t1", customer_id="c1", question="旧问题",
        reason="原因", conn=conn, now=datetime(2026, 8, 1, 0, 0, 0),
    )
    await create_ticket(
        tenant_id="t1", customer_id="c2", question="新问题（修复之后才提的）",
        reason="原因", conn=conn, now=datetime(2026, 8, 6, 0, 0, 0),
    )

    results = await list_pending_tickets_created_before(conn, tenant_id="t1", before=fixed_at)

    assert len(results) == 1
    assert results[0]["ticket_id"] == old_ticket.ticket_id
