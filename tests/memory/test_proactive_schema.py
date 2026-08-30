import aiosqlite
import pytest

from app.memory.proactive_scan import ensure_proactive_schema

pytestmark = pytest.mark.anyio

_PROACTIVE_TABLES = frozenset({
    # 期望名单在测试里独立硬写，不从被测模块读回来——从常量读等于让实现
    # 自我印证，漏建一张表时测试会跟着一起漏。
    "tickets",
    "customer_profiles",
    "followup_log",
    "known_fixes",
    "ticket_fix_notifications",
    "delayed_confirmations",
})


async def test_creates_every_table_the_three_scans_need():
    """一次建齐三个扫描入口用到的全部表。

    在这之前，三个入口各自手工排一份 6 选 N 的建表子集：发工单跟进要
    tickets/customer_profiles/followup_log，发已知修复通知要 known_fixes/
    ticket_fix_notifications/customer_profiles/followup_log，发延迟确认要
    delayed_confirmations/customer_profiles/followup_log。哪个子集对应哪个
    入口是调用方知识，排漏一张不会在入口报错，而是深到 store 函数里才炸出
    一句裸的 "no such table"。

    这里跟 ontology_lifecycle.ensure_ontology_schema 是同一个做法——那个
    docstring 写得很直白："统一入口……不需要调用方自己记得额外建表"。
    """
    async with aiosqlite.connect(":memory:") as conn:
        await ensure_proactive_schema(conn)

        cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = {row[0] for row in await cursor.fetchall()}

    assert _PROACTIVE_TABLES <= names, f"缺表: {sorted(_PROACTIVE_TABLES - names)}"


async def test_is_idempotent():
    """重复调用不报错——三个入口会各调一次，worker 每轮扫描也会再调。"""
    async with aiosqlite.connect(":memory:") as conn:
        await ensure_proactive_schema(conn)
        await ensure_proactive_schema(conn)
