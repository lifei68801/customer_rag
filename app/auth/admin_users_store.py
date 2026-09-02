"""admin_users 表的建表与增删改查。

表建在本体库（settings.graph_review_db_path）——tenants 表在那里，账号与
租户的关联不该跨库。

表名是 admin_users 而不是 users：前台问答有自己的 user_id（前端生成的
UUID，见 app/api/session_routes.py），两者是完全不同的东西，同名会让人
以为它们相关。
"""
from __future__ import annotations

import re
from typing import Any

import aiosqlite

from app.auth.password import hash_password

__all__ = [
    "USERNAME_PATTERN",
    "AdminUserNotFoundError",
    "AdminUserAlreadyExistsError",
    "InvalidUsernameError",
    "ensure_admin_users_schema",
    "create_admin_user",
    "get_admin_user",
    "list_admin_users",
    "set_admin_user_status",
    "set_admin_user_password",
    "touch_last_login",
    "count_active_admins",
]

USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{3,32}$")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS admin_users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    tenant_id     TEXT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT,
    -- admin 是全局的，member 必须属于一个租户。放在 CHECK 里而不是只靠
    -- 应用层校验：绕过应用层直接写库的路径（迁移脚本、手工修数据）同样
    -- 会被挡住。
    CHECK (
        (role = 'admin'  AND tenant_id IS NULL) OR
        (role = 'member' AND tenant_id IS NOT NULL)
    )
);
"""

#: 列表接口直接返回给前端，password_hash 绝不能在里面——哈希本身不是密码，
#: 但它足够拿去离线爆破。
_PUBLIC_COLUMNS = "username, role, tenant_id, status, created_at, last_login_at"


class AdminUserNotFoundError(Exception):
    """指定的用户名不存在。"""


class AdminUserAlreadyExistsError(Exception):
    """用户名已被占用，或 role 与 tenant_id 的搭配违反表约束。"""


class InvalidUsernameError(Exception):
    """用户名不符合 USERNAME_PATTERN。"""


async def ensure_admin_users_schema(conn: aiosqlite.Connection) -> None:
    """建表，幂等——启动时每次都会调用。"""
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def create_admin_user(
    conn: aiosqlite.Connection,
    *,
    username: str,
    password: str,
    role: str,
    tenant_id: str | None,
) -> None:
    if not USERNAME_PATTERN.match(username or ""):
        raise InvalidUsernameError("用户名只能包含字母、数字、下划线和连字符，长度 3-32")
    if await get_admin_user(conn, username) is not None:
        raise AdminUserAlreadyExistsError(f"用户名已存在：{username}")
    try:
        await conn.execute(
            "INSERT INTO admin_users (username, password_hash, role, tenant_id) VALUES (?, ?, ?, ?)",
            (username, hash_password(password), role, tenant_id),
        )
    except aiosqlite.IntegrityError as exc:
        # CHECK 约束（role 与 tenant_id 的搭配）或并发插入撞主键。
        raise AdminUserAlreadyExistsError(str(exc)) from exc
    await conn.commit()


async def get_admin_user(conn: aiosqlite.Connection, username: str) -> dict[str, Any] | None:
    """含 password_hash——只给登录校验用，不要直接返回给前端。"""
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        f"SELECT {_PUBLIC_COLUMNS}, password_hash FROM admin_users WHERE username = ?",
        (username,),
    )
    row = await cursor.fetchone()
    return dict(row) if row is not None else None


async def list_admin_users(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        f"SELECT {_PUBLIC_COLUMNS} FROM admin_users ORDER BY role, username"
    )
    return [dict(row) for row in await cursor.fetchall()]


async def set_admin_user_status(conn: aiosqlite.Connection, username: str, status: str) -> None:
    cursor = await conn.execute(
        "UPDATE admin_users SET status = ? WHERE username = ?", (status, username)
    )
    if cursor.rowcount == 0:
        # 静默成功是最糟的结果：admin 点了"禁用"，界面说成功，那个账号
        # 还能登录。
        raise AdminUserNotFoundError(f"用户不存在：{username}")
    await conn.commit()


async def set_admin_user_password(
    conn: aiosqlite.Connection, username: str, password: str
) -> None:
    cursor = await conn.execute(
        "UPDATE admin_users SET password_hash = ? WHERE username = ?",
        (hash_password(password), username),
    )
    if cursor.rowcount == 0:
        raise AdminUserNotFoundError(f"用户不存在：{username}")
    await conn.commit()


async def touch_last_login(conn: aiosqlite.Connection, username: str) -> None:
    await conn.execute(
        "UPDATE admin_users SET last_login_at = datetime('now') WHERE username = ?",
        (username,),
    )
    await conn.commit()


async def count_active_admins(conn: aiosqlite.Connection) -> int:
    """「不能禁用最后一个 admin」这条不变量的依据。只数 active 的——把
    disabled 的算进去就会允许把最后一个可用 admin 也禁掉。"""
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM admin_users WHERE role = 'admin' AND status = 'active'"
    )
    row = await cursor.fetchone()
    return int(row[0])
