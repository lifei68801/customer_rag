"""属性值冲突表。

表格 A 说售价 39、表格 B 说 45——今天后跑的赢，没有任何人知道发生过冲突
（spec §1 点名的「今天最大的静默失败」）。这张表是把那件事记下来的地方，
Task 5 让 upsert 在覆盖之前往这里写。

处理原则是 Global Constraint 13：**记下来、保留先写的、等人来定**，
不是「猜一个」。
"""

from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.attribute_conflicts import (
    ConflictAlreadyResolvedError,
    ConflictNotFoundError,
    count_conflicts,
    ensure_attribute_conflicts_schema,
    list_conflicts,
    record_conflict,
    resolve_conflict,
)

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_attribute_conflicts_schema(conn)
    return conn


async def _record(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str = "t1",
    node_key: str = "产品:洗发水",
    field: str = "售价",
    kept_value: str = "39",
    kept_source: str = "商品表.xlsx",
    incoming_value: str = "45",
    incoming_source: str = "促销表.xlsx",
) -> None:
    await record_conflict(
        conn, tenant_id=tenant_id, node_key=node_key, field=field,
        kept_value=kept_value, kept_source=kept_source,
        incoming_value=incoming_value, incoming_source=incoming_source,
    )


async def test_record_and_list_a_conflict():
    """一条冲突要能说清四件事：哪个实体、哪个属性、留下的是什么值来自哪、
    被挡下的是什么值来自哪。

    少任何一样，审核员都没法判断该选哪个——「39 和 45 冲突了」这句话本身
    不构成一个可以做的决定。
    """
    conn = await _conn()
    try:
        await _record(conn)

        rows = await list_conflicts(conn, tenant_id="t1")

        assert len(rows) == 1
        row = rows[0]
        assert row["node_key"] == "产品:洗发水"
        assert row["field"] == "售价"
        assert (row["kept_value"], row["kept_source"]) == ("39", "商品表.xlsx")
        assert (row["incoming_value"], row["incoming_source"]) == ("45", "促销表.xlsx")
        assert row["status"] == "pending"
    finally:
        await conn.close()


async def test_the_same_conflict_recorded_twice_does_not_pile_up():
    """同一个 (实体, 属性) 反复导入不该堆出一百条待审。

    ETL 每天跑一次，堆起来的话审核员面对的是同一个问题的一百个副本。
    """
    conn = await _conn()
    try:
        await _record(conn)
        await _record(conn)

        assert await count_conflicts(conn, tenant_id="t1") == 1
    finally:
        await conn.close()


async def test_a_later_import_with_a_different_value_updates_the_pending_row():
    """incoming_value 变了要更新那一条，不是新增，也不是丢掉。

    第三张表说 52 的话，审核员该看到的是「39（商品表）对 52（第三张表）」
    ——留着 45 的话他在对一个已经没人主张的值做决定。
    """
    conn = await _conn()
    try:
        await _record(conn)
        await _record(conn, incoming_value="52", incoming_source="第三张表.xlsx")

        rows = await list_conflicts(conn, tenant_id="t1")

        assert len(rows) == 1
        assert (rows[0]["incoming_value"], rows[0]["incoming_source"]) == (
            "52",
            "第三张表.xlsx",
        )
        # 留下的那个值不动：它是先写进库的那个，谁也没决定要换掉它。
        assert rows[0]["kept_value"] == "39"
    finally:
        await conn.close()


async def test_different_fields_of_the_same_entity_are_separate_conflicts():
    """一个实体的两个属性各自冲突，是两条。

    合并成一条的话，审核员只能整行选 A 或选 B——而正确答案可能是
    「售价用 A、产地用 B」。
    """
    conn = await _conn()
    try:
        await _record(conn, field="售价")
        await _record(conn, field="产地")

        assert await count_conflicts(conn, tenant_id="t1") == 2
    finally:
        await conn.close()


async def test_conflicts_are_scoped_to_the_tenant():
    """两个租户各记一条，各自只看到自己的。"""
    conn = await _conn()
    try:
        await _record(conn, tenant_id="t1")
        await _record(conn, tenant_id="t2")

        assert [r["tenant_id"] for r in await list_conflicts(conn, tenant_id="t1")] == ["t1"]
        assert await count_conflicts(conn, tenant_id="t1") == 1
        assert await count_conflicts(conn, tenant_id="t2") == 1
    finally:
        await conn.close()


