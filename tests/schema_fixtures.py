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
from app.graphrag.attribute_conflicts import ensure_attribute_conflicts_schema
from app.graphrag.duplicate_review_queue import ensure_duplicate_review_schema
from app.graphrag.review_queue import ensure_review_schema


async def ensure_admin_auth_schema(conn: aiosqlite.Connection) -> None:
    """建齐管理后台身份与授权判据所需的表：`admin_users` + `user_tenants`。

    两张表必须一起建，理由见模块 docstring。调用方不要再单独调这两个
    `ensure_*`——那正是这个函数要消灭的写法。
    """
    await ensure_admin_users_schema(conn)
    await ensure_user_tenants_schema(conn)


async def ensure_review_queues_schema(conn: aiosqlite.Connection) -> None:
    """建齐**三个审核队列**：关系审核 + 疑似重复 + 属性值冲突。

    跟 `ensure_admin_auth_schema` 同一个道理：这三张表被两处**一起**读——
    侧边栏徽标（`app/api/admin_nav_badges_routes.py`）和看板的待审计数
    （`app/graphrag/tenant_stats.py`）。2026-09-08 加属性值冲突那张表时，
    四个此前只建了前两张的测试文件同时炸掉，报错是
    `no such table: attribute_conflicts`——指向 aiosqlite 内部，跟真正的原因
    （又多了一个队列而 fixture 没跟上）毫无关系。

    **这跟模块 docstring 里"不建业务表"那条不矛盾**：那条说的是
    `tests/api/conftest.py` 的**兜底连接**不该无差别建齐所有表——那会把一句
    响亮的 `no such table: terms` 变成一张静默的空表。这里是一个具名的、
    需要显式调用的帮手，且它建的三张表在语义上是一件事（"有多少活等着人"）。
    少建其中一张，得到的不是"那一类没有待办"，而是一个 500。
    """
    await ensure_review_schema(conn)
    await ensure_duplicate_review_schema(conn)
    await ensure_attribute_conflicts_schema(conn)
