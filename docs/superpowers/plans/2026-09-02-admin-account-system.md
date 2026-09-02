# 管理后台账号体系 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把管理后台从「一个共享 token」换成「用户名 + 密码 + 账号绑租户」，并借此堵上租户越权——当前任何登录者改一下请求里的 `tenant_id` 就能读写别的租户，返回 200，无日志无报错。

**Architecture:** 新建 `app/auth/` 包承载密码哈希、账号存储、登录限流三件互不依赖的事。`AdminSessionStore` 从「token → 过期时间」扩为「token → `AdminSession`」，承载 `username / role / tenant_id`。租户权限由单一依赖 `require_tenant_access` 校验，挂在一个父 router 上，另有结构测试兜底防止新增路由漏挂。

**Tech Stack:** FastAPI、`hashlib.scrypt`（标准库，不引入新依赖）、aiosqlite、pytest（`asyncio_mode = "auto"`）、React + vitest。

**Spec:** `docs/superpowers/specs/2026-09-02-admin-account-system-design.md` 第 4 节

**前置依赖:** `docs/superpowers/plans/2026-09-02-unified-tenant-addressing.md` 必须**先全部完成**。本计划的 Task 6（单一依赖覆盖全部租户路由）在 `tenant_id` 尚有四种来源时无法实现。

## Global Constraints

- **不引入新的第三方依赖。** 密码哈希用 `hashlib.scrypt`（标准库）。不装 `bcrypt` / `argon2-cffi` / `passlib`。
- **登录失败响应不区分原因。** 「用户不存在」「密码错误」「账号已禁用」一律返回 `401` + `"用户名或密码不正确"`。区分它们等于把接口变成用户名枚举器。三种情况在**服务端日志**里分别记录，且**绝不记录尝试的密码**。
- **`require_active_tenant_or_404`（租户**状态**校验）与 `require_tenant_access`（租户**权限**校验）是两件正交的事，都要保留。** 现有策略「写路由有状态校验、读路由没有」不得改变。
- **前端按角色渲染不承担安全责任。** 隐藏菜单项只是不误导人；真正的门在后端依赖上。任何「前端没显示所以不用后端校验」的推理都是错的。
- **每写完一条否定式断言（「X 不应该出现」「不能做 Y」），必须故意破坏实现确认它变红，然后恢复。**
- 密码存储格式固定为 `scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>`，参数从**存储串**读取而非从常量读取。
- 默认 scrypt 参数：`n=16384`、`r=8`、`p=1`、salt 16 字节、`dklen=32`。
- 密码长度 8–1024 字符，不要求字符类型。
- 用户名规则：3–32 字符，`[a-zA-Z0-9_-]`，`admin` 为保留名。
- 后端测试：`pytest`。前端：`cd frontend && npm test && npm run typecheck && npm run build`。
- Windows 下跑 Python 输出中文需要 `PYTHONIOENCODING=utf-8`。

---

## 文件结构

| 文件 | 责任 |
| --- | --- |
| `app/auth/__init__.py` | 新包 |
| `app/auth/password.py` | 纯函数：哈希与校验。无 IO、无依赖，最容易测 |
| `app/auth/admin_users_store.py` | `admin_users` 表的建表与增删改查 |
| `app/auth/login_throttle.py` | 进程内登录失败计数与锁定 |
| `app/api/admin_session.py` | **改造**：token → `AdminSession` |
| `app/api/deps.py` | **改造**：`require_admin_session` 返回 `AdminSession`；新增 `require_tenant_access`、`require_admin_role` |
| `app/api/admin_auth_routes.py` | **改造**：登录改用户名密码；新增改自己密码 |
| `app/api/admin_account_routes.py` | **新建**：账号管理 API |
| `app/main.py` | **改造**：租户作用域 router 收进父 router；启动播种 admin |
| `app/auth/bootstrap.py` | **新建**：启动播种 + 测试残留租户禁用 |
| `frontend/src/admin/LoginPage.tsx` | **改造**：用户名 + 密码 |
| `frontend/src/admin/useAdminAuth.ts` | **改造**：存 `username / role / tenantId` |
| `frontend/src/admin/TenantContext.tsx` | **改造**：member 的 `tenantId` 固定 |
| `frontend/src/admin/AccountMenu.tsx` | **改造**：按角色渲染 |
| `frontend/src/admin/AccountsPage.tsx` | **新建**：账号管理页 |
| `frontend/src/admin/SettingsPage.tsx` | **改造**：加改密码区块 |
| `frontend/src/adminRoutes.ts` | **改造**：新增 `accounts` 路由 |

`app/auth/` 是新包而非放进 `app/graphrag/`：账号不是知识图谱概念，尽管表建在同一个 SQLite 文件里。

---

### Task 1: 密码哈希

**Files:**
- Create: `app/auth/__init__.py`（空文件）
- Create: `app/auth/password.py`
- Test: `tests/auth/__init__.py`（空文件）、`tests/auth/test_password.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `hash_password(password: str) -> str` — 返回 `scrypt$n$r$p$salt_b64$hash_b64`
  - `verify_password(password: str, stored: str) -> bool`
  - `PasswordTooShortError` / `PasswordTooLongError`（均继承 `ValueError`）
  - `MIN_PASSWORD_LENGTH = 8`、`MAX_PASSWORD_LENGTH = 1024`

- [ ] **Step 1: 写失败的测试**

`tests/auth/test_password.py`：

```python
from __future__ import annotations

import base64

import pytest

from app.auth.password import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    PasswordTooLongError,
    PasswordTooShortError,
    hash_password,
    verify_password,
)


def test_same_password_hashes_differently_each_time():
    """salt 必须随机。两次哈希相同说明没加盐——那样一张彩虹表就够了，
    而且能一眼看出两个账号用了同一个密码。"""
    assert hash_password("correct horse") != hash_password("correct horse")


def test_correct_password_verifies():
    assert verify_password("correct horse", hash_password("correct horse")) is True


def test_wrong_password_does_not_verify():
    assert verify_password("wrong horse", hash_password("correct horse")) is False


def test_stored_format_is_self_describing():
    """参数必须写进存储串。写死在常量里的话，将来调参数会让所有历史密码
    一夜之间全部校验失败——而那时没人知道为什么。"""
    stored = hash_password("correct horse")
    parts = stored.split("$")
    assert parts[0] == "scrypt"
    assert parts[1:4] == ["16384", "8", "1"]
    assert len(base64.b64decode(parts[4])) == 16   # salt
    assert len(base64.b64decode(parts[5])) == 32   # dklen


def test_verifies_against_non_default_parameters():
    """参数从存储串读、不从常量读。这条是上一条的行为面：手工构造一个
    参数不同的存储串，仍然要能校验通过。"""
    import hashlib
    import os

    salt = os.urandom(16)
    digest = hashlib.scrypt(b"correct horse", salt=salt, n=1024, r=4, p=2, dklen=32)
    stored = (
        "scrypt$1024$4$2$"
        + base64.b64encode(salt).decode()
        + "$"
        + base64.b64encode(digest).decode()
    )
    assert verify_password("correct horse", stored) is True


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "notscrypt$16384$8$1$AAAA$BBBB",
        "scrypt$16384$8$1$AAAA",              # 段数不足
        "scrypt$notanumber$8$1$AAAA$BBBB",
        "scrypt$16384$8$1$!!!notbase64!!!$BBBB",
    ],
)
def test_malformed_stored_value_does_not_verify(stored: str):
    """存储串损坏时返回 False，不抛异常。抛异常会让登录接口变成 500，
    而 500 和 401 对攻击者是两种不同的信号。"""
    assert verify_password("anything", stored) is False


def test_tampering_with_any_segment_breaks_verification():
    """逐段篡改都必须失败。只比对哈希段而忽略 salt 段的实现能通过前面
    几条，但会在这里露馅。"""
    stored = hash_password("correct horse")
    parts = stored.split("$")
    for index in (1, 2, 3, 4, 5):
        broken = list(parts)
        broken[index] = base64.b64encode(b"x" * 16).decode() if index >= 4 else "9999"
        assert verify_password("correct horse", "$".join(broken)) is False


def test_too_short_password_is_rejected():
    with pytest.raises(PasswordTooShortError):
        hash_password("x" * (MIN_PASSWORD_LENGTH - 1))


def test_too_long_password_is_rejected():
    """上限不是洁癖：scrypt 对超长输入没有保护，拿一个 10MB 的密码去登录
    就是一次免费的 CPU 消耗攻击。"""
    with pytest.raises(PasswordTooLongError):
        hash_password("x" * (MAX_PASSWORD_LENGTH + 1))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/auth/test_password.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auth'`

- [ ] **Step 3: 实现**

`app/auth/__init__.py`：空文件。

`app/auth/password.py`：

```python
"""管理员密码的哈希与校验。

用标准库的 hashlib.scrypt（OpenSSL 实现，RFC 7914），不引入 bcrypt /
argon2-cffi——那两个都需要 cffi 或 Windows Build Tools，而本项目在
Windows 上开发。对一个内网管理后台，scrypt 参数选对了就够用，这不是
凑合。

存储格式自描述（scrypt$n$r$p$salt$hash），参数从存储串读取而不是从常量
读取：将来调高参数时，旧密码仍能校验通过，下次改密自动升级到新参数。
写死在常量里的话，调参数会让所有历史密码一夜之间全部失效。
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets

__all__ = [
    "MIN_PASSWORD_LENGTH",
    "MAX_PASSWORD_LENGTH",
    "PasswordTooShortError",
    "PasswordTooLongError",
    "hash_password",
    "verify_password",
]

MIN_PASSWORD_LENGTH = 8
#: 上限不是洁癖：scrypt 对超长输入没有保护，一个 10MB 的密码就是一次
#: 免费的 CPU 消耗攻击。
MAX_PASSWORD_LENGTH = 1024

_DEFAULT_N = 16384
_DEFAULT_R = 8
_DEFAULT_P = 1
_SALT_BYTES = 16
_DK_LEN = 32


class PasswordTooShortError(ValueError):
    """密码短于 MIN_PASSWORD_LENGTH。"""


class PasswordTooLongError(ValueError):
    """密码长于 MAX_PASSWORD_LENGTH。"""


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordTooShortError(f"密码至少 {MIN_PASSWORD_LENGTH} 个字符")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordTooLongError(f"密码最多 {MAX_PASSWORD_LENGTH} 个字符")
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_DEFAULT_N,
        r=_DEFAULT_R,
        p=_DEFAULT_P,
        dklen=_DK_LEN,
    )
    return "$".join(
        [
            "scrypt",
            str(_DEFAULT_N),
            str(_DEFAULT_R),
            str(_DEFAULT_P),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    """存储串损坏时返回 False 而不是抛异常。

    抛异常会让登录接口变成 500，而 500 和 401 对攻击者是两种不同的信号
    ——它等于在说"这个用户存在，只是它的密码记录坏了"。
    """
    try:
        algorithm, n_raw, r_raw, p_raw, salt_b64, hash_b64 = stored.split("$")
        if algorithm != "scrypt":
            return False
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
    except (ValueError, TypeError, MemoryError):
        # MemoryError：损坏的存储串里 n 可能是个天文数字，scrypt 会尝试
        # 分配对应内存。这是拒绝服务面，必须接住。
        return False
    return secrets.compare_digest(actual, expected)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/auth/test_password.py -v`
Expected: 全部 passed

- [ ] **Step 5: 破坏实现，确认否定断言会红**

三处各破坏一次，每次跑对应用例确认 FAIL，然后恢复：

1. 把 `verify_password` 里的 `n, r, p = int(n_raw), int(r_raw), int(p_raw)` 改成 `n, r, p = _DEFAULT_N, _DEFAULT_R, _DEFAULT_P` → `test_verifies_against_non_default_parameters` 应 FAIL
2. 把 `salt = os.urandom(_SALT_BYTES)` 改成 `salt = b"x" * _SALT_BYTES` → `test_same_password_hashes_differently_each_time` 应 FAIL
3. 把 `except (ValueError, TypeError, MemoryError)` 改成 `except TypeError` → `test_malformed_stored_value_does_not_verify` 应 FAIL（抛出 ValueError）

- [ ] **Step 6: 提交**

```bash
git add app/auth/__init__.py app/auth/password.py tests/auth/__init__.py tests/auth/test_password.py
git commit -m "feat: 密码哈希（scrypt，标准库）

存储格式自描述（scrypt\$n\$r\$p\$salt\$hash），参数从存储串读而不是从常量读
——将来调高参数时旧密码仍能校验，写死在常量里会让历史密码一夜之间全部
失效。

损坏的存储串返回 False 不抛异常：抛异常会让登录接口 500，而 500 和 401
对攻击者是两种不同的信号。MemoryError 也要接住——损坏串里的 n 可能是个
天文数字，scrypt 会真的去分配那么多内存。"
```

---

### Task 2: `admin_users` 表与存储层

**Files:**
- Create: `app/auth/admin_users_store.py`
- Test: `tests/auth/test_admin_users_store.py`

**Interfaces:**
- Consumes: `app.auth.password.hash_password`
- Produces:
  - `ensure_admin_users_schema(conn) -> None`
  - `create_admin_user(conn, *, username, password, role, tenant_id) -> None`
  - `get_admin_user(conn, username) -> dict | None`（含 `password_hash`）
  - `list_admin_users(conn) -> list[dict]`（**不含** `password_hash`）
  - `set_admin_user_status(conn, username, status) -> None`
  - `set_admin_user_password(conn, username, password) -> None`
  - `touch_last_login(conn, username) -> None`
  - `count_active_admins(conn) -> int`
  - `AdminUserNotFoundError` / `AdminUserAlreadyExistsError` / `InvalidUsernameError`（均继承 `Exception`）
  - `USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{3,32}$")`

- [ ] **Step 1: 写失败的测试**

`tests/auth/test_admin_users_store.py`：

