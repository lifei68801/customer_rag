import asyncio

import aiosqlite

from app.graphrag.tenant_personas_store import (
    DEFAULT_PERSONA_ID,
    InvalidPersonaError,
    create_persona,
    delete_persona,
    ensure_tenant_personas_schema,
    list_personas,
    get_persona,
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
                # 不传 persona_id 的调用方落在 default 那张脸上（ADR-0004
                # 解耦之后的兼容形态），name 空着——它只在多张脸时才有意义。
                "persona_id": "default",
                "name": "",
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
                "persona_id": "default",
                "name": "",
                "avatar": "🅰️",
                "tagline": "甲的脸",
            }
            assert persona_y == {
                "tenant_id": "tenant-y",
                "persona_id": "default",
                "name": "",
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


def test_upsert_does_not_touch_questions():
    """变异 C：upsert_persona 的 ON CONFLICT 如果顺手把 questions 也清空
    （比如加上 `questions = '[]'`），改一次头像就会把下一阶段写好的引导
    问题全部清掉。这里手写一条 questions 进库，upsert 头像之后确认它
    还在原地——upsert_persona 目前的公开接口不产生 questions，所以
    这条断言只能用 SQL 直接验证，不能靠 get_persona
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


# ---- 一个租户挂多张脸（ADR-0004：脸是展示单元，租户是隔离单元）----
#
# persona_id 只用来定位一张脸，**绝不进任何权限判据**——谁能看什么仍然只由
# tenant_id 决定。见计划的 Ruling P7-1。


def test_one_tenant_can_carry_two_faces():
    """一个租户两张脸，各自的头像/一句话/引导问题互不干扰。

    这是整个计划的支点：今天主键是 tenant_id 单列，写第二张脸会把第一张
    覆盖掉——「要几张脸」于是决定了「切几刀隔离」。
    """

    async def run():
        conn = await _conn()
        try:
            await create_persona(conn, tenant_id="muji", persona_id="daogou", name="导购小美")
            await create_persona(conn, tenant_id="muji", persona_id="dianwu", name="店务老张")
            await upsert_persona(
                conn, tenant_id="muji", persona_id="daogou", avatar="A", tagline="我懂商品"
            )
            await upsert_persona(
                conn, tenant_id="muji", persona_id="dianwu", avatar="B", tagline="我懂门店"
            )
            await set_questions(
                conn, tenant_id="muji", persona_id="daogou", questions=["有无香料洗发水吗"]
            )
            await set_questions(
                conn, tenant_id="muji", persona_id="dianwu", questions=["哪家店缺货"]
            )

            faces = await list_personas(conn, "muji")
            assert [f["persona_id"] for f in faces] == ["daogou", "dianwu"]
            a = await get_persona(conn, "muji", persona_id="daogou")
            b = await get_persona(conn, "muji", persona_id="dianwu")
            assert (a["avatar"], a["tagline"]) == ("A", "我懂商品")
            assert (b["avatar"], b["tagline"]) == ("B", "我懂门店")
            assert await get_questions(conn, "muji", persona_id="daogou") == ["有无香料洗发水吗"]
            assert await get_questions(conn, "muji", persona_id="dianwu") == ["哪家店缺货"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_faces_are_scoped_to_the_tenant():
    """两个租户各建一个同名 persona_id，各自只看到自己的。

    复合主键让同名并存是合法的——两个客户都把自己的脸叫 default 是常态。
    """

    async def run():
        conn = await _conn()
        try:
            await create_persona(conn, tenant_id="muji", persona_id="default", name="小美")
            await create_persona(conn, tenant_id="acme", persona_id="default", name="Acme 助手")

            assert [f["name"] for f in await list_personas(conn, "muji")] == ["小美"]
            assert [f["name"] for f in await list_personas(conn, "acme")] == ["Acme 助手"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_the_default_face_is_what_existing_callers_get():
    """不传 persona_id 的既有调用方拿到的是 'default' 那张脸。

    存量代码一行不用改——这是这次解耦能做到零迁移的原因。
    """

    async def run():
        conn = await _conn()
        try:
            await upsert_persona(conn, tenant_id="muji", avatar="A", tagline="一句话")
            await set_questions(conn, tenant_id="muji", questions=["问题一"])

            faces = await list_personas(conn, "muji")
            assert [f["persona_id"] for f in faces] == [DEFAULT_PERSONA_ID]
            assert (await get_persona(conn, "muji"))["tagline"] == "一句话"
            assert await get_questions(conn, "muji") == ["问题一"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_deleting_a_face_leaves_the_others():
    async def run():
        conn = await _conn()
        try:
            await create_persona(conn, tenant_id="muji", persona_id="a", name="A")
            await create_persona(conn, tenant_id="muji", persona_id="b", name="B")

            await delete_persona(conn, tenant_id="muji", persona_id="a")

            assert [f["persona_id"] for f in await list_personas(conn, "muji")] == ["b"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_deleting_another_tenants_face_does_nothing():
    """拿别的租户的 persona_id 过来删不掉。tenant_id 是条件不是断言。"""

    async def run():
        conn = await _conn()
        try:
            await create_persona(conn, tenant_id="acme", persona_id="a", name="A")

            await delete_persona(conn, tenant_id="muji", persona_id="a")

            assert len(await list_personas(conn, "acme")) == 1
        finally:
            await conn.close()

    asyncio.run(run())


def test_a_blank_persona_id_is_refused():
    """空 / 纯空白的 persona_id 拒掉。

    理由同组织那次：空串是合法的主键值，建出来之后在任何按 id 定位的地方
    都跟"没指定"撞车——而"没指定"走的是 default 那张脸。
    """
    import pytest

    async def run():
        conn = await _conn()
        try:
            for bad in ("", "   "):
                with pytest.raises(InvalidPersonaError):
                    await create_persona(conn, tenant_id="muji", persona_id=bad, name="X")
            # 反面：正常的建得成，否则"一律拒绝"的实现也能让上面变绿。
            await create_persona(conn, tenant_id="muji", persona_id="ok", name="X")
            assert len(await list_personas(conn, "muji")) == 1
        finally:
            await conn.close()

    asyncio.run(run())
