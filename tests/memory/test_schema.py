import aiosqlite

from app.memory.schema import ensure_schema


async def test_ensure_schema_adds_conflict_type_column_to_memory_history():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)

    cursor = await conn.execute("PRAGMA table_info(memory_history)")
    columns = {row[1] for row in await cursor.fetchall()}

    assert "conflict_type" in columns


async def test_ensure_schema_migrates_legacy_table_missing_conflict_type():
    """模拟已建库、还没有 conflict_type 列的历史部署——先手动建一份不带
    这一列的 memory_history 表，再调 ensure_schema，确认迁移把列补上而
    不是报错（ALTER TABLE ADD COLUMN 遇到已存在的列会报错，
    add_column_if_missing 必须先查 PRAGMA table_info 避免这个问题）。"""
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(
        """
        CREATE TABLE memory_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            event TEXT NOT NULL,
            old_text TEXT,
            new_text TEXT,
            reason TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    await conn.commit()

    await ensure_schema(conn)

    cursor = await conn.execute("PRAGMA table_info(memory_history)")
    columns = {row[1] for row in await cursor.fetchall()}
    assert "conflict_type" in columns


async def test_ensure_schema_is_idempotent():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    await ensure_schema(conn)  # 不应该报错（列已存在）

    cursor = await conn.execute("PRAGMA table_info(memory_history)")
    columns = {row[1] for row in await cursor.fetchall()}
    assert "conflict_type" in columns