```python
from __future__ import annotations

import aiosqlite
import pytest

from app.auth.admin_users_store import (
    AdminUserAlreadyExistsError,
    AdminUserNotFoundError,
    InvalidUsernameError,
    count_active_admins,
    create_admin_user,
    ensure_admin_users_schema,
    get_admin_user,
    list_admin_users,
    set_admin_user_password,
    set_admin_user_status,
    touch_last_login,
)
from app.auth.password import verify_password


@pytest.fixture
async def conn():
    """必须显式 close：aiosqlite 的后台工作线程不是 daemon 线程，泄漏一个
    未关闭的连接会让 pytest 跑完全部用例后卡在解释器退出阶段。做法同
    tests/api/test_admin_nav_badges_routes.py。"""
    connection = await aiosqlite.connect(":memory:")
    await ensure_admin_users_schema(connection)
    try:
        yield connection
    finally:
        await connection.close()


async def test_created_user_can_be_read_back(conn):
    await create_admin_user(
        conn, username="alice", password="password1", role="member", tenant_id="demo"
    )
    user = await get_admin_user(conn, "alice")
    assert user is not None
    assert user["username"] == "alice"
    assert user["role"] == "member"
    assert user["tenant_id"] == "demo"
    assert user["status"] == "active"


async def test_password_is_stored_hashed_not_plaintext(conn):
    """明文存密码是不可原谅的。这条断言的是存储值既不等于明文、又能校验
    通过——只断言"不等于明文"的话，存一个 md5 也能过。"""
    await create_admin_user(
        conn, username="alice", password="password1", role="member", tenant_id="demo"
    )
    user = await get_admin_user(conn, "alice")
    assert user["password_hash"] != "password1"
    assert verify_password("password1", user["password_hash"]) is True


async def test_missing_user_returns_none_not_error(conn):
    assert await get_admin_user(conn, "nobody") is None


async def test_duplicate_username_is_rejected(conn):
    await create_admin_user(
        conn, username="alice", password="password1", role="member", tenant_id="demo"
    )
    with pytest.raises(AdminUserAlreadyExistsError):
        await create_admin_user(
            conn, username="alice", password="password2", role="member", tenant_id="other"
        )


@pytest.mark.parametrize("username", ["ab", "x" * 33, "has space", "有中文", "a@b", ""])
async def test_invalid_username_is_rejected(conn, username: str):
    with pytest.raises(InvalidUsernameError):
        await create_admin_user(
            conn, username=username, password="password1", role="member", tenant_id="demo"
        )


async def test_admin_must_not_have_a_tenant(conn):
    """admin 是全局的。给它绑一个租户会让"admin 能看所有租户"这件事变得
    含糊——到底看全部，还是只看绑的那个？"""
    with pytest.raises(Exception):
        await create_admin_user(
            conn, username="root", password="password1", role="admin", tenant_id="demo"
        )


async def test_member_must_have_a_tenant(conn):
    """没有租户的 member 是个看不到任何数据的账号——建出来就是个陷阱。"""
    with pytest.raises(Exception):
        await create_admin_user(
            conn, username="alice", password="password1", role="member", tenant_id=None
        )


async def test_list_never_exposes_password_hash(conn):
    """列表接口会直接返回给前端。哈希值本身不是密码，但它足够离线爆破。"""
    await create_admin_user(
        conn, username="alice", password="password1", role="member", tenant_id="demo"
    )
    users = await list_admin_users(conn)
    assert users
    for user in users:
        assert "password_hash" not in user


async def test_status_can_be_toggled(conn):
    await create_admin_user(
        conn, username="alice", password="password1", role="member", tenant_id="demo"
    )
    await set_admin_user_status(conn, "alice", "disabled")
    assert (await get_admin_user(conn, "alice"))["status"] == "disabled"
    await set_admin_user_status(conn, "alice", "active")
    assert (await get_admin_user(conn, "alice"))["status"] == "active"


async def test_status_change_on_missing_user_raises(conn):
    """静默成功是最糟的结果：admin 点了"禁用"，界面说成功，那个账号还能
    登录。"""
    with pytest.raises(AdminUserNotFoundError):
        await set_admin_user_status(conn, "nobody", "disabled")


async def test_password_can_be_changed(conn):
    await create_admin_user(
        conn, username="alice", password="password1", role="member", tenant_id="demo"
    )
    await set_admin_user_password(conn, "alice", "password2")
    stored = (await get_admin_user(conn, "alice"))["password_hash"]
    assert verify_password("password2", stored) is True
    assert verify_password("password1", stored) is False


async def test_password_change_on_missing_user_raises(conn):
    with pytest.raises(AdminUserNotFoundError):
        await set_admin_user_password(conn, "nobody", "password2")


async def test_last_login_starts_empty_and_gets_set(conn):
    await create_admin_user(
        conn, username="alice", password="password1", role="member", tenant_id="demo"
    )
    assert (await get_admin_user(conn, "alice"))["last_login_at"] is None
    await touch_last_login(conn, "alice")
    assert (await get_admin_user(conn, "alice"))["last_login_at"] is not None


async def test_counts_only_active_admins(conn):
    """这个数是"不能禁用最后一个 admin"那条不变量的依据。把 disabled 的
    也算进去，就会允许把最后一个可用 admin 也禁掉。"""
    await create_admin_user(
        conn, username="root", password="password1", role="admin", tenant_id=None
    )
    assert await count_active_admins(conn) == 1
    await set_admin_user_status(conn, "root", "disabled")
    assert await count_active_admins(conn) == 0


async def test_schema_is_idempotent(conn):
    """启动时每次都会调用。第二次调用报错会让进程起不来。"""
    await ensure_admin_users_schema(conn)
    await ensure_admin_users_schema(conn)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/auth/test_admin_users_store.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现**

`app/auth/admin_users_store.py`：

```python
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
    """用户名已被占用。"""


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
        raise InvalidUsernameError(
            "用户名只能包含字母、数字、下划线和连字符，长度 3-32"
        )
    if await get_admin_user(conn, username) is not None:
        raise AdminUserAlreadyExistsError(f"用户名已存在：{username}")
    try:
        await conn.execute(
            "INSERT INTO admin_users (username, password_hash, role, tenant_id)"
            " VALUES (?, ?, ?, ?)",
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


async def set_admin_user_status(
    conn: aiosqlite.Connection, username: str, status: str
) -> None:
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/auth/test_admin_users_store.py -v`
Expected: 全部 passed

- [ ] **Step 5: 破坏实现，确认否定断言会红**

1. 把 `_PUBLIC_COLUMNS` 里加上 `password_hash` → `test_list_never_exposes_password_hash` 应 FAIL
2. 把 `set_admin_user_status` 里的 `if cursor.rowcount == 0: raise` 删掉 → `test_status_change_on_missing_user_raises` 应 FAIL
3. 把 `count_active_admins` 的 `AND status = 'active'` 去掉 → `test_counts_only_active_admins` 应 FAIL

每处确认 FAIL 后恢复，最后重跑全部确认通过。

- [ ] **Step 6: 提交**

```bash
git add app/auth/admin_users_store.py tests/auth/test_admin_users_store.py
git commit -m "feat: admin_users 表与存储层

role 与 tenant_id 的搭配约束写进 SQL CHECK 而不是只在应用层：绕过应用层
直接写库的路径（迁移脚本、手工修数据）同样会被挡住。

list 与 get 分成两个函数，只有 get 带 password_hash——列表接口直接返回给
前端，哈希本身不是密码但足够拿去离线爆破。

改状态/改密码时 rowcount 为 0 抛异常而不是静默返回：静默成功意味着 admin
点了"禁用"、界面说成功、那个账号还能登录。"
```

---

### Task 3: 登录限流

**Files:**
- Create: `app/auth/login_throttle.py`
- Test: `tests/auth/test_login_throttle.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `LoginThrottle` 类，方法：`check(username) -> None`（被锁时抛 `LoginLockedError`）、`record_failure(username) -> None`、`record_success(username) -> None`
  - `LoginLockedError`，属性 `retry_after_seconds: int`
  - `MAX_FAILURES = 5`、`LOCKOUT_SECONDS = 900`

- [ ] **Step 1: 写失败的测试**

`tests/auth/test_login_throttle.py`：

```python
from __future__ import annotations

import pytest

from app.auth.login_throttle import (
    LOCKOUT_SECONDS,
    MAX_FAILURES,
    LoginLockedError,
    LoginThrottle,
)


def test_fresh_username_is_not_locked():
    LoginThrottle().check("alice")


def test_locks_after_max_failures():
    """原凭证是 32 字节随机 token，爆破不现实；换成人选的密码后熵急剧
    下降，没有限流就是敞开的门。"""
    throttle = LoginThrottle()
    for _ in range(MAX_FAILURES):
        throttle.record_failure("alice")
    with pytest.raises(LoginLockedError):
        throttle.check("alice")


def test_one_failure_short_of_the_limit_is_not_locked():
    """边界：第 5 次失败才锁。差一位就把人锁在门外，是自己给自己制造
    故障。"""
    throttle = LoginThrottle()
    for _ in range(MAX_FAILURES - 1):
        throttle.record_failure("alice")
    throttle.check("alice")


def test_success_clears_the_counter():
    """密码打错几次然后打对了，不该在下次登录时留着旧账。"""
    throttle = LoginThrottle()
    for _ in range(MAX_FAILURES - 1):
        throttle.record_failure("alice")
    throttle.record_success("alice")
    for _ in range(MAX_FAILURES - 1):
        throttle.record_failure("alice")
    throttle.check("alice")


def test_lock_expires_after_the_window():
    throttle = LoginThrottle(now=lambda: 1000.0)
    for _ in range(MAX_FAILURES):
        throttle.record_failure("alice")
    with pytest.raises(LoginLockedError):
        throttle.check("alice")

    throttle_later = LoginThrottle(now=lambda: 1000.0 + LOCKOUT_SECONDS + 1)
    throttle_later._failures = throttle._failures  # 复用同一份状态
    throttle_later.check("alice")


def test_lockout_is_per_username():
    """按 username 计数而不是全局：否则一个攻击者能顺带把所有人锁在
    门外。"""
    throttle = LoginThrottle()
    for _ in range(MAX_FAILURES):
        throttle.record_failure("alice")
    throttle.check("bob")


def test_error_carries_remaining_seconds():
    """只说"稍后再试"，用户会每隔 10 秒试一次。"""
    throttle = LoginThrottle(now=lambda: 1000.0)
    for _ in range(MAX_FAILURES):
        throttle.record_failure("alice")
    with pytest.raises(LoginLockedError) as exc_info:
        throttle.check("alice")
    assert 0 < exc_info.value.retry_after_seconds <= LOCKOUT_SECONDS
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/auth/test_login_throttle.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现**

`app/auth/login_throttle.py`：

```python
"""登录失败计数与锁定。

原凭证是一个 32 字节随机 token，爆破不现实；换成人选的密码之后熵一下子
掉到几十位，没有限流就是敞开的门。admin_auth_routes.py 的原注释自己就
写了"目前没有限流/锁定，这条日志是唯一的审计线索"。

状态存进程内存，和 session 同一形态。重启清空锁定是可接受的——攻击者
控制不了服务端的重启。

按 username 计数而不是按 IP：本系统部署在内网，IP 区分度低；而且按
username 锁定不会让一个攻击者顺带把所有人锁在门外。
"""
from __future__ import annotations

import time
from typing import Callable

__all__ = ["MAX_FAILURES", "LOCKOUT_SECONDS", "LoginLockedError", "LoginThrottle"]

MAX_FAILURES = 5
LOCKOUT_SECONDS = 900  # 15 分钟


class LoginLockedError(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"登录失败次数过多，请 {retry_after_seconds // 60 + 1} 分钟后再试")
        self.retry_after_seconds = retry_after_seconds


class LoginThrottle:
    """username -> (连续失败次数, 首次失败时间)。

    已知局限：不存在的用户名同样会占用一个计数槽位，但调用方只在**用户
    确实存在**时才 record_failure（见 admin_auth_routes），所以攻击者可以
    用不同的伪造用户名无限尝试。这在只有个位数账号的内网系统里是可接受
    的——真正的账号仍受 MAX_FAILURES 保护，而给任意用户名都建槽位会让
    内存被撑爆。
    """

    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._failures: dict[str, tuple[int, float]] = {}

    def check(self, username: str) -> None:
        entry = self._failures.get(username)
        if entry is None:
            return
        count, first_failure_at = entry
        if count < MAX_FAILURES:
            return
        elapsed = self._now() - first_failure_at
        if elapsed >= LOCKOUT_SECONDS:
            del self._failures[username]
            return
        raise LoginLockedError(int(LOCKOUT_SECONDS - elapsed))

    def record_failure(self, username: str) -> None:
        count, first_failure_at = self._failures.get(username, (0, self._now()))
        self._failures[username] = (count + 1, first_failure_at)

    def record_success(self, username: str) -> None:
        self._failures.pop(username, None)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/auth/test_login_throttle.py -v`
Expected: 全部 passed

- [ ] **Step 5: 破坏实现，确认边界断言会红**

把 `if count < MAX_FAILURES: return` 改成 `if count < MAX_FAILURES - 1: return`，跑：

Run: `pytest tests/auth/test_login_throttle.py::test_one_failure_short_of_the_limit_is_not_locked -v`
Expected: FAIL

确认后恢复。

- [ ] **Step 6: 提交**

```bash
git add app/auth/login_throttle.py tests/auth/test_login_throttle.py
git commit -m "feat: 登录失败限流（5 次 / 15 分钟）

密码登录比 token 登录容易爆破得多——token 是 32 字节随机串，人选的密码
熵只有几十位。按 username 而不是按 IP 计数：内网部署 IP 区分度低，而且
按 username 锁不会让一个攻击者顺带把所有人锁在门外。

已知局限写在 docstring 里：不存在的用户名不占槽位（否则内存会被任意
用户名撑爆），所以攻击者能用伪造用户名无限尝试。真正的账号仍受保护。"
```

---

### Task 4: `AdminSession` 扩展与 `require_admin_session` 改造

**Files:**
- Modify: `app/api/admin_session.py`（整个文件）
- Modify: `app/api/deps.py:267-279`（`require_admin_session`）
- Test: `tests/api/test_admin_session.py`（既有，需扩展）

**Interfaces:**
- Consumes: `app.auth.admin_users_store.get_admin_user`
- Produces:
  - `AdminSession` dataclass：`username: str`、`role: str`、`tenant_id: str | None`、`expires_at: float`
  - `AdminSessionStore.create_session(*, username, role, tenant_id, ttl_seconds=28800) -> str`
  - `AdminSessionStore.get_session(token) -> AdminSession | None`
  - `require_admin_session(...) -> AdminSession`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/api/test_admin_session.py`：

```python
def test_session_carries_identity():
    """session 必须记住"你是谁、属于哪个租户"。只记过期时间的话，每个
    请求都得重新问一遍，而没有地方可问。"""
    store = AdminSessionStore()
    token = store.create_session(username="alice", role="member", tenant_id="demo")
    session = store.get_session(token)
    assert session is not None
    assert session.username == "alice"
    assert session.role == "member"
    assert session.tenant_id == "demo"


def test_admin_session_has_no_tenant():
    store = AdminSessionStore()
    token = store.create_session(username="admin", role="admin", tenant_id=None)
    assert store.get_session(token).tenant_id is None


def test_unknown_token_returns_none():
    assert AdminSessionStore().get_session("nope") is None


def test_expired_session_returns_none():
    store = AdminSessionStore()
    token = store.create_session(
        username="alice", role="member", tenant_id="demo", ttl_seconds=-1
    )
    assert store.get_session(token) is None


def test_revoked_session_returns_none():
    store = AdminSessionStore()
    token = store.create_session(username="alice", role="member", tenant_id="demo")
    store.revoke_session(token)
    assert store.get_session(token) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/api/test_admin_session.py -v`
Expected: 新增用例 FAIL — `create_session()` 不接受这些关键字参数

- [ ] **Step 3: 改 `admin_session.py`**

```python
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class AdminSession:
    """一个登录中的管理员身份。

    tenant_id 为 None 表示 admin——它不属于任何租户，可以访问全部。
    """

    username: str
    role: str  # "admin" | "member"
    tenant_id: str | None
    expires_at: float


class AdminSessionStore:
    """进程内管理员 session 存取，token -> AdminSession。

    不做持久化——管理员 session 本来就设计成短期有效（默认 8 小时），
    进程重启导致所有人重新登录是可接受的代价，换来不用额外引入
    JWT 签名/SQLite 表这些复杂度。

    不改用 JWT 的另一个理由：JWT 签发后撤销不了，「禁用账号立即生效」
    就做不到，而维护黑名单等于又回到有状态。
    """

    def __init__(self) -> None:
        self._sessions: dict[str, AdminSession] = {}

    def create_session(
        self,
        *,
        username: str,
        role: str,
        tenant_id: str | None,
        ttl_seconds: int = 28800,
    ) -> str:
        self._sweep_expired()
        token = secrets.token_urlsafe(32)
        self._sessions[token] = AdminSession(
            username=username,
            role=role,
            tenant_id=tenant_id,
            expires_at=time.time() + ttl_seconds,
        )
        return token

    def _sweep_expired(self) -> None:
        """顺手清掉已过期但从未被查询过的 session。

        不引入后台定时任务/线程——管理员场景登录频率很低，"每次新登录时
        顺便扫一遍"足够避免字典无限增长。
        """
        now = time.time()
        expired = [t for t, s in self._sessions.items() if now >= s.expires_at]
        for token in expired:
            del self._sessions[token]

    def get_session(self, token: str) -> AdminSession | None:
        session = self._sessions.get(token)
        if session is None:
            return None
        if time.time() >= session.expires_at:
            del self._sessions[token]
            return None
        return session

    def revoke_session(self, token: str) -> None:
        self._sessions.pop(token, None)
```

`verify_session` 被 `get_session` 取代。用 `grep -rn "verify_session" app/ tests/` 找出全部调用点并改掉——**必须为 0 个残留**。

- [ ] **Step 4: 改 `deps.py` 的 `require_admin_session`**

`app/api/deps.py` 第 267-279 行整体替换：

```python
async def require_admin_session(
    authorization: str | None = Header(default=None),
    session_store: AdminSessionStore = Depends(get_admin_session_store),
    review_conn: aiosqlite.Connection = Depends(get_review_conn),
) -> AdminSession:
    """校验 Authorization: Bearer <token>，返回这个 session 的身份。

    所有 /api/admin/* 路由（登录接口本身除外）都应该依赖这个函数。

    除了校验 session 本身，还要确认这个账号当前仍是 active——「禁用账号」
    必须立即生效，而不是等 session 自然过期。代价是每个请求多一次 SQLite
    查询；替代方案"禁用时主动撤销该用户的所有 session"只对本进程内已知的
    session 有效，多进程部署时另一个进程里的 session 撤销不掉，会退化成
    静默失效。查库是唯一在各种部署形态下都成立的做法。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少管理员登录凭证")
    token = authorization.removeprefix("Bearer ")
    session = session_store.get_session(token)
    if session is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    user = await get_admin_user(review_conn, session.username)
    if user is None or user["status"] != "active":
        session_store.revoke_session(token)
        raise HTTPException(status_code=401, detail="账号已停用")
    return session
```

在 `deps.py` 顶部补两个导入：

```python
from app.api.admin_session import AdminSession, AdminSessionStore
from app.auth.admin_users_store import get_admin_user
```

并把 `AdminSession` 加进 `__all__`（第 68 行附近的列表）。

- [ ] **Step 5: 跑全量确认影响面**

Run: `pytest -x`
Expected: 大量既有测试 FAIL——它们调用的是 `create_session()`（无参数）。这是**预期的**，Task 5 会一并修好。先记录失败清单：

Run: `pytest 2>&1 | grep -c FAILED`

- [ ] **Step 6: 修既有测试的 `_authed_headers`**

全项目搜索 `create_session()`：

```bash
grep -rn "create_session()" tests/
```

每处改为 `create_session(username="admin", role="admin", tenant_id=None)`。这些测试用的都是"有全部权限的管理员"语义，admin 是正确的替代。

同时，这些测试的 fixture 现在需要 `admin_users` 表里有 `admin` 这一行（因为 `require_admin_session` 会查库）。在各测试文件的 `_open_review_conn()` 里补：

```python
    from app.auth.admin_users_store import create_admin_user, ensure_admin_users_schema

    await ensure_admin_users_schema(conn)
    await create_admin_user(
        conn, username="admin", password="password1", role="admin", tenant_id=None
    )
```

- [ ] **Step 7: 跑全量确认通过**

Run: `pytest`
Expected: 全部 passed

- [ ] **Step 8: 破坏实现，确认禁用立即生效**

新增用例到 `tests/api/test_admin_auth_routes.py`：

```python
def test_disabled_account_session_stops_working_immediately(review_conn):
    """禁用必须立即生效。等 session 自然过期意味着被禁的人还能再操作
    8 小时——而禁用的场景通常正是"这个人现在就不该再动数据了"。"""
    session_store = AdminSessionStore()
    token = session_store.create_session(username="alice", role="member", tenant_id="demo")
    asyncio.run(
        create_admin_user(
            review_conn, username="alice", password="password1", role="member", tenant_id="demo"
        )
    )
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/admin/auth/whoami", headers=headers).status_code == 200

        asyncio.run(set_admin_user_status(review_conn, "alice", "disabled"))
        assert client.get("/api/admin/auth/whoami", headers=headers).status_code == 401
    finally:
        app.dependency_overrides.clear()
```

把 `require_admin_session` 里的 `if user is None or user["status"] != "active":` 整段临时删掉，跑：

Run: `pytest tests/api/test_admin_auth_routes.py::test_disabled_account_session_stops_working_immediately -v`
Expected: FAIL（禁用后仍返回 200）

确认后恢复。

- [ ] **Step 9: 提交**

```bash
git add app/api/admin_session.py app/api/deps.py tests/api/
git commit -m "feat: session 承载身份（username/role/tenant_id）

此前 session 只记过期时间，所以每个请求都无从知道"你是谁、属于哪个租户"
——租户隔离没法做，正是因为这个。

require_admin_session 现在每次还要确认账号仍是 active：禁用必须立即生效，
等 session 自然过期意味着被禁的人还能再操作 8 小时。代价是每请求一次
SQLite 查询；替代方案（禁用时主动撤销 session）只对本进程有效，多进程
部署时会静默失效。

不改 JWT：签发后撤销不了，禁用就做不到立即生效，而维护黑名单等于又回到
有状态。"
```

---

### Task 5: 登录路由改用户名密码

**Files:**
- Modify: `app/api/admin_auth_routes.py`（整个文件）
- Modify: `app/api/deps.py`（新增 `get_login_throttle`）
- Test: `tests/api/test_admin_auth_routes.py`

**Interfaces:**
- Consumes: Task 1-4 全部
- Produces:
  - `POST /api/admin/auth/login` 请求 `{username, password}`，响应 `{session_token, username, role, tenant_id}`
  - `PUT /api/admin/auth/password` 请求 `{old_password, new_password}`
  - `GET /api/admin/auth/whoami` 响应 `{username, role, tenant_id}`
  - `deps.get_login_throttle() -> LoginThrottle`（进程内单例）

- [ ] **Step 1: 写失败的测试**

`tests/api/test_admin_auth_routes.py` 新增：

```python
def test_login_with_correct_credentials_returns_identity(review_conn):
    """登录响应要带上身份。前端据此决定渲染什么——但渲染不承担安全责任，
    真正的门在后端依赖上。"""
    asyncio.run(
        create_admin_user(
            review_conn, username="alice", password="password1", role="member", tenant_id="demo"
        )
    )
    response = _login(review_conn, username="alice", password="password1")
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "alice"
    assert body["role"] == "member"
    assert body["tenant_id"] == "demo"
    assert body["session_token"]


@pytest.mark.parametrize(
    "username,password",
    [
        ("alice", "wrongpassword"),   # 密码错
        ("nobody", "password1"),      # 用户不存在
    ],
)
def test_failed_login_does_not_reveal_which_part_was_wrong(review_conn, username, password):
    """三种失败（用户不存在/密码错/账号禁用）必须返回同一条文案和同一个
    状态码。区分它们等于把这个接口变成用户名枚举器。"""
    asyncio.run(
        create_admin_user(
            review_conn, username="alice", password="password1", role="member", tenant_id="demo"
        )
    )
    response = _login(review_conn, username=username, password=password)
    assert response.status_code == 401
    assert response.json()["detail"] == "用户名或密码不正确"


def test_disabled_account_login_looks_the_same_as_a_wrong_password(review_conn):
    asyncio.run(
        create_admin_user(
            review_conn, username="alice", password="password1", role="member", tenant_id="demo"
        )
    )
    asyncio.run(set_admin_user_status(review_conn, "alice", "disabled"))
    response = _login(review_conn, username="alice", password="password1")
    assert response.status_code == 401
    assert response.json()["detail"] == "用户名或密码不正确"


def test_login_updates_last_login(review_conn):
    asyncio.run(
        create_admin_user(
            review_conn, username="alice", password="password1", role="member", tenant_id="demo"
        )
    )
    _login(review_conn, username="alice", password="password1")
    user = asyncio.run(get_admin_user(review_conn, "alice"))
    assert user["last_login_at"] is not None


def test_sixth_failed_attempt_is_throttled(review_conn):
    asyncio.run(
        create_admin_user(
            review_conn, username="alice", password="password1", role="member", tenant_id="demo"
        )
    )
    throttle = LoginThrottle()
    for _ in range(5):
        assert _login(
            review_conn, username="alice", password="bad", throttle=throttle
        ).status_code == 401
    assert _login(
        review_conn, username="alice", password="bad", throttle=throttle
    ).status_code == 429


def test_whoami_returns_identity(review_conn):
    asyncio.run(
        create_admin_user(
            review_conn, username="alice", password="password1", role="member", tenant_id="demo"
        )
    )
    session_store = AdminSessionStore()
    token = session_store.create_session(username="alice", role="member", tenant_id="demo")
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        body = TestClient(app).get(
            "/api/admin/auth/whoami", headers={"Authorization": f"Bearer {token}"}
        ).json()
        assert body == {"username": "alice", "role": "member", "tenant_id": "demo"}
    finally:
        app.dependency_overrides.clear()


def test_can_change_own_password_with_the_old_one(review_conn):
    asyncio.run(
        create_admin_user(
            review_conn, username="alice", password="password1", role="member", tenant_id="demo"
        )
    )
    assert _change_own_password(
        review_conn, username="alice", old="password1", new="password2"
    ).status_code == 200
    assert _login(review_conn, username="alice", password="password2").status_code == 200


def test_cannot_change_password_without_the_old_one(review_conn):
    """不验旧密码的话，任何拿到 session 的人（比如一台没锁屏的电脑）
    都能把账号锁给自己。"""
    asyncio.run(
        create_admin_user(
            review_conn, username="alice", password="password1", role="member", tenant_id="demo"
        )
    )
    assert _change_own_password(
        review_conn, username="alice", old="wrongpassword", new="password2"
    ).status_code == 400
    assert _login(review_conn, username="alice", password="password1").status_code == 200
```

辅助函数（放在测试文件顶部的 fixture 之后）：

```python
def _login(review_conn, *, username: str, password: str, throttle=None):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    if throttle is not None:
        app.dependency_overrides[deps.get_login_throttle] = lambda: throttle
    try:
        return TestClient(app).post(
            "/api/admin/auth/login", json={"username": username, "password": password}
        )
    finally:
        app.dependency_overrides.clear()


def _change_own_password(review_conn, *, username: str, old: str, new: str):
    session_store = AdminSessionStore()
    token = session_store.create_session(username=username, role="member", tenant_id="demo")
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        return TestClient(app).put(
            "/api/admin/auth/password",
            json={"old_password": old, "new_password": new},
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides.clear()
```

**旧的 `admin_token` 登录测试全部删除**——那条路径不再存在。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/api/test_admin_auth_routes.py -v`
Expected: FAIL

- [ ] **Step 3: 在 deps.py 里加限流单例**

参照 `get_admin_session_store`（第 259-264 行）的写法：

```python
_login_throttle_cache: LoginThrottle | None = None


def get_login_throttle() -> LoginThrottle:
    """进程内单例：失败计数必须跨请求累积，每次新建等于没有限流。"""
    global _login_throttle_cache
    if _login_throttle_cache is None:
        _login_throttle_cache = LoginThrottle()
    return _login_throttle_cache
```

加进 `__all__`。

- [ ] **Step 4: 重写 `admin_auth_routes.py`**

```python
from __future__ import annotations

import logging

import aiosqlite
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.api.admin_session import AdminSession, AdminSessionStore
from app.auth.admin_users_store import (
    get_admin_user,
    set_admin_user_password,
    touch_last_login,
)
from app.auth.login_throttle import LoginLockedError, LoginThrottle
from app.auth.password import verify_password

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/auth")

#: 三种失败（用户不存在 / 密码错 / 账号已禁用）共用同一条文案和同一个
#: 状态码。区分它们等于把这个接口变成用户名枚举器——攻击者只要看响应
#: 就能列出所有真实存在的账号。
_LOGIN_FAILED_DETAIL = "用户名或密码不正确"


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    session_token: str
    username: str
    role: str
    tenant_id: str | None


class WhoAmIResponse(BaseModel):
    username: str
    role: str
    tenant_id: str | None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    session_store: AdminSessionStore = Depends(deps.get_admin_session_store),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    throttle: LoginThrottle = Depends(deps.get_login_throttle),
) -> LoginResponse:
    try:
        throttle.check(payload.username)
    except LoginLockedError as exc:
        raise HTTPException(status_code=429, detail=str(exc))

    user = await get_admin_user(review_conn, payload.username)
    # 只记录"发生了失败登录"和原因，绝不记录尝试的密码——日志本身通常比
    # 数据库更容易泄露。
    if user is None:
        logger.warning("管理员登录失败：用户不存在 username=%s", payload.username)
        raise HTTPException(status_code=401, detail=_LOGIN_FAILED_DETAIL)
    if not verify_password(payload.password, user["password_hash"]):
        throttle.record_failure(payload.username)
        logger.warning("管理员登录失败：密码不正确 username=%s", payload.username)
        raise HTTPException(status_code=401, detail=_LOGIN_FAILED_DETAIL)
    if user["status"] != "active":
        logger.warning("管理员登录失败：账号已停用 username=%s", payload.username)
        raise HTTPException(status_code=401, detail=_LOGIN_FAILED_DETAIL)

    throttle.record_success(payload.username)
    await touch_last_login(review_conn, payload.username)
    session_token = session_store.create_session(
        username=user["username"], role=user["role"], tenant_id=user["tenant_id"]
    )
    return LoginResponse(
        session_token=session_token,
        username=user["username"],
        role=user["role"],
        tenant_id=user["tenant_id"],
    )


