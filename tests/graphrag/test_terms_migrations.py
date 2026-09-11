"""terms 表迁移链的测试。

这条链有一条顺序约束，此前只写在注释里：有一步会**重建整张表**，而重建
DDL 里没有的列，排在它前面的话会被那次重建原地丢掉。丢掉之后补不回来
（`_SCHEMA_SQL` 是 CREATE TABLE IF NOT EXISTS，表已经在了），结果是本次启动
之后每一次 upsert 都在 SELECT 那一行抛 no such column，重启才自愈——而且
只有最老的那批存量库会踩，它们恰恰是最没人盯着的。

按模块各自为政的单元测试抓不住这种缺陷：每个迁移函数单独看都是对的。
抓得住的只有一条**从最老那版 schema 起跑完整条链**的测试。
"""

import asyncio
import json
import re
from pathlib import Path

import aiosqlite
import pytest

from app.graphrag.terms_migrations import (
    REBUILD_DDL_COLUMNS,
    TERMS_MIGRATIONS,
    apply_terms_migrations,
)
from app.graphrag.terms_store import ensure_terms_schema

#: 2026-08-15 之前的 terms 表：standard_name 当主键，没有 tenant_id/node_key，
#: 还带着后来删掉的 product_line 列。这是这条链要能从头跑起来的那个起点。
OLDEST_SCHEMA_SQL = """
CREATE TABLE terms (
    standard_name     TEXT PRIMARY KEY,
    aliases           TEXT NOT NULL,
    term_type         TEXT NOT NULL,
    product_line      TEXT NOT NULL DEFAULT '',
    extra_properties  TEXT NOT NULL DEFAULT '{}',
    source            TEXT NOT NULL DEFAULT 'unknown'
);
CREATE UNIQUE INDEX idx_terms_tenant_standard_name ON terms(standard_name, term_type);
"""


def _current_schema_columns() -> set[str]:
    """当前 `_SCHEMA_SQL` 声明的列名。

    从源码里解析而不是在这里抄一份：抄的那份改了源码也不会红，等于没测。
    """
    from app.graphrag import terms_store

    body = terms_store._SCHEMA_SQL.split("CREATE TABLE IF NOT EXISTS terms (", 1)[1]
    body = body.split("PRIMARY KEY", 1)[0]
    columns = set()
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        match = re.match(r"^(\w+)\s+TEXT", line)
        if match:
            columns.add(match.group(1))
    return columns


async def _columns_of(conn: aiosqlite.Connection) -> set[str]:
    cursor = await conn.execute("PRAGMA table_info(terms)")
    return {row[1] for row in await cursor.fetchall()}


async def _open_oldest_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(OLDEST_SCHEMA_SQL)
    await conn.execute(
        "INSERT INTO terms (standard_name, aliases, term_type, product_line, "
        "extra_properties, source) VALUES (?, ?, ?, ?, ?, ?)",
        ("拿铁", json.dumps(["latte"]), "产品", "咖啡", json.dumps({"价格": 32}), "manual"),
    )
    await conn.commit()
    return conn


@pytest.fixture()
def oldest_db():
    conn = asyncio.run(_open_oldest_db())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


def test_the_whole_chain_lands_on_the_current_schema(oldest_db):
    """从最老那版库跑完整条链，当前 schema 声明的每一列都得在。

    **这是这个文件存在的理由。** 顺序排错（把重建之后才能加的列排到重建
    前面）时，那一列会被重建原地丢掉，这条立刻红。此前没有任何测试能抓住
    这件事——四个迁移函数各自的测试都是绿的。
    """
    asyncio.run(ensure_terms_schema(oldest_db))

    actual = asyncio.run(_columns_of(oldest_db))
    expected = _current_schema_columns()
    assert expected, "没能从 _SCHEMA_SQL 解析出列名，这条测试会假绿"
    missing = expected - actual
    assert not missing, f"跑完迁移链之后还缺这些列：{sorted(missing)}"


