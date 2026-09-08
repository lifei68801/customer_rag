import asyncio

import aiosqlite

from app.graphrag.tenant_personas_store import (
    ensure_tenant_personas_schema,
    get_persona,
    get_personas,
    get_questions,
    set_questions,
    upsert_persona,
)


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await ensure_tenant_personas_schema(conn)
    return conn


def test_upsert_then_get():
    async def run():
        conn = await _conn()
        try:
            await upsert_persona(
                conn, tenant_id="muji-goods", avatar="🛍️", tagline="我知道商品、口味和产地"
            )
            persona = await get_persona(conn, "muji-goods")
            assert persona == {
                "tenant_id": "muji-goods",
                "avatar": "🛍️",
                "tagline": "我知道商品、口味和产地",
            }
        finally:
            await conn.close()

    asyncio.run(run())


def test_upsert_twice_updates_instead_of_duplicating():
    async def run():
        conn = await _conn()
        try:
            await upsert_persona(conn, tenant_id="muji-goods", avatar="🛍️", tagline="旧的")
            await upsert_persona(conn, tenant_id="muji-goods", avatar="🧴", tagline="新的")
            persona = await get_persona(conn, "muji-goods")
            assert persona is not None
            assert persona["avatar"] == "🧴"
            assert persona["tagline"] == "新的"
        finally:
            await conn.close()

    asyncio.run(run())


def test_get_persona_returns_the_requested_tenant_not_another():
    """租户隔离是二元的：get_persona 必须精确匹配点名的那个租户，不能是
    「表里随便一行」。库里同时有两个内容能区分的脸，分别问这两个租户，
    各自的返回值必须对应各自写入的内容——如果实现退化成"忽略 tenant_id、
    返回任意一行"（比如把 WHERE 换成裸的 LIMIT 1），两次问不同的租户会拿到
    同一行，这里必然至少有一次对不上。"""

    async def run():
        conn = await _conn()
        try:
            await upsert_persona(conn, tenant_id="tenant-x", avatar="🅰️", tagline="甲的脸")
            await upsert_persona(conn, tenant_id="tenant-y", avatar="🅱️", tagline="乙的脸")

            persona_x = await get_persona(conn, "tenant-x")
            persona_y = await get_persona(conn, "tenant-y")

            assert persona_x == {
                "tenant_id": "tenant-x",
                "avatar": "🅰️",
                "tagline": "甲的脸",
            }
            assert persona_y == {
                "tenant_id": "tenant-y",
                "avatar": "🅱️",
                "tagline": "乙的脸",
            }
        finally:
            await conn.close()

    asyncio.run(run())


def test_missing_persona_returns_none_not_a_blank_row():
    """没配过的租户返回 None，前端据此渲染一个「还没配」的占位。
    返回一个空字段的字典的话，界面上会出现一个没有名字、没有头像的
    数字人，用户看不出它是「没配」还是「坏了」。"""

    async def run():
        conn = await _conn()
        try:
            assert await get_persona(conn, "从未配过") is None
        finally:
            await conn.close()

    asyncio.run(run())


def test_get_personas_batches_and_skips_the_unconfigured():
    """一次问一批：右栏有 N 个数字人，逐个问就是 N 次往返。
    没配过的不出现在结果里，而不是占一个空位。"""

    async def run():
        conn = await _conn()
        try:
            await upsert_persona(conn, tenant_id="a", avatar="🛍️", tagline="甲")
            await upsert_persona(conn, tenant_id="b", avatar="🏪", tagline="乙")
            await upsert_persona(conn, tenant_id="c", avatar="📦", tagline="丙")
            # 只问 a 和 b，还多问一个没配过的 z。c 配过但没问——它不该出现，
            # 否则「返回全表」的实现也能变绿。
            result = await get_personas(conn, ["a", "b", "z"])
            assert sorted(result) == ["a", "b"]
            assert result["a"]["tagline"] == "甲"
        finally:
            await conn.close()

    asyncio.run(run())


def test_get_personas_with_empty_list_does_not_query():
    """一个都不问时返回空字典。不加这条判断的话 SQL 会拼出
    `IN ()`，SQLite 上是语法错误。"""

    async def run():
        conn = await _conn()
        try:
            assert await get_personas(conn, []) == {}
        finally:
            await conn.close()

    asyncio.run(run())


def test_upsert_does_not_touch_questions():
    """变异 C：upsert_persona 的 ON CONFLICT 如果顺手把 questions 也清空
    （比如加上 `questions = '[]'`），改一次头像就会把下一阶段写好的引导
    问题全部清掉。这里手写一条 questions 进库，upsert 头像之后确认它
    还在原地——upsert_persona 目前的公开接口不产生 questions，所以
    这条断言只能用 SQL 直接验证，不能靠 get_persona/get_personas
    （它们本来就不选这一列）。"""

    async def run():
        conn = await _conn()
        try:
            await upsert_persona(conn, tenant_id="muji-goods", avatar="🛍️", tagline="旧的")
            await conn.execute(
                "UPDATE tenant_personas SET questions = ? WHERE tenant_id = ?",
                ('["先问预算"]', "muji-goods"),
            )
            await conn.commit()

            await upsert_persona(conn, tenant_id="muji-goods", avatar="🧴", tagline="新的")

            cursor = await conn.execute(
                "SELECT questions FROM tenant_personas WHERE tenant_id = ?", ("muji-goods",)
            )
            row = await cursor.fetchone()
            assert row["questions"] == '["先问预算"]'
        finally:
            await conn.close()

    asyncio.run(run())


def test_set_questions_does_not_wipe_the_face():
    """写问题不该把头像和人设清掉——它们是两个独立的编辑动作。"""

    async def run():
        conn = await _conn()
        try:
            await upsert_persona(conn, tenant_id="t1", avatar="🛍️", tagline="我知道商品")
            await set_questions(conn, tenant_id="t1", questions=["有什么新品？"])
            persona = await get_persona(conn, "t1")
            assert persona is not None
            assert persona["avatar"] == "🛍️"
            assert persona["tagline"] == "我知道商品"
            assert await get_questions(conn, "t1") == ["有什么新品？"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_questions_keep_their_order():
    """引导问题是有序的——第一条占的位置最值钱。存成集合或按字典序排
    的话，管理员精心排的顺序就没了。"""

    async def run():
        conn = await _conn()
        try:
            ordered = ["丙", "甲", "乙"]
            await set_questions(conn, tenant_id="t1", questions=ordered)
            assert await get_questions(conn, "t1") == ordered
        finally:
            await conn.close()

    asyncio.run(run())


def test_a_corrupt_questions_column_reads_as_empty_not_as_a_crash():
    async def run():
        conn = await _conn()
        try:
            await upsert_persona(conn, tenant_id="t1", avatar="", tagline="")
            await conn.execute(
                "UPDATE tenant_personas SET questions = ? WHERE tenant_id = ?",
                ("这不是 JSON", "t1"),
            )
            await conn.commit()
            assert await get_questions(conn, "t1") == []
        finally:
            await conn.close()

    asyncio.run(run())
