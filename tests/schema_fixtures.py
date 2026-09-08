"""测试用的建表帮手。

只收一件事：**管理后台每个请求都要过的那两道判据，各自依赖的表必须一起建**。

背景。`deps.require_admin_session` 每个请求查一次 `admin_users`（确认账号仍是
active），`deps.assert_tenant_accessible` 每个租户内请求查一次 `user_tenants`
（确认这个账号被授权访问这个租户）。生产路径上两张表都由 `app/main.py` 的
lifespan 建好；而测试里手工搭的 `:memory:` 连接绕开了那条路径，得自己建。

问题不在于"要建两张表"，在于**这两张表是被分开手写的**：2026-09-08 把授权判据
从 `admin_users.tenant_id` 单列改成 `user_tenants` 多对多表时，三个此前只建了
`admin_users` 的测试文件同时炸掉，报错是 `no such table: user_tenants`——
指向 aiosqlite 内部，跟真正的原因（fixture 少建了一张表）毫无关系。

`ensure_admin_auth_schema` 把这一对绑在一起，让"只建了一半"在语法上写不出来。
将来判据再多依赖一张表时，要改的地方从八处变成这里一处。

**这个模块不建业务表**（terms / 审核队列 / 本体等）。那些仍由各测试文件按需
自己建，且 `tests/api/conftest.py` 的兜底连接刻意也不建它们——需要读写术语表的
测试必须显式 override 成自己那个建了对应表的连接，忘了就撞上一句响亮的
"no such table: terms"。把兜底改成"建齐所有表"会让那个保护变成一张静默的空表，
测试照常绿、断言却什么也没测到。
"""
from __future__ import annotations

import aiosqlite

from app.auth.admin_users_store import ensure_admin_users_schema
from app.auth.user_tenants_store import ensure_user_tenants_schema


async def ensure_admin_auth_schema(conn: aiosqlite.Connection) -> None:
    """建齐管理后台身份与授权判据所需的表：`admin_users` + `user_tenants`。

    两张表必须一起建，理由见模块 docstring。调用方不要再单独调这两个
    `ensure_*`——那正是这个函数要消灭的写法。
    """
    await ensure_admin_users_schema(conn)
    await ensure_user_tenants_schema(conn)