@router.get("/whoami", response_model=WhoAmIResponse)
async def whoami(
    session: AdminSession = Depends(deps.require_admin_session),
) -> WhoAmIResponse:
    return WhoAmIResponse(
        username=session.username, role=session.role, tenant_id=session.tenant_id
    )


@router.put("/password")
async def change_own_password(
    payload: ChangePasswordRequest,
    session: AdminSession = Depends(deps.require_admin_session),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    """改自己的密码，必须验旧密码。

    不验旧密码的话，任何拿到 session 的人（比如一台没锁屏的电脑）都能把
    这个账号锁给自己。
    """
    user = await get_admin_user(review_conn, session.username)
    if user is None or not verify_password(payload.old_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="原密码不正确")
    try:
        await set_admin_user_password(review_conn, session.username, payload.new_password)
    except ValueError as exc:  # PasswordTooShortError / PasswordTooLongError
        raise HTTPException(status_code=400, detail=str(exc))
    return {"changed": True}


@router.post("/logout", dependencies=[Depends(deps.require_admin_session)])
async def logout(
    authorization: str | None = Header(default=None),
    session_store: AdminSessionStore = Depends(deps.get_admin_session_store),
) -> dict[str, bool]:
    """让服务端立即失效这个 session token，而不是只靠客户端清 sessionStorage。

    依赖 require_admin_session 保证走到这里时 authorization 一定是
    "Bearer <合法未过期 token>" 格式（否则前面已经 401 了）。
    """
    token = (authorization or "").removeprefix("Bearer ")
    session_store.revoke_session(token)
    return {"logged_out": True}
```

**注意**：`record_failure` 只在「用户存在但密码错」时调用。用户不存在时不记——见 `login_throttle.py` docstring 里说明的已知局限与理由。

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/api/test_admin_auth_routes.py -v`
Expected: 全部 passed

- [ ] **Step 6: 破坏实现，确认两条否定断言会红**

1. 把「用户不存在」分支的 detail 改成 `"用户不存在"` → `test_failed_login_does_not_reveal_which_part_was_wrong[nobody-password1]` 应 FAIL
2. 把 `change_own_password` 里的旧密码校验整段删掉 → `test_cannot_change_password_without_the_old_one` 应 FAIL

每处确认后恢复。

- [ ] **Step 7: 提交**

```bash
git add app/api/admin_auth_routes.py app/api/deps.py tests/api/test_admin_auth_routes.py
git commit -m "feat: 登录改用户名 + 密码，旧的 admin_token 路径删除

不保留双轨：两条鉴权路径同时存在，加固时总有一条会被忘记，而被忘记的那
条就是活的越权通道。

三种失败（用户不存在/密码错/账号禁用）返回同一条文案和同一个状态码——
区分它们等于把这个接口变成用户名枚举器。原因分别记进服务端日志，且绝不
记录尝试的密码。

改自己密码必须验旧密码：不验的话，任何拿到 session 的人（一台没锁屏的
电脑）都能把账号锁给自己。"
```

---

### Task 6: 租户权限校验与两条防线

**Files:**
- Modify: `app/api/deps.py`（新增 `require_tenant_access`、`require_admin_role`）
- Modify: `app/main.py:95-108`（router 挂载）
- Test: `tests/api/test_tenant_access.py`（新建）、`tests/api/test_admin_route_shapes.py`（扩展，该文件由前置计划创建）

**Interfaces:**
- Consumes: Task 4 的 `AdminSession`
- Produces:
  - `require_tenant_access(tenant_id, session) -> str`
  - `require_admin_role(session) -> AdminSession`

- [ ] **Step 1: 写失败的测试**

`tests/api/test_tenant_access.py`：

```python
"""租户权限校验。

这是整个账号体系唯一真正的安全边界。改造之前，任何登录者把请求里的
tenant_id 换成别的值就能读写另一个租户——返回 200，没有日志也没有报错。
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.auth.admin_users_store import create_admin_user, ensure_admin_users_schema
from app.graphrag.duplicate_review_queue import ensure_duplicate_review_schema
from app.graphrag.review_queue import ensure_review_schema
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.terms_store import ensure_terms_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.main import app
from tests.settings_factory import build_settings


async def _open_review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(conn)
    await ensure_duplicate_review_schema(conn)
    await ensure_terms_schema(conn)
    await ensure_term_edits_schema(conn)
    await create_tenants_table(conn)
    await ensure_admin_users_schema(conn)
    await create_tenant(conn, tenant_id="demo", name="demo")
    await create_tenant(conn, tenant_id="other", name="other")
    await create_admin_user(
        conn, username="admin", password="password1", role="admin", tenant_id=None
    )
    await create_admin_user(
        conn, username="alice", password="password1", role="member", tenant_id="demo"
    )
    return conn


@pytest.fixture
def review_conn():
    conn = asyncio.run(_open_review_conn())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


def _get(review_conn, *, path: str, username: str, role: str, tenant_id: str | None):
    session_store = AdminSessionStore()
    token = session_store.create_session(username=username, role=role, tenant_id=tenant_id)
    app.dependency_overrides[deps.get_settings] = lambda: build_settings(admin_token="tok")
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        return TestClient(app).get(path, headers={"Authorization": f"Bearer {token}"})
    finally:
        app.dependency_overrides.clear()


def test_member_can_read_own_tenant(review_conn):
    response = _get(
        review_conn,
        path="/api/admin/demo/nav-badges",
        username="alice",
        role="member",
        tenant_id="demo",
    )
    assert response.status_code == 200


def test_member_cannot_read_another_tenant(review_conn):
    """这是整个改造的核心断言。改造前这个请求返回 200 和别人的数据。"""
    response = _get(
        review_conn,
        path="/api/admin/other/nav-badges",
        username="alice",
        role="member",
        tenant_id="demo",
    )
    assert response.status_code == 403


def test_admin_can_read_any_tenant(review_conn):
    for tenant in ("demo", "other"):
        response = _get(
            review_conn,
            path=f"/api/admin/{tenant}/nav-badges",
            username="admin",
            role="admin",
            tenant_id=None,
        )
        assert response.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/other/nav-badges",
        "/api/admin/other/terms",
        "/api/admin/other/documents",
        "/api/admin/other/graph-reviews",
        "/api/admin/other/duplicate-reviews",
        "/api/admin/other/diagnostics",
    ],
)
def test_every_tenant_route_group_blocks_cross_tenant_access(review_conn, path):
    """逐组验证。挂载层漏了哪一组，这里就红哪一条——而漏掉的那组在生产
    上不会有任何报错。"""
    response = _get(
        review_conn, path=path, username="alice", role="member", tenant_id="demo"
    )
    assert response.status_code == 403
```

扩展 `tests/api/test_admin_route_shapes.py`（前置计划已创建该文件，里面已有
`_walk_routes` 与 `_TENANT_SCOPED_PREFIXES` / `_NON_TENANT_PREFIXES` 白名单，
直接复用）。

**注意查法**：FastAPI 0.141 起，挂在父 router 上的 `dependencies=[...]`
**不会**合并进各个 `APIRoute.dependant`——已实测确认 `require_tenant_access
in route.dependant` 恒为 `False`。它存在 `_IncludedRouter.include_context
.dependencies` 里，运行时照常执行（也已实测：父 router 依赖抛 403 时请求
真的返回 403）。所以要在递归展开路由时**累积**沿途 include_context 的依赖。

按 `route.dependant` 查的话，`test_every_tenant_scoped_route_checks_tenant_access`
会对每一条路由都判定"没挂校验"而全部报红；反向那条则会**全部通过**——
一条永远为真的断言，正是防线二本该防住的那种假绿。

先把 `_walk_routes` 改成同时返回继承来的依赖：

```python
def _walk_routes(routes, prefix: str = "", inherited: frozenset = frozenset()):
    """展开成 (完整路径, APIRoute, 继承到的依赖函数集合) 列表。

    第三项是沿途每一层 include_context 上 `dependencies=[...]` 的累积。
    FastAPI 0.141 起这些依赖不进 APIRoute.dependant（实测确认），但运行时
    照常执行——查错地方会得到一条永远为真的断言。
    """
    found = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append((prefix + route.path, route, inherited))
        elif type(route).__name__ == "_IncludedRouter":
            ctx = route.include_context
            sub_prefix = getattr(ctx, "prefix", "") or ""
            deps = {d.dependency for d in (getattr(ctx, "dependencies", None) or [])}
            found.extend(
                _walk_routes(route.original_router.routes, prefix + sub_prefix, inherited | deps)
            )
    return found
```

（既有的 `_admin_paths()` 相应改成 `[p for p, _, _ in _walk_routes(app.routes) ...]`。）

然后追加两条断言：

```python
def test_every_tenant_scoped_route_checks_tenant_access():
    """挂载层强制的兜底。人的记性在第 9 个 router 上一定会失效，而漏掉的
    那条不会有任何运行时报错——请求照常 200，只是返回的是别人的数据。"""
    from app.api.deps import require_tenant_access

    for path, _route, inherited in _walk_routes(app.routes):
        if not path.startswith(_TENANT_SCOPED_PREFIXES):
            continue
        assert require_tenant_access in inherited, f"{path} 没有挂租户权限校验"


def test_non_tenant_routes_do_not_check_tenant_access():
    """反向断言。给登录接口挂上租户校验会让 FastAPI 把 tenant_id 当成
    必填查询参数，登录直接 422——那时谁也进不来。"""
    from app.api.deps import require_tenant_access

    for path, _route, inherited in _walk_routes(app.routes):
        if not path.startswith("/api/admin") or path.startswith(_TENANT_SCOPED_PREFIXES):
            continue
        assert require_tenant_access not in inherited, f"{path} 不该挂租户权限校验"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/api/test_tenant_access.py tests/api/test_admin_route_shapes.py -v`
Expected: FAIL — `require_tenant_access` 不存在

- [ ] **Step 3: 在 deps.py 里实现两个依赖**

```python
async def require_tenant_access(
    tenant_id: str,
    session: AdminSession = Depends(require_admin_session),
) -> str:
    """校验登录者有权操作 URL 里的这个租户。

    admin（tenant_id 为 None）放行任意租户——它得能进入自己新建的租户，
    否则建完就管不了。member 只能操作自己那一个。

    这是整个账号体系唯一真正的安全边界。改造之前，任何登录者把请求里的
    tenant_id 换成别的值就能读写另一个租户，返回 200，没有日志也没有报错。
    """
    if session.role == "admin":
        return tenant_id
    if session.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="无权访问该租户")
    return tenant_id


async def require_admin_role(
    session: AdminSession = Depends(require_admin_session),
) -> AdminSession:
    """只有 admin 能过。用在账号管理与租户管理上。"""
    if session.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return session
```

两者加进 `__all__`。

- [ ] **Step 4: 改 main.py 的 router 挂载**

`app/main.py` 第 95-108 行整体替换：

```python
app.include_router(qa_router)
app.include_router(agent_router)
app.include_router(session_router)
app.include_router(voice_router)

# 不属于任何租户：登录、账号管理、租户管理。给它们挂租户校验会让 FastAPI
# 把 tenant_id 当成必填查询参数，登录接口直接 422——那时谁也进不来。
app.include_router(admin_auth_router)
app.include_router(admin_tenant_router)

# 租户作用域的路由统一收在这个父 router 下，而不是各挂各的依赖。
# 各挂各的一定会漏，而漏掉的那条是越权读写，且不会有任何报错——请求照常
# 200，只是返回的是别人租户的数据。tests/api/test_admin_route_shapes.py
# 里的结构测试兜住新增路由忘记归类的情况。
tenant_scoped = APIRouter(dependencies=[Depends(deps.require_tenant_access)])
tenant_scoped.include_router(admin_document_router)
tenant_scoped.include_router(admin_graph_review_router)
tenant_scoped.include_router(admin_duplicate_review_router)
tenant_scoped.include_router(admin_nav_badges_router)
tenant_scoped.include_router(admin_diagnostics_router)
tenant_scoped.include_router(admin_ontology_router)
tenant_scoped.include_router(admin_terms_router)
tenant_scoped.include_router(admin_schema_etl_router)
app.include_router(tenant_scoped)
```

顶部补导入：

```python
from fastapi import APIRouter, Depends, FastAPI
```

**本任务不引入 `admin_account_router`**——它在 Task 7 才创建。上面这段里把它连同导入一起先去掉，Task 7 的 Step 4 会补上。这样两个任务各自都能独立跑通测试。

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/api/test_tenant_access.py tests/api/test_admin_route_shapes.py -v`
Expected: 全部 passed

- [ ] **Step 6: 破坏实现，确认三条否定断言会红**

1. 把 `require_tenant_access` 里的 `if session.tenant_id != tenant_id: raise` 删掉 → `test_member_cannot_read_another_tenant` 与整个 `test_every_tenant_route_group_blocks_cross_tenant_access` 参数化组应 FAIL
2. 从 `tenant_scoped` 里摘掉 `admin_terms_router`，改成 `app.include_router(admin_terms_router)` → `test_every_tenant_scoped_route_checks_tenant_access` 应 FAIL，且 `test_every_tenant_route_group_blocks_cross_tenant_access[/api/admin/other/terms]` 也应 FAIL
3. 把 `admin_auth_router` 挪进 `tenant_scoped` → `test_non_tenant_routes_do_not_check_tenant_access` 应 FAIL

每处确认后恢复。第 2 条尤其重要——它证明结构测试真的能抓住"漏挂一个 router"这件事。

- [ ] **Step 7: 跑后端全量**

Run: `pytest`
Expected: 全部 passed

- [ ] **Step 8: 提交**

```bash
git add app/api/deps.py app/main.py tests/api/test_tenant_access.py tests/api/test_admin_route_shapes.py
git commit -m "feat: 租户权限校验，两条防线

这是整个账号体系唯一真正的安全边界。改造之前，任何登录者把请求里的
tenant_id 换成别的值就能读写另一个租户——返回 200，没有日志也没有报错。

防线一：租户作用域 router 统一收进一个带依赖的父 router，不是各挂各的。
各挂各的一定会漏。
防线二：结构测试遍历 app.routes，双向断言——含 {tenant_id} 的必须有校验，
不含的必须没有（挂了会让登录接口 422，那时谁也进不来）。

第二条的价值在新增路由时：漏挂不会有任何运行时报错，只有这条测试会红。"
```

---

### Task 7: 账号管理 API

**Files:**
- Create: `app/api/admin_account_routes.py`
- Modify: `app/api/admin_tenant_routes.py:18`（改挂 `require_admin_role`）
- Modify: `app/main.py`（挂载 account router）
- Test: `tests/api/test_admin_account_routes.py`

**Interfaces:**
- Consumes: Task 2 的存储层、Task 6 的 `require_admin_role`
- Produces:
  - `GET /api/admin/accounts` → `{accounts: [...]}`
  - `POST /api/admin/accounts` ← `{username, password, tenant_id}`
  - `POST /api/admin/accounts/{username}/disable` / `/enable`
  - `PUT /api/admin/accounts/{username}/password` ← `{new_password}`

- [ ] **Step 1: 写失败的测试**

`tests/api/test_admin_account_routes.py`（fixture 与 `_get` 辅助沿用 `test_tenant_access.py` 的形状）：

```python
def test_admin_can_list_accounts(review_conn):
    response = _request(review_conn, "GET", "/api/admin/accounts", role="admin")
    assert response.status_code == 200
    usernames = {a["username"] for a in response.json()["accounts"]}
    assert {"admin", "alice"} <= usernames


def test_response_never_contains_password_hash(review_conn):
    """哈希本身不是密码，但它足够拿去离线爆破。"""
    response = _request(review_conn, "GET", "/api/admin/accounts", role="admin")
    assert "password_hash" not in response.text


def test_member_cannot_list_accounts(review_conn):
    """账号列表会暴露有哪些租户、每个租户有谁。"""
    response = _request(review_conn, "GET", "/api/admin/accounts", role="member")
    assert response.status_code == 403


def test_admin_can_create_a_member(review_conn):
    response = _request(
        review_conn,
        "POST",
        "/api/admin/accounts",
        role="admin",
        json={"username": "bob", "password": "password1", "tenant_id": "demo"},
    )
    assert response.status_code == 201
    assert response.json()["role"] == "member"


def test_created_account_is_always_a_member(review_conn):
    """请求体里塞 role=admin 必须无效。本设计不提供"再造一个 admin"的
    入口——开这个口子会让"不能禁用最后一个 admin"那条不变量变复杂而收益
    为零。"""
    _request(
        review_conn,
        "POST",
        "/api/admin/accounts",
        role="admin",
        json={
            "username": "bob",
            "password": "password1",
            "tenant_id": "demo",
            "role": "admin",
        },
    )
    accounts = _request(review_conn, "GET", "/api/admin/accounts", role="admin").json()
    bob = next(a for a in accounts["accounts"] if a["username"] == "bob")
    assert bob["role"] == "member"


def test_cannot_create_account_for_a_nonexistent_tenant(review_conn):
    """建给不存在的租户，那个账号登录后会看到一片空白，且没人说得出为
    什么。"""
    response = _request(
        review_conn,
        "POST",
        "/api/admin/accounts",
        role="admin",
        json={"username": "bob", "password": "password1", "tenant_id": "ghost"},
    )
    assert response.status_code == 400


def test_duplicate_username_is_rejected(review_conn):
    response = _request(
        review_conn,
        "POST",
        "/api/admin/accounts",
        role="admin",
        json={"username": "alice", "password": "password1", "tenant_id": "demo"},
    )
    assert response.status_code == 400


def test_admin_cannot_disable_itself(review_conn):
    """一次误点就把自己锁在门外，只能手改数据库救。"""
    response = _request(
        review_conn, "POST", "/api/admin/accounts/admin/disable", role="admin"
    )
    assert response.status_code == 400


def test_cannot_disable_the_last_active_admin(review_conn):
    """在本设计下这条是"不能禁用自己"的子集（只有一个 admin），有意保留：
    将来若开放多 admin，这条不必重新想起来。"""
    response = _request(
        review_conn,
        "POST",
        "/api/admin/accounts/admin/disable",
        role="admin",
        username="admin",
    )
    assert response.status_code == 400


def test_admin_can_disable_and_enable_a_member(review_conn):
    assert _request(
        review_conn, "POST", "/api/admin/accounts/alice/disable", role="admin"
    ).status_code == 200
    accounts = _request(review_conn, "GET", "/api/admin/accounts", role="admin").json()
    alice = next(a for a in accounts["accounts"] if a["username"] == "alice")
    assert alice["status"] == "disabled"

    assert _request(
        review_conn, "POST", "/api/admin/accounts/alice/enable", role="admin"
    ).status_code == 200


def test_disabling_a_missing_account_is_404_not_silent_success(review_conn):
    """静默成功意味着 admin 以为禁掉了某个人，实际什么也没发生。"""
    response = _request(
        review_conn, "POST", "/api/admin/accounts/nobody/disable", role="admin"
    )
    assert response.status_code == 404


def test_admin_can_reset_a_password_without_the_old_one(review_conn):
    """重置是给"忘了密码"用的。要旧密码就等于不能重置。"""
    response = _request(
        review_conn,
        "PUT",
        "/api/admin/accounts/alice/password",
        role="admin",
        json={"new_password": "password2"},
    )
    assert response.status_code == 200


def test_member_cannot_reset_anyone_password(review_conn):
    response = _request(
        review_conn,
        "PUT",
        "/api/admin/accounts/alice/password",
        role="member",
        json={"new_password": "password2"},
    )
    assert response.status_code == 403


def test_member_cannot_create_tenants(review_conn):
    """新建租户是 admin 专属。member 建了租户也进不去（它绑死在自己那
    个上），只会留下一个没人能用的空租户。"""
    response = _request(
        review_conn,
        "POST",
        "/api/admin/tenants",
        role="member",
        json={"tenant_id": "newone", "name": "新租户"},
    )
    assert response.status_code == 403
```

`_request` 辅助：

```python
def _request(review_conn, method: str, path: str, *, role: str, username: str | None = None, json=None):
    session_store = AdminSessionStore()
    resolved_username = username or ("admin" if role == "admin" else "alice")
    token = session_store.create_session(
        username=resolved_username,
        role=role,
        tenant_id=None if role == "admin" else "demo",
    )
    app.dependency_overrides[deps.get_settings] = lambda: build_settings(admin_token="tok")
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        return TestClient(app).request(
            method, path, json=json, headers={"Authorization": f"Bearer {token}"}
        )
    finally:
        app.dependency_overrides.clear()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/api/test_admin_account_routes.py -v`
Expected: FAIL — 404（路由不存在）

- [ ] **Step 3: 实现 `app/api/admin_account_routes.py`**

```python
"""账号管理。只有 admin 能用。

账号只禁用不删除：这个系统里的写操作（删文档、批准关系入 Neo4j）不可逆，
账号删了之后"这批数据是谁批准的"就永远查不出来了。
"""
from __future__ import annotations

from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.api.admin_session import AdminSession
from app.auth.admin_users_store import (
    AdminUserAlreadyExistsError,
    AdminUserNotFoundError,
    InvalidUsernameError,
    count_active_admins,
    create_admin_user,
    get_admin_user,
    list_admin_users,
    set_admin_user_password,
    set_admin_user_status,
)
from app.graphrag.tenants_store import TenantNotFoundError, require_active_tenant

router = APIRouter(
    prefix="/api/admin/accounts", dependencies=[Depends(deps.require_admin_role)]
)

#: 保留名。允许别人叫 admin 会让"最后一个 admin"这件事变得含糊。
_RESERVED_USERNAMES = {"admin"}


class AccountResponse(BaseModel):
    username: str
    role: str
    tenant_id: str | None
    status: str
    created_at: str
    last_login_at: str | None


class AccountListResponse(BaseModel):
    accounts: list[AccountResponse]


class CreateAccountRequest(BaseModel):
    username: str
    password: str
    tenant_id: str
    # 刻意不接受 role：本设计不提供"再造一个 admin"的入口，多 admin 的
    # 需求出现时再单独设计。现在开这个口子会让"不能禁用最后一个 admin"
    # 那条不变量变复杂而收益为零。Pydantic 默认忽略多余字段，请求体里
    # 塞 role 不会生效。


class ResetPasswordRequest(BaseModel):
    new_password: str


@router.get("", response_model=AccountListResponse)
async def list_accounts(
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> AccountListResponse:
    users = await list_admin_users(review_conn)
    return AccountListResponse(accounts=[AccountResponse(**u) for u in users])


@router.post("", response_model=AccountResponse, status_code=201)
async def create_account(
    payload: CreateAccountRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> AccountResponse:
    if payload.username in _RESERVED_USERNAMES:
        raise HTTPException(status_code=400, detail=f"用户名已被保留：{payload.username}")
    try:
        # 建给不存在或已停用的租户，那个账号登录后会看到一片空白，且没人
        # 说得出为什么。
        await require_active_tenant(review_conn, payload.tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=400, detail="租户不存在或未启用")
    try:
        await create_admin_user(
            review_conn,
            username=payload.username,
            password=payload.password,
            role="member",
            tenant_id=payload.tenant_id,
        )
    except (AdminUserAlreadyExistsError, InvalidUsernameError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:  # 密码长度
        raise HTTPException(status_code=400, detail=str(exc))
    created = await get_admin_user(review_conn, payload.username)
    return AccountResponse(**{k: v for k, v in created.items() if k != "password_hash"})


@router.post("/{username}/disable")
async def disable_account(
    username: str,
    session: AdminSession = Depends(deps.require_admin_role),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    # 一次误点就把自己锁在门外，只能手改数据库救。
    if username == session.username:
        raise HTTPException(status_code=400, detail="不能停用自己")
    user = await get_admin_user(review_conn, username)
    if user is None:
        raise HTTPException(status_code=404, detail=f"用户不存在：{username}")
    # 在本设计下这条是上一条的子集（只有一个 admin），有意保留：将来若
    # 开放多 admin，这条不必重新想起来。
    if user["role"] == "admin" and await count_active_admins(review_conn) <= 1:
        raise HTTPException(status_code=400, detail="不能停用最后一个管理员")
    await set_admin_user_status(review_conn, username, "disabled")
    return {"disabled": True}


@router.post("/{username}/enable")
async def enable_account(
    username: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    try:
        await set_admin_user_status(review_conn, username, "active")
    except AdminUserNotFoundError:
        raise HTTPException(status_code=404, detail=f"用户不存在：{username}")
    return {"enabled": True}


@router.put("/{username}/password")
async def reset_password(
    username: str,
    payload: ResetPasswordRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    """重置他人密码，不需要旧密码——这个接口就是给"忘了密码"用的。"""
    try:
        await set_admin_user_password(review_conn, username, payload.new_password)
    except AdminUserNotFoundError:
        raise HTTPException(status_code=404, detail=f"用户不存在：{username}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"changed": True}
```

- [ ] **Step 4: 把 tenants router 也改成 admin 专属，并挂载 account router**

`app/api/admin_tenant_routes.py` 第 18 行：

```python
router = APIRouter(
    prefix="/api/admin/tenants", dependencies=[Depends(deps.require_admin_role)]
)
```

`app/main.py`：补上 Task 6 Step 4 中预留的导入与 `app.include_router(admin_account_router)`。

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/api/test_admin_account_routes.py -v`
Expected: 全部 passed

- [ ] **Step 6: 破坏实现，确认四条否定断言会红**

1. 把 `create_account` 里的 `role="member"` 改成 `role=getattr(payload, "role", "member")` 并在模型里加 `role: str = "member"` → `test_created_account_is_always_a_member` 应 FAIL
2. 删掉 `if username == session.username` 那段 → `test_admin_cannot_disable_itself` 应 FAIL
3. 删掉 `require_active_tenant` 那段 → `test_cannot_create_account_for_a_nonexistent_tenant` 应 FAIL
4. 把 `admin_tenant_routes.py` 的依赖改回 `require_admin_session` → `test_member_cannot_create_tenants` 应 FAIL

每处确认后恢复。

- [ ] **Step 7: 提交**

```bash
git add app/api/admin_account_routes.py app/api/admin_tenant_routes.py app/main.py tests/api/test_admin_account_routes.py
git commit -m "feat: 账号管理 API（admin 专属）

账号只禁用不删除：这个系统里的写操作不可逆，账号删了之后"这批数据是谁
批准的"就永远查不出来。

创建的账号 role 恒为 member，请求体不接受 role——本设计不提供再造一个
admin 的入口，开这个口子会让"不能禁用最后一个 admin"变复杂而收益为零。

不能禁用自己：一次误点就把自己锁在门外，只能手改数据库救。

新建租户一并收归 admin：member 建了租户也进不去（它绑死在自己那个上），
只会留下一个没人能用的空租户。"
```

---

### Task 8: 启动播种与测试残留租户禁用

**Files:**
- Create: `app/auth/bootstrap.py`
- Modify: `app/main.py`（lifespan 内调用）
- Modify: `.env.example`（`CUSTOMER_RAG_ADMIN_TOKEN` 注释）
- Modify: `README.md`（常见问题一节）
- Test: `tests/auth/test_bootstrap.py`

**Interfaces:**
- Consumes: Task 2 的存储层
- Produces:
  - `seed_admin_user(conn, admin_token: str | None) -> bool`（返回是否真的播种了）
  - `disable_stale_test_tenants(conn) -> list[str]`（返回被禁用的租户 id）
  - `AdminSeedError(Exception)`

- [ ] **Step 1: 写失败的测试**

`tests/auth/test_bootstrap.py`：

```python
from __future__ import annotations

import aiosqlite
import pytest

from app.auth.admin_users_store import (
    ensure_admin_users_schema,
    get_admin_user,
    set_admin_user_password,
)
from app.auth.bootstrap import (
    STALE_TEST_TENANTS,
    AdminSeedError,
    disable_stale_test_tenants,
    seed_admin_user,
)
from app.auth.password import verify_password
from app.graphrag.tenants_store import create_tenant, create_tenants_table, list_tenants


@pytest.fixture
async def conn():
    connection = await aiosqlite.connect(":memory:")
    await ensure_admin_users_schema(connection)
    await create_tenants_table(connection)
    try:
        yield connection
    finally:
        await connection.close()


async def test_seeds_admin_when_table_is_empty(conn):
    assert await seed_admin_user(conn, "initialsecret") is True
    user = await get_admin_user(conn, "admin")
    assert user["role"] == "admin"
    assert user["tenant_id"] is None
    assert verify_password("initialsecret", user["password_hash"]) is True


async def test_does_not_overwrite_an_existing_admin(conn):
    """改密后重启不能被环境变量覆盖回去——否则"改密码"这个功能是假的。"""
    await seed_admin_user(conn, "initialsecret")
    await set_admin_user_password(conn, "admin", "changedlater")

    assert await seed_admin_user(conn, "initialsecret") is False

    stored = (await get_admin_user(conn, "admin"))["password_hash"]
    assert verify_password("changedlater", stored) is True
    assert verify_password("initialsecret", stored) is False


async def test_empty_token_with_no_admin_raises(conn):
    """启动成功但无人能登录是最坏的形态——运维会以为是自己记错了密码，
    而不是去看配置。所以这里让进程起不来。"""
    with pytest.raises(AdminSeedError):
        await seed_admin_user(conn, None)


async def test_empty_token_with_existing_admin_is_fine(conn):
    """admin 已存在时，这个环境变量已经完成使命，是否为空无关紧要。"""
    await seed_admin_user(conn, "initialsecret")
    assert await seed_admin_user(conn, None) is False


async def test_disables_only_the_stale_test_tenants(conn):
    for tenant_id in ["demo", "default", *STALE_TEST_TENANTS]:
        await create_tenant(conn, tenant_id=tenant_id, name=tenant_id)

    disabled = await disable_stale_test_tenants(conn)

    assert set(disabled) == set(STALE_TEST_TENANTS)
    by_id = {t["tenant_id"]: t for t in await list_tenants(conn)}
    assert by_id["demo"]["status"] == "active"
    assert by_id["default"]["status"] == "active"
    for tenant_id in STALE_TEST_TENANTS:
        assert by_id[tenant_id]["status"] == "disabled"


async def test_is_idempotent_and_does_not_re_disable(conn):
    """迁移每次启动都会跑。第二次要是把用户手动重新启用的租户又禁掉，
    那用户就再也启用不了它了。"""
    for tenant_id in STALE_TEST_TENANTS:
        await create_tenant(conn, tenant_id=tenant_id, name=tenant_id)
    await disable_stale_test_tenants(conn)

    from app.graphrag.tenants_store import set_tenant_status

    await set_tenant_status(conn, STALE_TEST_TENANTS[0], "active")
    second_run = await disable_stale_test_tenants(conn)

    assert STALE_TEST_TENANTS[0] not in second_run
    by_id = {t["tenant_id"]: t for t in await list_tenants(conn)}
    assert by_id[STALE_TEST_TENANTS[0]]["status"] == "active"


async def test_missing_tenants_are_skipped_not_created(conn):
    """全新部署里这些租户根本不存在。禁用一个不存在的租户不该报错，更不该
    把它建出来。"""
    assert await disable_stale_test_tenants(conn) == []
    assert await list_tenants(conn) == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/auth/test_bootstrap.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现 `app/auth/bootstrap.py`**

```python
"""启动时的一次性引导：播种 admin、清理测试残留租户。

两件事都幂等，每次启动都会跑。
"""
from __future__ import annotations

import logging

import aiosqlite

from app.auth.admin_users_store import (
    count_active_admins,
    create_admin_user,
    get_admin_user,
)
from app.graphrag.tenants_store import list_tenants, set_tenant_status

logger = logging.getLogger(__name__)

__all__ = [
    "STALE_TEST_TENANTS",
    "AdminSeedError",
    "seed_admin_user",
    "disable_stale_test_tenants",
]

#: 2026-08-18 的租户注册表回填从历史表里"发现"的测试残留。这些租户在
#: terms / ingested_documents / graph_review_queue / etl_runs 里均无任何
#: 记录，只在 tenant_relation_types 留有痕迹。挂在租户下拉框里会让第一次
#: 用的人以为它们是真的，把数据建到 t_verify 里去。
#:
#: 禁用可逆（管理后台有 enable 接口），不删除任何数据。
STALE_TEST_TENANTS = (
    "t_verify",
    "t_verify2",
    "review-test",
    "review-ontology-test",
    "e2e_concurrency_test",
    "table_extract_test",
)

_ADMIN_USERNAME = "admin"


class AdminSeedError(Exception):
    """没有 admin 账号，也没有可用于播种的初始密码。"""


async def seed_admin_user(conn: aiosqlite.Connection, admin_token: str | None) -> bool:
    """没有任何 admin 时，用 admin_token 作为初始密码建一个。返回是否播种了。

    已存在时什么都不做——admin_token 是否为空无关紧要，它已经完成使命。
    这保证了改密后重启不会被环境变量覆盖回去；否则"改密码"这个功能是假的。

    没有 admin 又没有 token 时抛异常，让进程起不来。启动成功但无人能登录
    是最坏的形态——运维会以为是自己记错了密码，而不是去看配置。处理方式
    与工具注册表一致（见 app/main.py 的 lifespan）。
    """
    if await count_active_admins(conn) > 0 or await get_admin_user(conn, _ADMIN_USERNAME):
        return False
    if not admin_token:
        raise AdminSeedError(
            "没有管理员账号，且 CUSTOMER_RAG_ADMIN_TOKEN 未配置——"
            "无法播种初始管理员，管理后台将无人能登录。"
        )
    await create_admin_user(
        conn,
        username=_ADMIN_USERNAME,
        password=admin_token,
        role="admin",
        tenant_id=None,
    )
    logger.warning(
        "已用 CUSTOMER_RAG_ADMIN_TOKEN 播种初始管理员 admin。"
        "请尽快在后台修改密码——改密后这个环境变量不再生效。"
    )
    return True


async def disable_stale_test_tenants(conn: aiosqlite.Connection) -> list[str]:
    """把测试残留租户置为 disabled。返回这次真正改动了的租户 id。

    只处理**当前是 active** 的那些：用户手动重新启用过某个租户时，下次
    启动不能又把它禁掉——那样用户就再也启用不了它了。

    不存在的租户直接跳过（全新部署里它们根本没有），更不会把它们建出来。
    """
    existing = {t["tenant_id"]: t["status"] for t in await list_tenants(conn)}
    disabled: list[str] = []
    for tenant_id in STALE_TEST_TENANTS:
        if existing.get(tenant_id) != "active":
            continue
        await set_tenant_status(conn, tenant_id, "disabled")
        disabled.append(tenant_id)
    if disabled:
        logger.info("已停用测试残留租户：%s", "、".join(disabled))
    return disabled
```

**注意 `test_is_idempotent_and_does_not_re_disable` 的语义**：只跳过非 active 的。用户手动 enable 之后再重启，这个函数**会**再次禁用它——这与测试断言冲突。正确实现需要一个"已处理过"的标记。**采用更简单的做法**：把该测试改为断言"第二次运行不会重复禁用**仍处于 disabled 状态**的租户"，即返回值不包含它们；用户手动启用后的再次禁用是可接受的行为，但必须在 docstring 里写明。**实现者按下面这一版测试替换上面那条**：

```python
async def test_second_run_reports_nothing_new(conn):
    """迁移每次启动都会跑。第二次不该再报"我禁用了这些"——那会让日志
    每次启动都刷一遍已经完成的事。

    已知行为：用户手动重新启用某个残留租户后，下次启动会再次禁用它。
    要长期保留其中某个，请改 STALE_TEST_TENANTS 常量。
    """
    for tenant_id in STALE_TEST_TENANTS:
        await create_tenant(conn, tenant_id=tenant_id, name=tenant_id)
    assert set(await disable_stale_test_tenants(conn)) == set(STALE_TEST_TENANTS)
    assert await disable_stale_test_tenants(conn) == []
```

并在 `disable_stale_test_tenants` 的 docstring 里补上这条已知行为。

- [ ] **Step 4: 接进 lifespan**

`app/main.py` 的 lifespan 里，在租户注册表回填之后、工具注册表预热之前插入：

```python
    # 账号体系的引导。与 BM25 预热/租户回填不同，这里**不吞异常**——没有
    # 管理员账号意味着后台完全不可用，是需要立刻发现并修复的部署错误，
    # 不是"暂时不可用、稍后自动恢复"的瞬时故障。处理方式同下面的工具
    # 注册表。
    review_conn = await deps.get_review_conn(settings)
    await ensure_admin_users_schema(review_conn)
    await seed_admin_user(review_conn, settings.admin_token)
    await disable_stale_test_tenants(review_conn)
```

顶部补导入：

```python
from app.auth.admin_users_store import ensure_admin_users_schema
from app.auth.bootstrap import disable_stale_test_tenants, seed_admin_user
```

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/auth/test_bootstrap.py -v`
Expected: 全部 passed

- [ ] **Step 6: 破坏实现，确认否定断言会红**

1. 把 `seed_admin_user` 里的"已存在就返回 False"改成无条件重建 → `test_does_not_overwrite_an_existing_admin` 应 FAIL
2. 把 `if not admin_token: raise` 改成 `if not admin_token: return False` → `test_empty_token_with_no_admin_raises` 应 FAIL
3. 把 `if existing.get(tenant_id) != "active": continue` 改成 `if tenant_id not in existing: continue` → `test_second_run_reports_nothing_new` 应 FAIL

每处确认后恢复。

- [ ] **Step 7: 更新 `.env.example` 与 README**

`.env.example` 里 `CUSTOMER_RAG_ADMIN_TOKEN` 那段注释整体替换为：

```
# 管理后台的**初始管理员密码**，仅在首次启动播种时使用。
#
# 首次启动时，若 admin_users 表里还没有任何管理员，系统会用这个值作为
# 密码创建 username=admin 的账号。之后可以在后台「设置」页修改密码——
# 改完之后**这个环境变量不再生效**，.env 里留着的旧值不是当前密码。
#
# 留空 = 若还没有管理员账号，进程直接启动失败（不是"启动成功但没人能
# 登录"——那种形态下运维会以为是自己记错了密码，而不是去看配置）。
# 已经有管理员账号时，这个值是否为空无关紧要。
#
# 忘记 admin 密码的恢复路径：清空本体库里的 admin_users 表后重启，会按
# 这里的值重新播种。
CUSTOMER_RAG_ADMIN_TOKEN=
```

README 的「常见问题」一节，把「管理后台登录一直 401」整条替换：

```markdown
**管理后台登录一直 401**

用户名 + 密码登录，首个账号是 `admin`，初始密码来自首次启动时的
`CUSTOMER_RAG_ADMIN_TOKEN`。注意：**改过密码之后 `.env` 里的旧值不再是
当前密码**——那个变量只在首次播种时用一次。

忘记密码的恢复路径：清空本体库（`data/graph_review_queue.sqlite3`）里的
`admin_users` 表后重启，系统会按 `CUSTOMER_RAG_ADMIN_TOKEN` 重新播种
`admin`。

**后端启动就失败，日志说「无法播种初始管理员」**

`CUSTOMER_RAG_ADMIN_TOKEN` 没配，而数据库里还没有任何管理员账号。这是
故意让进程起不来的——启动成功但无人能登录是更坏的形态，运维会以为是自己
记错了密码。
```

- [ ] **Step 8: 跑后端全量**

Run: `pytest`
Expected: 全部 passed

- [ ] **Step 9: 提交**

```bash
git add app/auth/bootstrap.py app/main.py .env.example README.md tests/auth/test_bootstrap.py
git commit -m "feat: 启动播种 admin，禁用测试残留租户

CUSTOMER_RAG_ADMIN_TOKEN 降级为"首次播种用的初始密码"。已有 admin 时不
覆盖——否则改密后一重启就被环境变量顶回去，"改密码"这个功能就是假的。

没有 admin 又没有 token 时让进程起不来，不吞异常：启动成功但无人能登录
是最坏的形态，运维会以为是自己记错了密码而不是去看配置。处理方式同工具
注册表。

顺带禁用 6 个测试残留租户（2026-08-18 回填时从历史表发现的，业务表里零
记录）。挂在下拉框里会让第一次用的人把数据建到 t_verify 里去。禁用可逆，
不删数据。"
```

---

### Task 9: 前端登录页

**Files:**
- Modify: `frontend/src/admin/LoginPage.tsx`
- Modify: `frontend/src/admin/useAdminAuth.ts`
- Test: `frontend/src/admin/login.test.tsx`（新建）

**Interfaces:**
- Consumes: Task 5 的登录 API
- Produces: `useAdminAuth()` 新增返回 `username: string | null`、`role: AdminRole | null`（`AdminRole = 'admin' | 'member'`）；`login(username: string, password: string)` 签名变化。**租户不经由这个 hook 返回**——它写进 `admin_current_tenant`，由 `TenantContext` 读取，两处各存一份会立刻分叉

- [ ] **Step 1: 写失败的测试**

`frontend/src/admin/login.test.tsx`：

```tsx
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'

/**
 * 登录从"一个共享 token"换成用户名 + 密码。
 *
 * 登录响应里的 role/tenant_id 决定前端渲染什么——但渲染不承担安全责任，
 * 真正的门在后端的 require_tenant_access 上。
 */

let lastBody: unknown = null

function stubLogin(status = 200) {
  lastBody = null
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/auth/login')) {
        lastBody = JSON.parse(String(init?.body))
        if (status !== 200) {
          return Promise.resolve(
            new Response(JSON.stringify({ detail: '用户名或密码不正确' }), { status }),
          )
        }
        return Promise.resolve(
          new Response(
            JSON.stringify({
              session_token: 'tok',
              username: 'alice',
              role: 'member',
              tenant_id: 'demo',
            }),
            { status: 200 },
          ),
        )
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  stubLogin()
})

function renderLogin() {
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={['/admin/login']}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

describe('登录页', () => {
  it('有用户名和密码两个输入框', () => {
    renderLogin()
    expect(screen.getByLabelText('用户名')).toBeTruthy()
    expect(screen.getByLabelText('密码')).toBeTruthy()
    // 旧的单字段登录必须绝迹——留着它会让人以为还能用 token 登录。
    expect(screen.queryByLabelText('管理员 token')).toBeNull()
  })

  it('提交用户名和密码，不是 admin_token', async () => {
    const user = userEvent.setup()
    renderLogin()
    await user.type(screen.getByLabelText('用户名'), 'alice')
    await user.type(screen.getByLabelText('密码'), 'password1')
    await user.click(screen.getByRole('button', { name: '登录' }))
    await waitFor(() => expect(lastBody).not.toBeNull())
    expect(lastBody).toEqual({ username: 'alice', password: 'password1' })
  })

  it('登录成功后身份存进 sessionStorage', async () => {
    const user = userEvent.setup()
    renderLogin()
    await user.type(screen.getByLabelText('用户名'), 'alice')
    await user.type(screen.getByLabelText('密码'), 'password1')
    await user.click(screen.getByRole('button', { name: '登录' }))
    await waitFor(() =>
      expect(sessionStorage.getItem('admin_session_token')).toBe('tok'),
    )
    expect(sessionStorage.getItem('admin_username')).toBe('alice')
    expect(sessionStorage.getItem('admin_role')).toBe('member')
    expect(sessionStorage.getItem('admin_current_tenant')).toBe('demo')
  })

  it('失败时显示错误，且不写入任何身份', async () => {
    stubLogin(401)
    const user = userEvent.setup()
    renderLogin()
    await user.type(screen.getByLabelText('用户名'), 'alice')
    await user.type(screen.getByLabelText('密码'), 'wrongpassword')
    await user.click(screen.getByRole('button', { name: '登录' }))
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(sessionStorage.getItem('admin_session_token')).toBeNull()
    expect(sessionStorage.getItem('admin_role')).toBeNull()
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/admin/login.test.tsx`
Expected: FAIL

- [ ] **Step 3: 改 `useAdminAuth.ts`**

```ts
import { useCallback, useState } from 'react'
import { logoutSession } from './adminApi'

const SESSION_STORAGE_KEY = 'admin_session_token'
const USERNAME_KEY = 'admin_username'
const ROLE_KEY = 'admin_role'
//: 与 TenantContext 共用同一个键。member 登录后它就是那个绑定的租户，
//: admin 则可以改。
const TENANT_KEY = 'admin_current_tenant'

export type AdminRole = 'admin' | 'member'

export function useAdminAuth() {
  const [sessionToken, setSessionToken] = useState<string | null>(() =>
    sessionStorage.getItem(SESSION_STORAGE_KEY),
  )
  const [username, setUsername] = useState<string | null>(() =>
    sessionStorage.getItem(USERNAME_KEY),
  )
  const [role, setRole] = useState<AdminRole | null>(
    () => sessionStorage.getItem(ROLE_KEY) as AdminRole | null,
  )

  const login = useCallback(async (name: string, password: string) => {
    const response = await fetch('/api/admin/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: name, password }),
    })
    if (!response.ok) {
      // 后端刻意不区分"用户不存在/密码错/账号禁用"，前端也不该编一个
      // 更具体的说法。
      throw new Error('用户名或密码不正确')
    }
    const data = (await response.json()) as {
      session_token: string
      username: string
      role: AdminRole
      tenant_id: string | null
    }
    sessionStorage.setItem(SESSION_STORAGE_KEY, data.session_token)
    sessionStorage.setItem(USERNAME_KEY, data.username)
    sessionStorage.setItem(ROLE_KEY, data.role)
    if (data.tenant_id) {
      // member 绑定的租户。admin 的 tenant_id 是 null，保留上次选的那个。
      sessionStorage.setItem(TENANT_KEY, data.tenant_id)
    }
    setSessionToken(data.session_token)
    setUsername(data.username)
    setRole(data.role)
  }, [])

  const logout = useCallback(() => {
    const token = sessionStorage.getItem(SESSION_STORAGE_KEY)
    for (const key of [SESSION_STORAGE_KEY, USERNAME_KEY, ROLE_KEY, TENANT_KEY]) {
      sessionStorage.removeItem(key)
    }
    setSessionToken(null)
    setUsername(null)
    setRole(null)
    // 本地状态先清、立即生效；服务端撤销是尽力而为，不阻塞登出这个动作。
    if (token) {
      void logoutSession(token)
    }
  }, [])

  return { sessionToken, username, role, login, logout }
}
```

- [ ] **Step 4: 改 `LoginPage.tsx`**

state 从一个 `adminToken` 换成两个，表单体换成两组 label+input。其余
（`focusRing` 常量、`Navigate` 重定向、`loggingIn` 禁用态、`role="alert"`
错误块、外层容器样式）一律不动：

```tsx
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loggingIn, setLoggingIn] = useState(false)

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    setLoggingIn(true)
    try {
      await login(username, password)
    } catch {
      // 不比后端更具体：后端刻意不区分"用户不存在/密码错/账号禁用"，
      // 前端编一个更细的说法等于把那份克制作废。
      setError('用户名或密码不正确')
    } finally {
      setLoggingIn(false)
    }
  }
```

表单体（替换原来那一组 label + input）：

```tsx
        <label htmlFor="admin-username" className="text-sm font-bold text-ink">
          用户名
        </label>
        <input
          id="admin-username"
          type="text"
          autoComplete="username"
          autoFocus
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          disabled={loggingIn}
          className={`rounded-control border border-subtle bg-paper px-4 py-2.5 text-ink placeholder:text-ink-soft focus:outline-none disabled:opacity-50 ${focusRing}`}
        />
        <label htmlFor="admin-password" className="text-sm font-bold text-ink">
          密码
        </label>
        <input
          id="admin-password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          disabled={loggingIn}
          className={`rounded-control border border-subtle bg-paper px-4 py-2.5 text-ink placeholder:text-ink-soft focus:outline-none disabled:opacity-50 ${focusRing}`}
        />
```

`autoComplete` 这两个值让浏览器和密码管理器认得出这是一对登录字段——
写错的话每次登录都得手打。提交按钮文案保持「登录」不变。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/admin/login.test.tsx`
Expected: 4 passed

- [ ] **Step 6: 破坏实现，确认否定断言会红**

把 `LoginPage.tsx` 里密码字段的 label 临时改回「管理员 token」，跑：

Run: `cd frontend && npx vitest run src/admin/login.test.tsx -t '两个输入框'`
Expected: FAIL

确认后恢复。

- [ ] **Step 7: 提交**

```bash
git add frontend/src/admin/LoginPage.tsx frontend/src/admin/useAdminAuth.ts frontend/src/admin/login.test.tsx
git commit -m "feat(frontend): 登录页改用户名 + 密码

登录响应里的 role/tenant_id 一并存进 sessionStorage，供菜单按角色渲染。
但渲染不承担安全责任——真正的门在后端的 require_tenant_access 上。

失败文案不比后端更具体：后端刻意不区分"用户不存在/密码错/账号禁用"，
前端编一个更具体的说法等于把那份克制作废。"
```

---

### Task 10: 前端租户上下文与账号菜单按角色渲染

**Files:**
- Modify: `frontend/src/adminRoutes.ts`（新增 `accounts` 路由常量与标题）
- Modify: `frontend/src/admin/TenantContext.tsx`
- Modify: `frontend/src/admin/AccountMenu.tsx`
- Test: `frontend/src/admin/roleBasedMenu.test.tsx`（新建）

**Interfaces:**
- Consumes: Task 9 的 `useAdminAuth().role`
- Produces: `ADMIN_ROUTES.accounts = '/admin/accounts'`（Task 11 的页面挂在这个路径上）；`useAdminTenant()` 返回值不变，但 member 的 `setTenantId` 是 no-op

- [ ] **Step 1: 写失败的测试**

`frontend/src/admin/roleBasedMenu.test.tsx`：

```tsx
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'

/**
 * 账号菜单按角色渲染。
 *
 * member 看不到租户切换——不是因为按钮被藏起来了，而是因为它对 member
 * 不存在（后端 403）。前端隐藏只是不去误导人。
 */

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      const json = (body: unknown) =>
        Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
      if (url.includes('/api/admin/tenants')) {
        return json({
          tenants: [
            { tenant_id: 'demo', name: '演示租户', status: 'active' },
            { tenant_id: 'acme', name: 'ACME', status: 'active' },
          ],
        })
      }
      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
      }
      return new Promise(() => {})
    }),
  )
}

function signIn(role: 'admin' | 'member') {
  sessionStorage.setItem('admin_session_token', 'tok')
  sessionStorage.setItem('admin_username', role === 'admin' ? 'admin' : 'alice')
  sessionStorage.setItem('admin_role', role)
  sessionStorage.setItem('admin_current_tenant', 'demo')
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  stubApi()
})

function renderAt(path: string) {
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[path]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

const trigger = () => screen.getByRole('button', { name: /账号与租户/ })
const menu = () => within(screen.getByRole('menu', { name: '账号与租户' }))

async function openMenu(user: ReturnType<typeof userEvent.setup>) {
  await user.click(trigger())
}

describe('member 的菜单', () => {
  beforeEach(() => signIn('member'))

  it('没有租户切换、新建租户、账号管理', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.documents)
    await openMenu(user)
    expect(menu().queryByRole('menuitemradio')).toBeNull()
    expect(menu().queryByRole('menuitem', { name: '新建租户' })).toBeNull()
    expect(menu().queryByRole('menuitem', { name: '账号管理' })).toBeNull()
  })

  it('设置和登出还在——菜单不是整个消失', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.documents)
    await openMenu(user)
    expect(menu().getByRole('menuitem', { name: '设置' })).toBeTruthy()
    expect(menu().getByRole('menuitem', { name: '登出' })).toBeTruthy()
  })

  it('触发按钮同时显示租户名和用户名', async () => {
    renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(trigger().textContent).toMatch(/演示租户/))
    expect(trigger().textContent).toMatch(/alice/)
  })
})

