from datetime import datetime, timedelta

import aiosqlite

from app.memory.delayed_confirmation import (
    ensure_delayed_confirmation_schema,
    list_due_confirmations,
    mark_confirmed,
    schedule_delayed_confirmation,
)


async def test_list_due_confirmations_returns_only_due_and_unconfirmed():
    conn = await aiosqlite.connect(":memory:")
    await ensure_delayed_confirmation_schema(conn)
    now = datetime(2026, 8, 6, 10, 0, 0)

    due_id = await schedule_delayed_confirmation(
        conn, tenant_id="t1", user_id="u1", context="重启路由器试试",
        confirm_after=now - timedelta(hours=1),
    )
    await schedule_delayed_confirmation(
        conn, tenant_id="t1", user_id="u2", context="还没到期的",
        confirm_after=now + timedelta(hours=1),
    )

    due = await list_due_confirmations(conn, tenant_id="t1", now=now)

    assert len(due) == 1
    assert due[0]["id"] == due_id
    assert due[0]["context"] == "重启路由器试试"
    assert due[0]["user_id"] == "u1"


async def test_mark_confirmed_excludes_it_from_due_list():
    conn = await aiosqlite.connect(":memory:")
    await ensure_delayed_confirmation_schema(conn)
    now = datetime(2026, 8, 6, 10, 0, 0)

    confirmation_id = await schedule_delayed_confirmation(
        conn, tenant_id="t1", user_id="u1", context="重启路由器试试",
        confirm_after=now - timedelta(hours=1),
    )
    await mark_confirmed(conn, confirmation_id=confirmation_id, now=now)

    due = await list_due_confirmations(conn, tenant_id="t1", now=now)
    assert due == []


async def test_scoped_to_tenant():
    conn = await aiosqlite.connect(":memory:")
    await ensure_delayed_confirmation_schema(conn)
    now = datetime(2026, 8, 6, 10, 0, 0)

    await schedule_delayed_confirmation(
        conn, tenant_id="t2", user_id="u1", context="别的租户",
        confirm_after=now - timedelta(hours=1),
    )

    due = await list_due_confirmations(conn, tenant_id="t1", now=now)
    assert due == []
