import aiosqlite

from app.db_migrations import add_column_if_missing


async def test_add_column_if_missing_adds_new_column():
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    await conn.commit()

    await add_column_if_missing(conn, table="t", column="tenant_id", ddl="TEXT NOT NULL DEFAULT 'demo'")

    cursor = await conn.execute("PRAGMA table_info(t)")
    columns = {row[1] for row in await cursor.fetchall()}
    assert "tenant_id" in columns


async def test_add_column_if_missing_is_idempotent():
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    await conn.commit()

    await add_column_if_missing(conn, table="t", column="tenant_id", ddl="TEXT NOT NULL DEFAULT 'demo'")
    # 第二次调用不应该报错（列已存在）
    await add_column_if_missing(conn, table="t", column="tenant_id", ddl="TEXT NOT NULL DEFAULT 'demo'")

    cursor = await conn.execute("PRAGMA table_info(t)")
    columns = [row[1] for row in await cursor.fetchall()]
    assert columns.count("tenant_id") == 1
