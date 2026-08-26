import aiosqlite

from app.memory.action_executor import apply_memory_actions
from app.memory.memory_store import list_active_memory_items, upsert_memory_item
from app.memory.schema import ensure_schema
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult


async def _connect():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    return conn


async def test_add_action_creates_a_new_memory_item():
    conn = await _connect()

    applied = await apply_memory_actions(
        conn,
        tenant_id="t1",
        user_id="u1",
        actions=[{"event": "ADD", "memory_id": "", "text": "客户使用企业版套餐", "reason": ""}],
    )

    items = await list_active_memory_items(conn, tenant_id="t1", user_id="u1")
    assert [i["text"] for i in items] == ["客户使用企业版套餐"]
    assert applied[0]["event"] == "ADD"
    assert applied[0]["memory_id"]


async def test_update_action_overwrites_existing_text():
    conn = await _connect()
    await upsert_memory_item(
        conn, memory_id="m1", tenant_id="t1", user_id="u1", text="旧内容"
    )

    await apply_memory_actions(
        conn,
        tenant_id="t1",
        user_id="u1",
        actions=[
            {"event": "UPDATE", "memory_id": "m1", "text": "新内容", "reason": ""}
        ],
    )

    items = await list_active_memory_items(conn, tenant_id="t1", user_id="u1")
    assert [i["text"] for i in items] == ["新内容"]


async def test_delete_action_removes_item_from_active_list():
    conn = await _connect()
    await upsert_memory_item(
        conn, memory_id="m1", tenant_id="t1", user_id="u1", text="待删除"
    )

    await apply_memory_actions(
        conn,
        tenant_id="t1",
        user_id="u1",
        actions=[{"event": "DELETE", "memory_id": "m1", "text": "", "reason": ""}],
    )

    items = await list_active_memory_items(conn, tenant_id="t1", user_id="u1")
    assert items == []


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


async def test_add_action_embeds_text_when_embedding_registry_provided():
    conn = await _connect()
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())

    await apply_memory_actions(
        conn,
        tenant_id="t1",
        user_id="u1",
        actions=[{"event": "ADD", "memory_id": "", "text": "客户使用企业版套餐", "reason": ""}],
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
    )

    items = await list_active_memory_items(conn, tenant_id="t1", user_id="u1")
    assert items[0]["embedding"] == [1.0, 0.0]


async def test_add_action_without_embedding_registry_leaves_embedding_none():
    conn = await _connect()

    await apply_memory_actions(
        conn,
        tenant_id="t1",
        user_id="u1",
        actions=[{"event": "ADD", "memory_id": "", "text": "客户使用企业版套餐", "reason": ""}],
    )

    items = await list_active_memory_items(conn, tenant_id="t1", user_id="u1")
    assert items[0]["embedding"] is None


async def test_none_action_does_not_write_anything():
    conn = await _connect()

    applied = await apply_memory_actions(
        conn,
        tenant_id="t1",
        user_id="u1",
        actions=[{"event": "NONE", "memory_id": "", "text": "重复内容", "reason": ""}],
    )

    items = await list_active_memory_items(conn, tenant_id="t1", user_id="u1")
    assert items == []
    assert applied == []


async def test_update_action_records_old_text_in_history():
    conn = await _connect()
    await upsert_memory_item(
        conn, memory_id="m1", tenant_id="t1", user_id="u1", text="旧内容"
    )

    await apply_memory_actions(
        conn, tenant_id="t1", user_id="u1",
        actions=[
            {
                "event": "UPDATE", "memory_id": "m1", "text": "新内容",
                "reason": "套餐变更", "conflict_type": "value",
            }
        ],
    )

    cursor = await conn.execute(
        "SELECT old_text, new_text, conflict_type FROM memory_history WHERE memory_id = ?",
        ("m1",),
    )
    row = await cursor.fetchone()
    assert row[0] == "旧内容"
    assert row[1] == "新内容"
    assert row[2] == "value"


async def test_delete_action_records_old_text_in_history():
    conn = await _connect()
    await upsert_memory_item(
        conn, memory_id="m1", tenant_id="t1", user_id="u1", text="待删除内容"
    )

    await apply_memory_actions(
        conn, tenant_id="t1", user_id="u1",
        actions=[
            {
                "event": "DELETE", "memory_id": "m1", "text": "",
                "reason": "客户明确否认", "conflict_type": "logical",
            }
        ],
    )

    cursor = await conn.execute(
        "SELECT old_text, new_text, conflict_type FROM memory_history WHERE memory_id = ?",
        ("m1",),
    )
    row = await cursor.fetchone()
    assert row[0] == "待删除内容"
    assert row[1] is None
    assert row[2] == "logical"


async def test_update_action_for_unknown_memory_id_records_none_old_text():
    """UPDATE 一个不存在的 memory_id（理论上不应该发生，但防御性验证）——
    查不到当前值时 old_text 应该是 None，不应该抛异常。"""
    conn = await _connect()

    await apply_memory_actions(
        conn, tenant_id="t1", user_id="u1",
        actions=[
            {"event": "UPDATE", "memory_id": "unknown", "text": "新内容", "reason": "r"}
        ],
    )

    cursor = await conn.execute(
        "SELECT old_text FROM memory_history WHERE memory_id = ?", ("unknown",)
    )
    row = await cursor.fetchone()
    assert row[0] is None


async def test_add_action_records_conflict_type_none_by_default():
    conn = await _connect()

    await apply_memory_actions(
        conn, tenant_id="t1", user_id="u1",
        actions=[{"event": "ADD", "memory_id": "", "text": "新事实", "reason": ""}],
    )

    cursor = await conn.execute(
        "SELECT conflict_type FROM memory_history WHERE new_text = ?", ("新事实",)
    )
    row = await cursor.fetchone()
    assert row[0] is None