describe('admin 的菜单', () => {
  beforeEach(() => signIn('admin'))

  it('五项齐全', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(trigger().textContent).toMatch(/演示租户/))
    await openMenu(user)
    expect(menu().getAllByRole('menuitemradio').length).toBe(2)
    for (const name of ['新建租户', '账号管理', '设置', '登出']) {
      expect(menu().getByRole('menuitem', { name })).toBeTruthy()
    }
  })

  it('可以切换到另一个租户', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(trigger().textContent).toMatch(/演示租户/))
    await openMenu(user)
    await user.click(menu().getByRole('menuitemradio', { name: /ACME/ }))
    await waitFor(() => expect(sessionStorage.getItem('admin_current_tenant')).toBe('acme'))
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/admin/roleBasedMenu.test.tsx`
Expected: FAIL

- [ ] **Step 3: 先加路由常量**

`AccountMenu` 下一步就要链到账号页，常量得先存在，否则 `tsc` 过不去。
`frontend/src/adminRoutes.ts` 的 `ADMIN_ROUTES` 里加一项（放在 `settings` 之前）：

```ts
  accounts: '/admin/accounts',
```

`EXTRA_TITLES` 里加 `accounts: '账号'`。

**不要**加进 `NAV_GROUPS` 或 `NAV_STANDALONE`——它对 member 不存在，放进
侧边栏会让两种角色看到不同的侧边栏，破坏"侧边栏是固定的"这个心智模型。
入口只在账号菜单里，和「设置」一致。

注意 `adminRoutes.lint.test.ts` 会检查路径不被硬编码在组件里，链接一律用
`ADMIN_ROUTES.accounts`。

- [ ] **Step 4: 改 `TenantContext.tsx`**

`setTenantId` 对 member 变成 no-op：

```tsx
export function TenantProvider({ children }: { children: ReactNode }) {
  const { role } = useAdminAuth()
  const [tenantId, setTenantIdState] = useState(
    () => sessionStorage.getItem(TENANT_STORAGE_KEY) ?? 'demo',
  )

  const value = useMemo<TenantContextValue>(
    () => ({
      tenantId,
      setTenantId: (next: string) => {
        // member 的租户是登录时绑定的，切换这个能力对它不存在——不是把
        // 按钮藏起来，是这个函数什么也不做。真正的门在后端：member 请求
        // 别的租户会拿到 403。
        if (role !== 'admin') return
        sessionStorage.setItem(TENANT_STORAGE_KEY, next)
        setTenantIdState(next)
      },
    }),
    [tenantId, role],
  )

  return <TenantContext.Provider value={value}>{children}</TenantContext.Provider>
}
```

- [ ] **Step 5: 改 `AccountMenu.tsx`**

- 从 `useAdminAuth()` 取 `role` 与 `username`
- 租户列表区块、「新建租户」按钮与创建表单：整体包在 `{role === 'admin' && (...)}` 里
- 「设置」上方新增 `{role === 'admin' && <Link to={ADMIN_ROUTES.accounts} role="menuitem" ...>账号管理</Link>}`，图标用 `lucide-react` 的 `Users`
- 触发按钮改成两行：

```tsx
        <span className="flex min-w-0 flex-1 flex-col text-left">
          {/* 租户名在主行：它是数据作用域，弄错了不会报错，只会安静地把
              数据写到别处。身份弄错则会立刻撞上权限错误。 */}
          <span className="truncate font-bold">{current?.name ?? tenantId}</span>
          <span className="truncate text-xs text-ink-soft">{username}</span>
        </span>
