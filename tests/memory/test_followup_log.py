from datetime import datetime, timedelta

import aiosqlite

from app.memory.followup_log import ensure_followup_log_schema, get_send_history, record_followup_sent


async def test_get_send_history_empty_when_nothing_recorded():
    conn = await aiosqlite.connect(":memory:")
    await ensure_followup_log_schema(conn)

    history = await get_send_history(
        conn, tenant_id="t1", customer_id="c1", since=datetime(2026, 1, 1)
    )

    assert history == []


async def test_record_followup_sent_makes_it_appear_in_send_history():
    conn = await aiosqlite.connect(":memory:")
    await ensure_followup_log_schema(conn)
    sent_at = datetime(2026, 8, 5, 10, 0, 0)

    await record_followup_sent(conn, tenant_id="t1", customer_id="c1", sent_at=sent_at)

    history = await get_send_history(
        conn, tenant_id="t1", customer_id="c1", since=sent_at - timedelta(days=1)
    )

    assert history == [sent_at]


async def test_get_send_history_excludes_records_before_since():
    conn = await aiosqlite.connect(":memory:")
    await ensure_followup_log_schema(conn)
    old_sent_at = datetime(2026, 8, 1, 10, 0, 0)

    await record_followup_sent(conn, tenant_id="t1", customer_id="c1", sent_at=old_sent_at)

    history = await get_send_history(
        conn, tenant_id="t1", customer_id="c1", since=datetime(2026, 8, 5, 0, 0, 0)
    )

    assert history == []


async def test_get_send_history_scoped_to_tenant_and_customer():
    conn = await aiosqlite.connect(":memory:")
    await ensure_followup_log_schema(conn)
    sent_at = datetime(2026, 8, 5, 10, 0, 0)

    await record_followup_sent(conn, tenant_id="t1", customer_id="c1", sent_at=sent_at)

    other_tenant = await get_send_history(
        conn, tenant_id="t2", customer_id="c1", since=sent_at - timedelta(days=1)
    )
    other_customer = await get_send_history(
        conn, tenant_id="t1", customer_id="c2", since=sent_at - timedelta(days=1)
    )

    assert other_tenant == []
    assert other_customer == []
