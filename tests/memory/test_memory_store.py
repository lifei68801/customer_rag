import aiosqlite

from app.memory.memory_store import (
    append_history,
    list_active_memory_items,
    mark_deleted,
    upsert_memory_item,
)
from app.memory.schema import ensure_schema


async def _connect():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    return conn


async def test_upsert_then_list_returns_the_item():
    conn = await _connect()

    await upsert_memory_item(
        conn, memory_id="m1", user_id="u1", text="客户使用企业版套餐"
    )

    items = await list_active_memory_items(conn, user_id="u1")

    assert [i["text"] for i in items] == ["客户使用企业版套餐"]


async def test_upsert_same_id_twice_updates_text_not_duplicates():
    conn = await _connect()

    await upsert_memory_item(conn, memory_id="m1", user_id="u1", text="旧内容")
    await upsert_memory_item(conn, memory_id="m1", user_id="u1", text="新内容")

    items = await list_active_memory_items(conn, user_id="u1")

    assert len(items) == 1
    assert items[0]["text"] == "新内容"


async def test_mark_deleted_excludes_item_from_active_list():
    conn = await _connect()
    await upsert_memory_item(conn, memory_id="m1", user_id="u1", text="待删除")

    await mark_deleted(conn, memory_id="m1", user_id="u1")

    items = await list_active_memory_items(conn, user_id="u1")
    assert items == []


async def test_list_active_memory_items_scoped_to_user():
    conn = await _connect()
    await upsert_memory_item(conn, memory_id="m1", user_id="u1", text="属于u1")
    await upsert_memory_item(conn, memory_id="m2", user_id="u2", text="属于u2")

    items = await list_active_memory_items(conn, user_id="u1")

    assert [i["text"] for i in items] == ["属于u1"]


async def test_append_history_records_an_audit_row():
    conn = await _connect()

    await append_history(
        conn,
        memory_id="m1",
        user_id="u1",
        event="ADD",
        old_text=None,
        new_text="客户使用企业版套餐",
        reason="首次提及",
    )

    cursor = await conn.execute("SELECT event, new_text FROM memory_history")
    rows = await cursor.fetchall()
    assert rows == [("ADD", "客户使用企业版套餐")]