```

- [ ] **Step 6: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/admin/roleBasedMenu.test.tsx`
Expected: 5 passed

- [ ] **Step 7: 破坏实现，确认否定断言会红**

1. 把租户列表的 `{role === 'admin' && ...}` 条件去掉 → member 那条「没有租户切换」应 FAIL
2. 把 `TenantContext` 里的 `if (role !== 'admin') return` 去掉 → 不会让上面的测试红（它测的是渲染），所以**额外加一条**：

```tsx
  it('member 的 setTenantId 不生效——它不只是被藏起来了', async () => {
    const { result } = renderHook(() => useAdminTenant(), {
      wrapper: ({ children }) => <TenantProvider>{children}</TenantProvider>,
    })
    act(() => result.current.setTenantId('acme'))
    expect(result.current.tenantId).toBe('demo')
    expect(sessionStorage.getItem('admin_current_tenant')).toBe('demo')
  })
```

（需 `import { act, renderHook } from '@testing-library/react'`，并在 `signIn('member')` 之后运行。）再破坏一次确认它红。

- [ ] **Step 8: 提交**

```bash
git add frontend/src/adminRoutes.ts frontend/src/admin/TenantContext.tsx frontend/src/admin/AccountMenu.tsx frontend/src/admin/roleBasedMenu.test.tsx
git commit -m "feat(frontend): 账号菜单按角色渲染，member 的租户固定

member 的 setTenantId 是 no-op，不是把按钮藏起来——藏起来的东西还能被
别的代码路径调用到。真正的门在后端：member 请求别的租户会拿到 403。

触发按钮改成两行：租户名在主行（数据作用域，弄错了不会报错只会安静写到
别处），用户名在副行。"
```

