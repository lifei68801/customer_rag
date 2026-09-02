"""API 测试的公共 fixture。

`require_admin_session` 现在每个请求都要查一次本体库，确认这个账号仍是
active（「禁用账号」必须立即生效，不能等 session 自然过期）。这意味着**每一个**
打管理后台接口的测试都需要一个带 admin_users 表的本体库连接——包括那些
本来只关心 ingestion 库、压根没碰过本体库的测试。

让每个测试各自 override 一遍是几十处重复，而且新写的测试忘了加就会撞上
一句 "no such table: admin_users"，报错指向的地方跟真正的原因毫无关系。
这里给一个默认值；测试自己 override 同名依赖时照常覆盖它。
"""
from __future__ import annotations

import asyncio
from typing import Iterator

import aiosqlite
import pytest

from app.api import deps
from app.auth.admin_users_store import create_admin_user, ensure_admin_users_schema
from app.main import app


async def _open_admin_users_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_admin_users_schema(conn)
    await create_admin_user(
        conn, username="admin", password="password1", role="admin", tenant_id=None
    )
    return conn


@pytest.fixture(autouse=True)
def default_admin_users_conn() -> Iterator[None]:
    """给 get_review_conn 一个兜底：一个只装了 admin_users 的内存库。

    只兜底身份校验这一件事。需要读写术语/审核队列的测试仍要 override 成
    自己那个建了对应表的连接——那时这个默认值会被覆盖掉。

    必须显式 close：aiosqlite 的后台工作线程不是 daemon 线程，泄漏一个未
    关闭的连接会让 pytest 跑完全部用例后卡在解释器退出阶段。
    """
    conn = asyncio.run(_open_admin_users_conn())
    app.dependency_overrides[deps.get_review_conn] = lambda: conn
    try:
        yield
    finally:
        app.dependency_overrides.pop(deps.get_review_conn, None)
        asyncio.run(conn.close())
