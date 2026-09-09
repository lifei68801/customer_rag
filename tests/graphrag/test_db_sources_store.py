"""数据源表。

**核心断言是这张表永远不许有密码列**（spec D3）。密码不落库，重跑时现填。
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from app.graphrag.db_sources_store import (
    create_db_source,
    delete_db_source,
    ensure_db_sources_schema,
    get_db_source,
    list_db_sources,
    touch_last_sync,
)
from app.ingestion.db_connector import DbConnectionSpec


@pytest.fixture()
async def conn():
    connection = await aiosqlite.connect(":memory:")
    try:
        await ensure_db_sources_schema(connection)
        yield connection
    finally:
        await connection.close()


def _spec(**over) -> DbConnectionSpec:
    payload = {
        "driver": "mysql",
        "host": "10.0.0.5",
        "port": 3306,
        "database": "shop",
        "username": "reader",
    }
    payload.update(over)
    return DbConnectionSpec(**payload)


async def _create(conn, *, tenant_id="demo", source_id="s1", **over):
    payload = {
        "name": "商品库",
        "spec": _spec(),
        "query": "SELECT id, name FROM goods",
        "mapping": {"term_type": "产品", "standard_name_parts": ["name"]},
    }
    payload.update(over)
    await create_db_source(conn, tenant_id=tenant_id, source_id=source_id, **payload)


async def test_the_table_has_no_password_column(conn):
    """本计划的第二条核心断言。

    将来有人加一列存密码时，这条会红。密码进了库就意味着：它会出现在备份里、
    出现在导出的 sqlite 文件里、出现在任何一次 `SELECT *` 的日志里。
    """
    cursor = await conn.execute("PRAGMA table_info(db_sources)")
    names = [row[1].lower() for row in await cursor.fetchall()]

    for banned in ("password", "passwd", "secret", "credential", "dsn"):
        assert not any(banned in name for name in names), f"db_sources 出现了 {banned} 列"
    # 反面：表本身要真的存在且有列，否则一张不存在的表也能让上面全绿。
    assert "username" in names


async def test_create_and_read_back_a_source(conn):
    """连接信息、SQL、列映射都要能原样读回来——它们是重跑时最贵的部分。

    用户配一次列映射要十几分钟，读不回来就等于每次同步都重配一遍。
    """
    await _create(conn)

    source = await get_db_source(conn, tenant_id="demo", source_id="s1")

    assert source is not None
    assert source["name"] == "商品库"
    assert source["driver"] == "mysql"
    assert source["host"] == "10.0.0.5"
    assert source["port"] == 3306
    assert source["database"] == "shop"
    assert source["username"] == "reader"
    assert source["query"] == "SELECT id, name FROM goods"


async def test_sources_are_scoped_to_the_tenant(conn):
    """两个租户各建一个同 source_id 的，各自只看到自己的。

    复合主键让同名并存是合法的——租户之间本来就不该知道对方起了什么名字。
    """
    await _create(conn, tenant_id="demo", source_id="s1", name="我的")
    await _create(conn, tenant_id="other", source_id="s1", name="别人的")

    mine = await list_db_sources(conn, tenant_id="demo")

    assert [s["name"] for s in mine] == ["我的"]
    assert await get_db_source(conn, tenant_id="demo", source_id="s1") is not None
    other = await get_db_source(conn, tenant_id="other", source_id="s1")
    assert other is not None and other["name"] == "别人的"


async def test_mapping_round_trips_as_a_dict(conn):
    """mapping 存 JSON、读回来是 dict。

    存成 str 让调用方自己解析的话，解析代码会在三个地方各写一遍，而其中
    一处迟早忘了 try。
    """
    mapping = {"term_type": "产品", "node_key_parts": [{"column": "id"}]}
    await _create(conn, mapping=mapping)

    source = await get_db_source(conn, tenant_id="demo", source_id="s1")

    assert source is not None
    assert source["mapping"] == mapping
    assert isinstance(source["mapping"], dict)


async def test_touch_last_sync_records_when_and_how_many(conn):
    """「上次同步 2 小时前，导入 4,712 行」——两个信息缺一不可。

    只有时间的话，用户不知道那次同步是成功导了数据还是导了个空。
    """
    await _create(conn)

    await touch_last_sync(conn, tenant_id="demo", source_id="s1", row_count=4712)

    source = await get_db_source(conn, tenant_id="demo", source_id="s1")
    assert source is not None
    assert source["last_sync_rows"] == 4712
    assert source["last_sync_at"]


async def test_a_source_that_never_synced_says_so(conn):
    """从没同步过时两个字段都是空。

    编一个 0 或者当前时间出来的话，列表上会显示「刚刚同步 · 0 行」——
    而它其实一次都没跑过，那是两件完全不同的事。
    """
    await _create(conn)

    source = await get_db_source(conn, tenant_id="demo", source_id="s1")

    assert source is not None
    assert source["last_sync_at"] is None
    assert source["last_sync_rows"] is None


async def test_touch_last_sync_does_not_touch_other_sources(conn):
    """只更新那一个数据源。

    漏了 source_id 条件的话，同步一个数据源会让所有数据源都显示「刚刚同步」。
    """
    await _create(conn, source_id="s1")
    await _create(conn, source_id="s2")

    await touch_last_sync(conn, tenant_id="demo", source_id="s1", row_count=10)

    other = await get_db_source(conn, tenant_id="demo", source_id="s2")
    assert other is not None and other["last_sync_at"] is None


async def test_delete_only_removes_that_one_source(conn):
    """两个数据源，删一个，另一个还在。"""
    await _create(conn, source_id="s1")
    await _create(conn, source_id="s2")

    await delete_db_source(conn, tenant_id="demo", source_id="s1")

    remaining = await list_db_sources(conn, tenant_id="demo")
    assert [s["source_id"] for s in remaining] == ["s2"]


async def test_deleting_another_tenants_source_does_nothing(conn):
    """拿别的租户的 source_id 过来删不掉。

    tenant_id 是条件不是断言——不能只靠调用方自觉。
    """
    await _create(conn, tenant_id="other", source_id="s1")

    await delete_db_source(conn, tenant_id="demo", source_id="s1")

    assert await get_db_source(conn, tenant_id="other", source_id="s1") is not None


async def test_a_missing_source_reads_back_as_none(conn):
    """不存在就是 None，不是抛异常也不是空 dict。"""
    assert await get_db_source(conn, tenant_id="demo", source_id="nope") is None