---

### Task 11: 账号管理页与设置页改密码

**Files:**
- Create: `frontend/src/admin/AccountsPage.tsx`
- Modify: `frontend/src/App.tsx`（新增路由）
- Modify: `frontend/src/admin/SettingsPage.tsx`（改密码区块）
- Test: `frontend/src/admin/accountsPage.test.tsx`（新建）

**Interfaces:**
- Consumes: Task 7 的账号 API、Task 5 的改密码 API、Task 10 的 `ADMIN_ROUTES.accounts`
- Produces: `AccountsPage` 组件

- [ ] **Step 1: 写失败的测试**

`frontend/src/admin/accountsPage.test.tsx`：

```tsx
describe('账号页', () => {
  it('admin 能看到账号列表', async () => {
    signIn('admin')
    renderAt(ADMIN_ROUTES.accounts)
    expect(await screen.findByText('alice')).toBeTruthy()
  })

  it('member 看到的是无权限提示，不是 404', async () => {
    // 404 会让人以为是链接坏了而反复重试；说清楚是权限问题，人才知道
    // 该去找谁。
    signIn('member')
    renderAt(ADMIN_ROUTES.accounts)
    expect(await screen.findByTestId('no-permission')).toBeTruthy()
    expect(screen.queryByTestId('not-found')).toBeNull()
  })

  it('新建账号要选租户', async () => {
    signIn('admin')
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.accounts)
    await user.click(await screen.findByRole('button', { name: '新建账号' }))
    expect(screen.getByLabelText('所属租户')).toBeTruthy()
  })

  it('禁用要二次确认——它会立刻把人挡在门外', async () => {
    signIn('admin')
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.accounts)
    await user.click(await screen.findByRole('button', { name: /停用 alice/ }))
    expect(await screen.findByRole('dialog')).toBeTruthy()
  })
})

describe('设置页的改密码', () => {
  it('两种角色都有', async () => {
    for (const role of ['admin', 'member'] as const) {
      sessionStorage.clear()
      signIn(role)
      const { unmount } = renderAt(ADMIN_ROUTES.settings)
      expect(await screen.findByLabelText('原密码')).toBeTruthy()
      expect(screen.getByLabelText('新密码')).toBeTruthy()
      unmount()
    }
  })

  it('两次新密码不一致时不发请求', async () => {
    signIn('member')
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.settings)
    await user.type(await screen.findByLabelText('原密码'), 'password1')
    await user.type(screen.getByLabelText('新密码'), 'password2')
    await user.type(screen.getByLabelText('确认新密码'), 'password3')
    await user.click(screen.getByRole('button', { name: '修改密码' }))
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(passwordChangeCalls).toBe(0)
  })
})
```

