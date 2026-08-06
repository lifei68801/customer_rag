from datetime import datetime

import aiosqlite

from app.memory.ticket_fix_notifications import (
    ensure_ticket_fix_notifications_schema,
    is_already_notified,
    mark_notified,
)


async def test_is_already_notified_false_when_never_marked():
    conn = await aiosqlite.connect(":memory:")
    await ensure_ticket_fix_notifications_schema(conn)

    assert await is_already_notified(conn, ticket_id="tk1", fix_id="fx1") is False


async def test_mark_notified_then_is_already_notified_true():
    conn = await aiosqlite.connect(":memory:")
    await ensure_ticket_fix_notifications_schema(conn)

    await mark_notified(conn, ticket_id="tk1", fix_id="fx1", now=datetime(2026, 8, 1))

    assert await is_already_notified(conn, ticket_id="tk1", fix_id="fx1") is True


async def test_notification_is_scoped_to_the_specific_ticket_fix_pair():
    conn = await aiosqlite.connect(":memory:")
    await ensure_ticket_fix_notifications_schema(conn)

    await mark_notified(conn, ticket_id="tk1", fix_id="fx1", now=datetime(2026, 8, 1))

    assert await is_already_notified(conn, ticket_id="tk1", fix_id="fx2") is False
    assert await is_already_notified(conn, ticket_id="tk2", fix_id="fx1") is False