def test_the_row_survives_the_rebuild_and_lands_in_the_default_tenant(oldest_db):
    """存量数据不能在重建里丢掉，而且要落到 default 租户。

    只断言列在不够：一条"先 DROP TABLE 再按新 DDL 建一张空表"的迁移也能让
    上面那条通过，而它会把用户的数据全删了。
    """
    asyncio.run(ensure_terms_schema(oldest_db))

    async def _read():
        cursor = await oldest_db.execute(
            "SELECT tenant_id, node_key, standard_name, term_type, extra_properties "
            "FROM terms"
        )
        return await cursor.fetchall()

    rows = asyncio.run(_read())
    assert len(rows) == 1
    row = rows[0]
    assert row["tenant_id"] == "default"
    assert row["standard_name"] == "拿铁"
    # node_key 回填成当时的 standard_name——老库里没有别的稳定身份可用。
    assert row["node_key"] == "拿铁"
    assert json.loads(row["extra_properties"]) == {"价格": 32}


def test_product_line_is_gone(oldest_db):
    asyncio.run(ensure_terms_schema(oldest_db))

    assert "product_line" not in asyncio.run(_columns_of(oldest_db))


def test_the_standard_name_index_ends_up_non_unique(oldest_db):
    """唯一性下沉到 node_key 之后，展示名的索引必须是普通索引。

    留着唯一索引的话，两个同名不同人的客户写不进去——而那正是 node_key
    要解决的问题（ADR-0003）。
    """
    asyncio.run(ensure_terms_schema(oldest_db))

    async def _is_unique():
        cursor = await oldest_db.execute("PRAGMA index_list('terms')")
        rows = await cursor.fetchall()
        return any(
            row[1] == "idx_terms_tenant_standard_name" and row[2] == 1 for row in rows
        )

    assert asyncio.run(_is_unique()) is False


def test_running_the_chain_twice_changes_nothing(oldest_db):
    """每一步都必须幂等——整条链每次启动都从头跑一遍。"""
    asyncio.run(ensure_terms_schema(oldest_db))
    after_once = asyncio.run(_columns_of(oldest_db))

    asyncio.run(apply_terms_migrations(oldest_db))
    asyncio.run(apply_terms_migrations(oldest_db))

    assert asyncio.run(_columns_of(oldest_db)) == after_once

    async def _count():
        cursor = await oldest_db.execute("SELECT COUNT(*) FROM terms")
        return (await cursor.fetchone())[0]

    assert asyncio.run(_count()) == 1


def test_columns_added_before_the_rebuild_are_all_in_the_rebuild_ddl():
    """顺序约束本身：重建之前加的列，必须是重建 DDL 里有的列。

    上面那条端到端测试会在排错时变红，但它说不出**为什么**。这一条直接
    检查规则，报错里能点名是哪一步排错了位置。

    这条规则不是"新列都排在后面"——extra_properties 和 source 就排在重建
    之前，而且是对的，因为重建那份 DDL 里有它们。
    """
    rebuild_index = max(
        i for i, m in enumerate(TERMS_MIGRATIONS) if m.rebuilds_table
    )
    for migration in TERMS_MIGRATIONS[:rebuild_index]:
        if not migration.name.startswith("add_"):
            continue
        column = migration.name.removeprefix("add_")
        assert column in REBUILD_DDL_COLUMNS, (
            f"迁移 {migration.name!r} 排在重建型迁移之前，但 {column!r} 不在重建 "
            "DDL 里——这一列会被那次重建原地丢掉，而且补不回来。把它挪到重建之后。"
        )


def test_at_least_one_migration_rebuilds_the_table():
    """上面那条规则检查依赖"存在一步重建"。一步都没有的话它会静默通过。"""
    assert any(m.rebuilds_table for m in TERMS_MIGRATIONS)


def test_migration_names_are_unique():
    """名字是这条链的身份，重名会让上面按名字定位的断言指错地方。"""
    names = [m.name for m in TERMS_MIGRATIONS]
    assert len(names) == len(set(names))


def test_no_migration_functions_left_behind_in_terms_store():
    """迁移不许再搬回 terms_store。

    搬回去不会有任何测试变红——它一开始总是对的，要等到下一个人读业务函数
    时才付出代价。所以用源码扫描挡住。
    """
    source = (
        Path(__file__).resolve().parents[2] / "app" / "graphrag" / "terms_store.py"
    ).read_text(encoding="utf-8")
    assert "_migrate_terms" not in source, "terms_store.py 里又出现了迁移函数"