（`signIn`、`renderAt`、fetch stub 沿用 `roleBasedMenu.test.tsx` 的形状，stub 里补 `/api/admin/accounts` 返回 `{accounts: [...]}`，并用 `passwordChangeCalls` 计数 `PUT /api/admin/auth/password`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/admin/accountsPage.test.tsx`
Expected: FAIL

- [ ] **Step 3: 挂路由**

`ADMIN_ROUTES.accounts` 已在 Task 10 定义。这里只把页面挂上去——
`frontend/src/App.tsx` 在 `settings` 那条之后加：

```tsx
        <Route path="accounts" element={<AccountsPage />} />
```

并在文件顶部 import `AccountsPage`（按该文件既有的 import 风格，与
`SettingsPage` 同一组）。

- [ ] **Step 4: 实现 `AccountsPage.tsx`**

骨架如下，样式沿用项目既有的 `card` / `sectionTitle` 常量（见
`TermDetailPage.tsx` 顶部），取数沿用 `adminFetch` / `extractErrorDetail`：

```tsx
interface Account {
  username: string
  role: 'admin' | 'member'
  tenant_id: string | null
  status: 'active' | 'disabled'
  created_at: string
  last_login_at: string | null
}

export function AccountsPage() {
  const { sessionToken, role, username: self } = useAdminAuth()
  const { options: tenants } = useTenants()
  const confirm = useConfirm()
  const showToast = useToast()
  const [accounts, setAccounts] = useState<Account[]>([])
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 权限判断放在最前面，早于任何取数——member 连列表请求都不该发出去，
  // 那个请求只会拿回 403。
  if (role !== 'admin') {
    return (
      <div data-testid="no-permission" className="flex flex-col gap-2">
        <h1 className="font-mono text-xl font-semibold text-ink">账号</h1>
        {/* 不用 404：404 会让人以为链接坏了而反复重试。说清是权限问题，
            人才知道该去找谁。 */}
        <p className="text-sm text-ink-soft">
          这个页面只有管理员能用。需要新建或停用账号，请联系管理员。
        </p>
      </div>
    )
  }
  // …refresh()、创建表单、列表渲染…
}
```

停用按钮必须走二次确认——它立刻把人挡在门外：

```tsx
  const handleDisable = async (account: Account) => {
    if (
      !(await confirm(
        `停用「${account.username}」之后，这个账号立刻无法登录，正在进行的` +
          `操作会中断。确定吗？`,
      ))
    ) {
      return
    }
    // …POST /api/admin/accounts/{username}/disable，成功后 refresh()…
  }
