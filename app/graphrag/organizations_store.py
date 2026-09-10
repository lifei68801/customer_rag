from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from app.db_migrations import add_column_if_missing
from app.graphrag.tenants_store import TenantNotFoundError

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS organizations (
    org_id     TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
# 组织只是租户之上的一层归拢，不参与任何隔离判据——隔离依旧是 tenant_id
# 一维（见 spec D1）。它存在的唯一理由是让看板能按客户聚合，以及让「一个
# 客户下有哪几个数字人」这个问题有地方回答。


class OrganizationNotFoundError(Exception):
    """指定的 org_id 不在 organizations 表里。"""


class OrganizationAlreadyExistsError(Exception):
    """这个 org_id 已经建过了。"""


async def ensure_organizations_schema(conn: aiosqlite.Connection) -> None:
    """建组织表，并给 tenants 补上 org_id 列。

    org_id 可空且没有外键约束：存量租户不属于任何组织，而这是合法状态，
    不是待修的数据问题。归属关系由 assign_tenant_to_org 在应用层校验——
    SQLite 默认不开启外键强制，写一个不生效的 FOREIGN KEY 会让读代码的人
    以为有约束在兜底。
    """
    await conn.executescript(_SCHEMA_SQL)
    await add_column_if_missing(conn, table="tenants", column="org_id", ddl="TEXT")
    await conn.commit()


class InvalidOrganizationError(ValueError):
    """org_id 或 name 是空的/纯空白。"""


class OrganizationDisabledError(Exception):
    """这个组织已停用，不能再往它下面挂新租户。"""


class OrganizationNotEmptyError(Exception):
    """这个组织名下还有租户，不能删。"""


#: 组织的两个状态。
#:
#: **「停用」在这个模型里只有一个含义：不能再往它下面挂新租户。** 已经挂着的
#: 不动，那些租户也照常工作——隔离是 tenant_id 一维（spec D1），组织只是归拢，
#: 停用它不该让任何数据变得不可访问。
#:
#: 把里面的租户一起踢出去是另一回事：用户点「停用」时想的是"先别再往里放了"，
#: 不是"把里面的东西倒出来"。
#:
#: 如果停用什么都不改变，那它就是个骗人的开关——所以这一条必须真的生效，
#: 有专门的用例钉着（test_disabling_an_org_blocks_new_assignments_...）。
_VALID_STATUSES = ("active", "disabled")


async def create_organization(conn: aiosqlite.Connection, *, org_id: str, name: str) -> None:
    """建一个组织。

    **空 / 纯空白的 org_id 直接拒绝，不能只靠前端按钮禁用。** 空串是 SQLite
    TEXT PRIMARY KEY 的合法值，唯一性检查也过得去（`""` 只跟另一个 `""` 撞），
    于是它会建出一个"看得见、摸不着"的组织：租户的「所属组织」下拉框里，
    `<option value="">` 正是「无」那一项，选中这个组织的名字实际发的是
    `org_id: null`（移出组织），租户被静默地移出而不是移入——而这个组织
    永远挂不上任何租户，也没有任何报错说明哪里不对。
    """
    if not org_id.strip():
        raise InvalidOrganizationError("组织 ID 不能为空")
    if not name.strip():
        raise InvalidOrganizationError("组织名称不能为空")
    cursor = await conn.execute("SELECT 1 FROM organizations WHERE org_id = ?", (org_id,))
    if await cursor.fetchone() is not None:
        raise OrganizationAlreadyExistsError(f"组织 {org_id!r} 已存在")
    await conn.execute(
        "INSERT INTO organizations (org_id, name) VALUES (?, ?)", (org_id, name)
    )
    await conn.commit()


async def list_organizations(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    """自己设 row_factory，跟本仓库其它 store 的做法一致（如
    app/auth/user_tenants_store.py）。生产路径上 review_conn 是进程内单例
    （见 app/api/deps.py::get_review_conn），启动阶段 seed_admin_user →
    get_admin_user 会先把它的 row_factory 设成 aiosqlite.Row，这里目前是
    "碰巧"能按列名取值；不自设的话，`dict(row)` 在未设 row_factory 的连接
    上拿到的是普通元组，会以 ValueError（"dictionary update sequence
    element #0 has length 4; 2 is required"）收场，而不是静默返回错的数据
    ——已用未设 row_factory 的连接验证过。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT org_id, name, status, created_at FROM organizations ORDER BY org_id"
    )
    return [dict(row) for row in await cursor.fetchall()]


async def list_tenants_with_organization(
    conn: aiosqlite.Connection, *, include_disabled: bool = False
) -> list[dict[str, Any]]:
    """启用中的租户，每条带上它所属组织的 id 和名字。看板的领域清单用。

    这个查询放在 organizations_store 而不是 tenants_store：`tenants.org_id`
    这一列是本模块加上去的（见 ensure_organizations_schema），"租户属于哪个
    组织"这件事的知识归本模块。

    LEFT JOIN 而不是 JOIN：没挂组织的租户是**合法状态**（存量租户，见模块
    docstring），内连接会让它们从看板上整个消失——用户会以为租户被删了。
    挂了一个不存在的 org_id 时同样走这一支（org_name 为 None）：
    assign_tenant_to_org 会挡住这种写入，但历史数据里可能有。

    默认只列启用中的，跟 tenants_store.list_tenants 的口径一致：停用的租户
    读得到、写全是 404，列在看板上只会让人切过去撞墙。
    """
    conn.row_factory = aiosqlite.Row
    sql = (
        "SELECT t.tenant_id, t.name, t.org_id, o.name AS org_name "
        "FROM tenants t LEFT JOIN organizations o ON o.org_id = t.org_id "
    )
    if not include_disabled:
        sql += "WHERE t.status = 'active' "
    sql += "ORDER BY t.tenant_id"
    cursor = await conn.execute(sql)
    return [dict(row) for row in await cursor.fetchall()]


async def assign_tenant_to_org(
    conn: aiosqlite.Connection, *, tenant_id: str, org_id: str | None
) -> None:
    """把租户挂到组织下；org_id 传 None 是移出组织。

    两侧都要校验存在性，理由不对称但都成立：

    - 挂到一个不存在的组织下会被拒绝：放行的话这个租户会从组织视图里彻底
      消失——它有 org_id，但那个 org_id 谁也查不到，界面上表现为「这个租户
      不见了」而没有任何报错。
    - tenant_id 不存在时同样要拒绝（`org_id=None` 的移出路径也不例外）：
      `UPDATE ... WHERE tenant_id = ?` 在没有匹配行时不会报错，只是静默
      影响 0 行，调用方会以为分配/移出成功了——这与姊妹函数
      `tenants_store.set_tenant_status` 先查后写、不存在就抛
      `TenantNotFoundError` 是同一种校验，这里补齐，复用同一个异常类型，
      不新造一个同义的，避免调用方要 catch 两种异常。
    """
    if org_id is not None:
        cursor = await conn.execute(
            "SELECT status FROM organizations WHERE org_id = ?", (org_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise OrganizationNotFoundError(f"组织 {org_id!r} 不存在")
        # 停用的组织拒收新租户。**移出（org_id=None）不走这条路**——不然
        # 停用之后里面的租户就被锁死了：既挂不进新的，也出不来。
        if row[0] == "disabled":
            raise OrganizationDisabledError(
                f"组织 {org_id!r} 已停用，不能再往它下面挂租户。要用的话先启用它。"
            )
    cursor = await conn.execute("SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,))
    if await cursor.fetchone() is None:
        raise TenantNotFoundError(f"租户 {tenant_id!r} 不存在")
    await conn.execute("UPDATE tenants SET org_id = ? WHERE tenant_id = ?", (org_id, tenant_id))
    await conn.commit()


async def set_organization_status(
    conn: aiosqlite.Connection, *, org_id: str, status: str
) -> None:
    """停用 / 启用一个组织。语义见 `_VALID_STATUSES` 上方那段说明。

    不存在时抛而不是静默影响 0 行——`UPDATE ... WHERE org_id = ?` 在没有
    匹配行时不报错，调用方会以为停用成功了。这跟 `assign_tenant_to_org`
    对 tenant_id 的处理是同一种校验。
    """
    if status not in _VALID_STATUSES:
        raise ValueError(f"非法 status: {status!r}，只能是 {list(_VALID_STATUSES)}")
    cursor = await conn.execute("SELECT 1 FROM organizations WHERE org_id = ?", (org_id,))
    if await cursor.fetchone() is None:
        raise OrganizationNotFoundError(f"组织 {org_id!r} 不存在")
    await conn.execute(
        "UPDATE organizations SET status = ? WHERE org_id = ?", (status, org_id)
    )
    await conn.commit()


async def delete_organization(conn: aiosqlite.Connection, *, org_id: str) -> None:
    """删掉一个组织。**名下还有租户时拒绝，并说清还有几个。**

    级联清掉那些租户的 org_id 是另一种做法，这里不用：那几个租户会静默地
    从组织视图里掉出来——用户不会注意到，直到打开看板发现分组变了。先让他
    把租户移出去，是一个他看得见、也做得到的纠正动作。

    这跟 `assign_tenant_to_org` 拒绝"挂到不存在的组织下"是同一条理由的两面：
    不让 `tenants.org_id` 指向一个查不到的组织。
    """
    cursor = await conn.execute("SELECT 1 FROM organizations WHERE org_id = ?", (org_id,))
    if await cursor.fetchone() is None:
        raise OrganizationNotFoundError(f"组织 {org_id!r} 不存在")
    tenant_ids = await list_tenants_in_org(conn, org_id)
    if tenant_ids:
        raise OrganizationNotEmptyError(
            f"组织 {org_id!r} 名下还有 {len(tenant_ids)} 个租户，先把它们移出去再删。"
        )
    await conn.execute("DELETE FROM organizations WHERE org_id = ?", (org_id,))
    await conn.commit()


async def list_tenants_in_org(conn: aiosqlite.Connection, org_id: str) -> list[str]:
    """row_factory 的理由同 list_organizations：不自设的话，未设
    row_factory 的连接上 `row["tenant_id"]` 会以 TypeError（"tuple indices
    must be integers or slices, not str"）收场，而不是拿到错的键——已用
    未设 row_factory 的连接验证过。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT tenant_id FROM tenants WHERE org_id = ? ORDER BY tenant_id", (org_id,)
    )
    return [row["tenant_id"] for row in await cursor.fetchall()]
