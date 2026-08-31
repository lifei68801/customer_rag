from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.term_edits_store import (
    FIELD_CREATED,
    FIELD_DELETED,
    delete_term_edit,
    ensure_term_edits_schema,
    list_term_edits,
    list_term_edits_for_node_key,
    upsert_term_edit,
)

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_term_edits_schema(conn)
    return conn


async def test_upsert_and_read_back_a_field_edit():
    conn = await _conn()

    await upsert_term_edit(
        conn, tenant_id="t1", node_key="产品:A", field="standard_name",
        value="人工改过的名字", edited_by="alice",
    )

    assert await list_term_edits(conn, "t1") == {
        "产品:A": {"standard_name": "人工改过的名字"}
    }


async def test_upsert_twice_on_the_same_field_keeps_only_the_last_value():
    """term_edits 保存的是当前编辑状态，不是 append-only 日志——同一个
    (node_key, field) 改两次只剩最后一次。见 spec 的非目标。"""
    conn = await _conn()

    await upsert_term_edit(
        conn, tenant_id="t1", node_key="产品:A", field="standard_name",
        value="第一次", edited_by="alice",
    )
    await upsert_term_edit(
        conn, tenant_id="t1", node_key="产品:A", field="standard_name",
        value="第二次", edited_by="bob",
    )

    assert await list_term_edits(conn, "t1") == {"产品:A": {"standard_name": "第二次"}}


async def test_edits_round_trip_lists_and_dicts_without_losing_types():
    """value 是 JSON 文本。aliases 是列表、extra_properties.<name> 可能是
    数值——存进去再读出来必须还是原来的类型，不能变成字符串。"""
    conn = await _conn()

    await upsert_term_edit(
        conn, tenant_id="t1", node_key="产品:A", field="aliases",
        value=["别名一", "别名二"], edited_by="alice",
    )
    await upsert_term_edit(
        conn, tenant_id="t1", node_key="产品:A", field="extra_properties.revenue",
        value=1234.5, edited_by="alice",
    )

    edits = await list_term_edits_for_node_key(conn, "t1", "产品:A")

    assert edits["aliases"] == ["别名一", "别名二"]
    assert edits["extra_properties.revenue"] == 1234.5


async def test_deleted_marker_is_stored_with_a_null_value():
    """__deleted__ 的 value 是 SQL NULL。读回来时这个字段必须存在于字典里
    （值为 None）——"有删除标记但值是 None"和"根本没有删除标记"是两件事，
    合并视图靠前者把实体整个排除掉。"""
    conn = await _conn()

    await upsert_term_edit(
        conn, tenant_id="t1", node_key="产品:A", field=FIELD_DELETED,
        value=None, edited_by="alice",
    )

    edits = await list_term_edits_for_node_key(conn, "t1", "产品:A")

    assert FIELD_DELETED in edits
    assert edits[FIELD_DELETED] is None


async def test_created_marker_holds_the_full_field_set():
    conn = await _conn()

    await upsert_term_edit(
        conn, tenant_id="t1", node_key="产品:NEW", field=FIELD_CREATED,
        value={"standard_name": "新建", "term_type": "产品", "aliases": []},
        edited_by="alice",
    )

    edits = await list_term_edits_for_node_key(conn, "t1", "产品:NEW")

    assert edits[FIELD_CREATED]["standard_name"] == "新建"


async def test_delete_term_edit_removes_only_that_field():
    conn = await _conn()
    for field, value in (("standard_name", "改名"), ("aliases", ["x"])):
        await upsert_term_edit(
            conn, tenant_id="t1", node_key="产品:A", field=field,
            value=value, edited_by="alice",
        )

    await delete_term_edit(conn, tenant_id="t1", node_key="产品:A", field="aliases")

    assert await list_term_edits_for_node_key(conn, "t1", "产品:A") == {
        "standard_name": "改名"
    }


async def test_edits_are_scoped_to_tenant():
    conn = await _conn()
    await upsert_term_edit(
        conn, tenant_id="t1", node_key="产品:A", field="standard_name",
        value="t1 的值", edited_by="alice",
    )
    await upsert_term_edit(
        conn, tenant_id="t2", node_key="产品:A", field="standard_name",
        value="t2 的值", edited_by="alice",
    )

    assert await list_term_edits(conn, "t1") == {"产品:A": {"standard_name": "t1 的值"}}