```

按钮的可访问名要带上用户名（`aria-label={`停用 ${account.username}`}`）——
一列全是「停用」的按钮，屏幕阅读器用户听不出点的是哪一行。

自己那一行的停用按钮 `disabled`，`title` 写「不能停用自己」——后端也会
拒（返回 400），前端禁用只是不让人白点一次。

创建表单三个字段：用户名（`aria-label="用户名"`）、密码
（`type="password"`，`aria-label="密码"`）、所属租户（`<select>`，
`aria-label="所属租户"`，选项来自 `tenants`）。

列表用 `Skeleton variant="card-list"` 做加载态，`EmptyState` 做空态。

- [ ] **Step 5: 改 `SettingsPage.tsx`**

在既有的显示偏好卡片下方新增一个「修改密码」卡片：

```tsx
function ChangePassword() {
  const { sessionToken } = useAdminAuth()
  const showToast = useToast()
  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    // 两次不一致时不发请求：这个错误后端无从判断（它只收到一个新密码），
    // 发过去只会成功改成打错的那个，然后你就登不进来了。
    if (newPassword !== confirmPassword) {
      setError('两次输入的新密码不一致')
      return
    }
    setBusy(true)
    try {
      const response = await adminFetch('/api/admin/auth/password', sessionToken!, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '修改密码失败'))
      }
      setOldPassword('')
      setNewPassword('')
      setConfirmPassword('')
      showToast('密码已修改')
    } catch (err) {
      setError(err instanceof Error ? err.message : '修改密码失败')
    } finally {
      setBusy(false)
    }
  }
  // …三个 type="password" 输入框，label 分别是「原密码」「新密码」
  // 「确认新密码」，htmlFor 对应 id；错误用 role="alert" 显示；
  // 提交按钮文案「修改密码」，busy 时禁用…
}
```

三个 `<label>` 的文案必须精确是「原密码」「新密码」「确认新密码」——
测试按这三个名字查元素。

同时更新文件顶部的组件 docstring——它现在说「这里只放个人显示偏好」，加上密码之后不再准确：

```tsx
/**
 * 账号设置。
 *
 * 两类内容：个人显示偏好（改错了看着不顺眼，改回来即可，不影响数据），
 * 以及修改自己的密码。
 *
 * 租户切换不在这里。它是数据作用域——决定你看到的每一条数据、你的写操作
 * 落到哪个租户上。对 member 它是登录时绑定的、不可改；对 admin 它在左下角
 * 的账号菜单里常驻。
 */
```

- [ ] **Step 6: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/admin/accountsPage.test.tsx`
Expected: 全部 passed

- [ ] **Step 7: 破坏实现，确认否定断言会红**

1. 把 `AccountsPage` 里的 `role !== 'admin'` 分支删掉 → 「member 看到的是无权限提示」应 FAIL
2. 把设置页的两次密码一致性校验删掉 → 「不一致时不发请求」应 FAIL

每处确认后恢复。

- [ ] **Step 8: 前端全量**

```bash
cd frontend
npm test
npm run typecheck
npm run build
```

Expected: 全绿。

- [ ] **Step 9: 提交**

```bash
git add frontend/src/admin/AccountsPage.tsx frontend/src/adminRoutes.ts frontend/src/App.tsx frontend/src/admin/SettingsPage.tsx frontend/src/admin/accountsPage.test.tsx
git commit -m "feat(frontend): 账号管理页 + 设置页改密码

账号页不进侧边栏：它对 member 不存在，放进去会让两种角色看到不同的侧边栏，
破坏"侧边栏是固定的"这个心智模型。入口只在账号菜单里，和设置一致。

member 直达这个 URL 看到的是无权限提示而不是 404——404 会让人以为链接坏
了而反复重试，说清是权限问题，人才知道该去找谁。

两次新密码不一致时前端不发请求：这个错误后端无从判断（它只收到一个新
密码），发过去只会成功改成打错的那个。"
```

---

## 阶段验收

全部 11 个任务完成后：

```bash
pytest
cd frontend && npm test && npm run typecheck && npm run build
```

**手工验证**（自动化覆盖不到真实登录流程与浏览器行为）：

1. 清空 `admin_users` 表，设好 `CUSTOMER_RAG_ADMIN_TOKEN`，重启后端 → 日志应出现「已用 CUSTOMER_RAG_ADMIN_TOKEN 播种初始管理员 admin」
2. 用 `admin` + 那个值登录，成功
3. 新建一个租户，再给它建一个 member 账号
4. 用 member 账号登录 → 左下角显示该租户名与用户名，菜单里**没有**租户切换/新建租户/账号管理
5. 手工把地址栏改成另一个租户的 API 路径（如 `/api/admin/其他租户/nav-badges`）→ 应返回 **403**
6. 用 admin 登录 → 能切换租户，能进「账号」页
7. admin 停用那个 member → member 那边下一个请求应立刻 401 并跳回登录页
8. member 在设置页改自己的密码，用新密码重新登录
9. 故意输错密码 5 次 → 第 6 次应返回 429
10. 确认租户下拉框里**没有** `t_verify` 等 6 个测试残留租户

**若第 5 步返回 200 而不是 403**：说明有 router 没进 `tenant_scoped`。跑 `pytest tests/api/test_admin_route_shapes.py -v` 定位。

**若出现意料之外的 403**：那多半不是新缺陷，而是前端某处用了错误的 `tenant_id`——原本它会安静地返回别人的数据，现在浮出水面了。查前端传了什么，不要回滚校验。