async def test_resolve_marks_it_and_records_who():
    """决议要记谁决的。

    记不下来的话，这张表跟本项目里那些没有审计的表一样，回答不了
    「谁改的」——而这是一个人工覆盖机器判断的动作，正是最需要留痕的那种。
    """
    conn = await _conn()
    try:
        await _record(conn)
        conflict_id = (await list_conflicts(conn, tenant_id="t1"))[0]["conflict_id"]

        chosen = await resolve_conflict(
            conn, tenant_id="t1", conflict_id=conflict_id,
            chosen_value="45", resolved_by="alice",
        )

        # 返回被选中的值，供调用方写回 terms——不返回的话调用方得自己再查
        # 一遍，而那一查和这一次决议之间隔着一个可以被别人插进来的窗口。
        assert chosen == "45"
        row = (await list_conflicts(conn, tenant_id="t1", status="resolved"))[0]
        assert row["status"] == "resolved"
        assert row["resolved_value"] == "45"
        assert row["resolved_by"] == "alice"
        assert row["resolved_at"]
    finally:
        await conn.close()


async def test_resolving_twice_is_refused():
    """已决议的不能再决议一次。

    放行的话，第二个人的选择会悄悄覆盖第一个人的，而两人都以为自己的生效了。
    """
    conn = await _conn()
    try:
        await _record(conn)
        conflict_id = (await list_conflicts(conn, tenant_id="t1"))[0]["conflict_id"]
        await resolve_conflict(
            conn, tenant_id="t1", conflict_id=conflict_id,
            chosen_value="45", resolved_by="alice",
        )

        with pytest.raises(ConflictAlreadyResolvedError):
            await resolve_conflict(
                conn, tenant_id="t1", conflict_id=conflict_id,
                chosen_value="39", resolved_by="bob",
            )

        # 第一个人的选择必须原样留着。
        row = (await list_conflicts(conn, tenant_id="t1", status="resolved"))[0]
        assert (row["resolved_value"], row["resolved_by"]) == ("45", "alice")
    finally:
        await conn.close()


async def test_resolving_another_tenants_conflict_is_refused():
    """按 conflict_id 决议时也要带租户判据。

    只按主键找的话，知道一个 id 就能改别人租户的数据——而 id 是自增的，
    猜得到。
    """
    conn = await _conn()
    try:
        await _record(conn, tenant_id="t2")
        conflict_id = (await list_conflicts(conn, tenant_id="t2"))[0]["conflict_id"]

        with pytest.raises(ConflictNotFoundError):
            await resolve_conflict(
                conn, tenant_id="t1", conflict_id=conflict_id,
                chosen_value="45", resolved_by="alice",
            )
    finally:
        await conn.close()


async def test_a_resolved_conflict_leaves_the_pending_count():
    """决议之后 count_conflicts 减一。

    不减的话看板和侧边栏的角标永远降不下去——审核员处理完一整页，那个数字
    纹丝不动，他会以为自己的操作没生效。
    """
    conn = await _conn()
    try:
        await _record(conn, field="售价")
        await _record(conn, field="产地")
        conflict_id = (await list_conflicts(conn, tenant_id="t1"))[0]["conflict_id"]

        await resolve_conflict(
            conn, tenant_id="t1", conflict_id=conflict_id,
            chosen_value="45", resolved_by="alice",
        )

        assert await count_conflicts(conn, tenant_id="t1") == 1
        # 默认列表也只列待处理的：已决议的混在里面，审核员会重复处理。
        assert len(await list_conflicts(conn, tenant_id="t1")) == 1
    finally:
        await conn.close()


async def test_resolve_accepts_a_value_that_is_neither_of_the_two():
    """审核员可以手填第三个值。

    两个来源都错是可能的（比如两张表都漏了单位），强制二选一等于逼他选一个
    已知是错的。
    """
    conn = await _conn()
    try:
        await _record(conn)
        conflict_id = (await list_conflicts(conn, tenant_id="t1"))[0]["conflict_id"]

        chosen = await resolve_conflict(
            conn, tenant_id="t1", conflict_id=conflict_id,
            chosen_value="39.00 元", resolved_by="alice",
        )

        assert chosen == "39.00 元"
        assert (await list_conflicts(conn, tenant_id="t1", status="resolved"))[0][
            "resolved_value"
        ] == "39.00 元"
    finally:
        await conn.close()


async def test_a_resolved_conflict_does_not_block_a_new_one_on_the_same_field():
    """同一个字段决议之后又冲突了，要能再记一条。

    去重判据里没有 status 的话，那条已决议的行会把新冲突挡在门外——ETL 明天
    再跑出一个不同的值，谁也不会知道。
    """
    conn = await _conn()
    try:
        await _record(conn)
        conflict_id = (await list_conflicts(conn, tenant_id="t1"))[0]["conflict_id"]
        await resolve_conflict(
            conn, tenant_id="t1", conflict_id=conflict_id,
            chosen_value="45", resolved_by="alice",
        )

        await _record(conn, incoming_value="52", incoming_source="第三张表.xlsx")

        assert await count_conflicts(conn, tenant_id="t1") == 1
        assert (await list_conflicts(conn, tenant_id="t1"))[0]["incoming_value"] == "52"
    finally:
        await conn.close()
