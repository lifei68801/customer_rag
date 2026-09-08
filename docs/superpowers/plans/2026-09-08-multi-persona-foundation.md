# 多数字人地基 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让一个账号能访问多个租户，前台右栏把这些租户呈现成可点击的数字人。

**Architecture:** 租户即数字人（spec D1），不新增隔离列。新增 `organizations` / `user_tenants` / `tenant_personas` 三张表，`tenants` 加一列 `org_id`。最危险的一步是授权：`session.tenant_id` 作为判据今天散在两处，本计划把它收敛成 `deps` 里的一个函数，两处都改成调它——两处各改各的就是下一个越权洞。

**Tech Stack:** FastAPI · aiosqlite · pydantic v2 · React 18 + TypeScript + Vite · vitest

**Spec:** `docs/superpowers/specs/2026-09-08-interaction-redesign-design.md`

## Global Constraints

1. 不许静默失败。判据是「猜错时用户能不能看见并纠正」。
2. 注释里不许写未经验证的因果。
3. 测试不许假绿：每条断言都要问「错误实现下会不会也通过」。同一批次同时含成功与失败样本。
4. 变异脚本必须先断言锚点找到了、且文件内容确实变了。
5. **绝不使用 `git add -A` 或 `git add .`**，逐个文件点名。工作区有用户自己保留的未提交改动。
6. 不推送到 origin。
7. 不修改 `.env`，不动 `data/` 下的真实数据库。
8. 租户隔离是二元的：新增的每个查询必须带 `tenant_id` 条件，新增的每个租户内路由必须走 `deps.require_tenant_access`。
9. 后端测试：`.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`。**不要用 `| tail`**——git-bash 下管道缓冲死锁，看起来像卡住；重定向到文件再读。
10. 前端测试：`cd frontend && npx vitest run src/admin` 与 `npx tsc --noEmit`。
11. 一次只跑一个 pytest 进程。
12. 改了后端路由必须重启后端。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `app/graphrag/organizations_store.py`（新） | `organizations` 表 DDL 与读写；`tenants.org_id` 加列迁移 |
| `app/auth/user_tenants_store.py`（新） | `user_tenants` 表 DDL 与读写；「这个人能访问哪些租户」的唯一事实来源 |
| `app/graphrag/tenant_personas_store.py`（新） | `tenant_personas` 表 DDL 与读写 |
| `app/api/deps.py`（改） | `list_accessible_tenant_ids()` + `require_tenant_access` 改用它 |
| `app/api/admin_auth_routes.py`（改） | 切租户路由改用同一个判据 |
| `app/api/admin_personas_routes.py`（新） | `GET /api/admin/personas`——任何角色都能拿到「我能访问的数字人」 |
| `app/api/admin_org_routes.py`（新） | 组织的增删改查 + 给账号分配租户，全部 admin 限定 |
| `app/main.py`（改） | 三张新表的 `ensure_*_schema` 注册 |
| `frontend/src/lib/personasApi.ts`（新） | 前台调 `GET /api/admin/personas` |
| `frontend/src/components/PersonaRail.tsx`（新） | 前台右栏 |
| `frontend/src/pages/ChatPage.tsx`（改） | 三栏布局 |
| `frontend/src/admin/AccountsPage.tsx`（改） | 给账号分配数字人 |

---

## Task 1: `organizations` 表与 `tenants.org_id`

**Files:**
- Create: `app/graphrag/organizations_store.py`
- Modify: `app/main.py:99-103`（`ensure_tenants_schema` 之后追加一行）
- Test: `tests/graphrag/test_organizations_store.py`

**Interfaces:**
- Consumes: `app/graphrag/tenants_store.py` 的 `tenants` 表（已存在，主键 `tenant_id`）
- Produces:
  - `async def ensure_organizations_schema(conn: aiosqlite.Connection) -> None`
  - `async def create_organization(conn, *, org_id: str, name: str) -> None`
  - `async def list_organizations(conn) -> list[dict[str, Any]]`（每项 `{org_id, name, status, created_at}`）
  - `async def assign_tenant_to_org(conn, *, tenant_id: str, org_id: str | None) -> None`
  - `async def list_tenants_in_org(conn, org_id: str) -> list[str]`
  - `class OrganizationNotFoundError(Exception)`
  - `class OrganizationAlreadyExistsError(Exception)`

- [ ] **Step 1: 写失败测试**

创建 `tests/graphrag/test_organizations_store.py`：

```python
import asyncio

import aiosqlite
import pytest

from app.graphrag.organizations_store import (
    OrganizationAlreadyExistsError,
    OrganizationNotFoundError,
    assign_tenant_to_org,
    create_organization,
    ensure_organizations_schema,
    list_organizations,
    list_tenants_in_org,
)
from app.graphrag.tenants_store import create_tenant, create_tenants_table


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_tenants_table(conn)
    await ensure_organizations_schema(conn)
    return conn


def test_create_and_list_organizations():
    async def run():
        conn = await _conn()
        try:
            await create_organization(conn, org_id="muji", name="无印良品")
            await create_organization(conn, org_id="acme", name="ACME")
            rows = await list_organizations(conn)
            assert [r["org_id"] for r in rows] == ["acme", "muji"]
            assert [r["name"] for r in rows] == ["ACME", "无印良品"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_duplicate_org_id_is_rejected_not_silently_merged():
    """同一个 org_id 建两次要报错。静默合并的话，第二次那个「新组织」
    的名字会覆盖第一个的，而建它的人以为自己建了个新的。"""

    async def run():
        conn = await _conn()
        try:
            await create_organization(conn, org_id="muji", name="无印良品")
            with pytest.raises(OrganizationAlreadyExistsError):
                await create_organization(conn, org_id="muji", name="另一家公司")
            rows = await list_organizations(conn)
            assert [r["name"] for r in rows] == ["无印良品"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_assign_tenant_to_org_and_list_back():
    async def run():
        conn = await _conn()
        try:
            await create_organization(conn, org_id="muji", name="无印良品")
            await create_tenant(conn, tenant_id="muji-goods", name="商品")
            await create_tenant(conn, tenant_id="muji-store", name="门店")
            await create_tenant(conn, tenant_id="other", name="别家")
            await assign_tenant_to_org(conn, tenant_id="muji-goods", org_id="muji")
            await assign_tenant_to_org(conn, tenant_id="muji-store", org_id="muji")
            # 第三个租户没分配——它不该出现在 muji 名下。全都返回的实现
            # 和正确实现都能让「两个都在」的断言变绿，所以这一条必须有。
            assert await list_tenants_in_org(conn, "muji") == ["muji-goods", "muji-store"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_assigning_to_a_nonexistent_org_is_rejected():
    """挂到一个不存在的组织下要报错。放行的话，这个租户会从组织视图里
    彻底消失——它有 org_id，但那个 org_id 谁也查不到。"""

    async def run():
        conn = await _conn()
        try:
            await create_tenant(conn, tenant_id="muji-goods", name="商品")
            with pytest.raises(OrganizationNotFoundError):
                await assign_tenant_to_org(conn, tenant_id="muji-goods", org_id="不存在")
        finally:
            await conn.close()

    asyncio.run(run())


def test_org_id_can_be_cleared():
    """org_id 传 None 是「把这个租户移出组织」，不是报错——存量租户本来
    就不属于任何组织，这条路径必须走得通。"""

    async def run():
        conn = await _conn()
        try:
            await create_organization(conn, org_id="muji", name="无印良品")
            await create_tenant(conn, tenant_id="muji-goods", name="商品")
            await assign_tenant_to_org(conn, tenant_id="muji-goods", org_id="muji")
            await assign_tenant_to_org(conn, tenant_id="muji-goods", org_id=None)
            assert await list_tenants_in_org(conn, "muji") == []
        finally:
            await conn.close()

    asyncio.run(run())


def test_ensure_schema_is_idempotent_and_keeps_existing_org_id():
    """启动会重复跑 ensure。第二次把 org_id 清空的话，重启一次所有租户
    就掉出组织了——而没有任何人会注意到，直到打开看板发现全空。"""

    async def run():
        conn = await _conn()
        try:
            await create_organization(conn, org_id="muji", name="无印良品")
            await create_tenant(conn, tenant_id="muji-goods", name="商品")
            await assign_tenant_to_org(conn, tenant_id="muji-goods", org_id="muji")
            await ensure_organizations_schema(conn)
            assert await list_tenants_in_org(conn, "muji") == ["muji-goods"]
        finally:
            await conn.close()

    asyncio.run(run())
```

- [ ] **Step 2: 跑测试确认它红**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_organizations_store.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.graphrag.organizations_store'`

- [ ] **Step 3: 写实现**

创建 `app/graphrag/organizations_store.py`：

```python
from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from app.db_migrations import add_column_if_missing

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


async def create_organization(conn: aiosqlite.Connection, *, org_id: str, name: str) -> None:
    cursor = await conn.execute("SELECT 1 FROM organizations WHERE org_id = ?", (org_id,))
    if await cursor.fetchone() is not None:
        raise OrganizationAlreadyExistsError(f"组织 {org_id!r} 已存在")
    await conn.execute(
        "INSERT INTO organizations (org_id, name) VALUES (?, ?)", (org_id, name)
    )
    await conn.commit()


async def list_organizations(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT org_id, name, status, created_at FROM organizations ORDER BY org_id"
    )
    return [dict(row) for row in await cursor.fetchall()]


async def assign_tenant_to_org(
    conn: aiosqlite.Connection, *, tenant_id: str, org_id: str | None
) -> None:
    """把租户挂到组织下；org_id 传 None 是移出组织。

    挂到一个不存在的组织下会被拒绝：放行的话这个租户会从组织视图里彻底
    消失——它有 org_id，但那个 org_id 谁也查不到，界面上表现为「这个租户
    不见了」而没有任何报错。
    """
    if org_id is not None:
        cursor = await conn.execute("SELECT 1 FROM organizations WHERE org_id = ?", (org_id,))
        if await cursor.fetchone() is None:
            raise OrganizationNotFoundError(f"组织 {org_id!r} 不存在")
    await conn.execute("UPDATE tenants SET org_id = ? WHERE tenant_id = ?", (org_id, tenant_id))
    await conn.commit()


async def list_tenants_in_org(conn: aiosqlite.Connection, org_id: str) -> list[str]:
    cursor = await conn.execute(
        "SELECT tenant_id FROM tenants WHERE org_id = ? ORDER BY tenant_id", (org_id,)
    )
    return [row["tenant_id"] for row in await cursor.fetchall()]
```

- [ ] **Step 4: （已核对，无需探索）**

加列的帮手函数是 `app/db_migrations.py:6` 的 `add_column_if_missing(conn, *, table, column, ddl)`——已经核对过，上面的 import 和调用直接照写即可。`terms_store.py:215-220` 是它的现成用例。

它内部用 `PRAGMA table_info` 先查再 ALTER，幂等；注释里明确要求调用方只传字面量常量（标识符不能参数化）——`"tenants"` / `"org_id"` / `"TEXT"` 三个都是字面量，符合要求。

- [ ] **Step 5: 跑测试确认它绿**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_organizations_store.py -q -p no:cacheprovider`
Expected: 6 passed

- [ ] **Step 6: 变异验证**

三条变异，每条都必须变红：

```bash
# 变异 A：重复 org_id 不再报错，改成覆盖
#   预期红：test_duplicate_org_id_is_rejected_not_silently_merged
# 变异 B：list_tenants_in_org 忽略 org_id，返回全部租户
#   预期红：test_assign_tenant_to_org_and_list_back
# 变异 C：ensure_organizations_schema 每次把 org_id 列删掉重建
#   预期红：test_ensure_schema_is_idempotent_and_keeps_existing_org_id
```

变异脚本必须先断言锚点找到了、且文件内容确实变了（Global Constraint 4）。

- [ ] **Step 7: 接进启动**

修改 `app/main.py`，在 `await ensure_tenants_schema(review_conn, ingestion_conn)` 那一行之后追加：

```python
        await ensure_organizations_schema(review_conn)
```

并在文件顶部 import 区加：

```python
from app.graphrag.organizations_store import ensure_organizations_schema
```

注意这一段在 `try/except` 里，失败只告警不阻断——保持原样，不要改成抛出。

- [ ] **Step 8: 确认应用还能起来**

Run: `.venv/Scripts/python.exe -c "import app.main; print('import ok')"`
Expected: `import ok`

- [ ] **Step 9: 提交**

```bash
git add app/graphrag/organizations_store.py tests/graphrag/test_organizations_store.py app/main.py
git commit -m "feat(tenancy): 租户之上加一层组织，隔离维度一列没动"
```

---

## Task 2: `user_tenants` 表

**Files:**
- Create: `app/auth/user_tenants_store.py`
- Modify: `app/main.py`（`ensure_admin_users_schema` 之后追加一行）
- Test: `tests/auth/test_user_tenants_store.py`

**Interfaces:**
- Consumes: Task 1 无依赖（两张表互不相干）
- Produces:
  - `async def ensure_user_tenants_schema(conn: aiosqlite.Connection) -> None`
  - `async def grant_tenant_access(conn, *, username: str, tenant_id: str) -> None`（幂等）
  - `async def revoke_tenant_access(conn, *, username: str, tenant_id: str) -> None`
  - `async def list_granted_tenant_ids(conn, username: str) -> list[str]`
  - `async def list_usernames_with_access(conn, tenant_id: str) -> list[str]`

- [ ] **Step 1: 写失败测试**

创建 `tests/auth/test_user_tenants_store.py`：

```python
import asyncio

import aiosqlite

from app.auth.user_tenants_store import (
    ensure_user_tenants_schema,
    grant_tenant_access,
    list_granted_tenant_ids,
    list_usernames_with_access,
    revoke_tenant_access,
)


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await ensure_user_tenants_schema(conn)
    return conn


def test_grant_and_list_by_user():
    async def run():
        conn = await _conn()
        try:
            await grant_tenant_access(conn, username="alice", tenant_id="muji-goods")
            await grant_tenant_access(conn, username="alice", tenant_id="muji-store")
            # bob 的授权不该出现在 alice 名下。少了这一条，「返回全表」的
            # 实现也能让上面两条变绿。
            await grant_tenant_access(conn, username="bob", tenant_id="other")
            assert await list_granted_tenant_ids(conn, "alice") == ["muji-goods", "muji-store"]
            assert await list_granted_tenant_ids(conn, "bob") == ["other"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_grant_is_idempotent():
    """重复授权不该报错、也不该在列表里出现两次。管理员点两下按钮是常态。"""

    async def run():
        conn = await _conn()
        try:
            await grant_tenant_access(conn, username="alice", tenant_id="muji-goods")
            await grant_tenant_access(conn, username="alice", tenant_id="muji-goods")
            assert await list_granted_tenant_ids(conn, "alice") == ["muji-goods"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_revoke_only_removes_that_one_pair():
    async def run():
        conn = await _conn()
        try:
            await grant_tenant_access(conn, username="alice", tenant_id="muji-goods")
            await grant_tenant_access(conn, username="alice", tenant_id="muji-store")
            await grant_tenant_access(conn, username="bob", tenant_id="muji-goods")
            await revoke_tenant_access(conn, username="alice", tenant_id="muji-goods")
            # 三个方向都要钉：alice 少了那一个、alice 另一个还在、bob 没被牵连。
            assert await list_granted_tenant_ids(conn, "alice") == ["muji-store"]
            assert await list_granted_tenant_ids(conn, "bob") == ["muji-goods"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_revoking_a_grant_that_never_existed_is_a_noop_not_an_error():
    """撤销一条不存在的授权不报错：并发下两个管理员同时点撤销，第二个人
    看到 500 会以为撤销失败、然后反复重试。"""

    async def run():
        conn = await _conn()
        try:
            await revoke_tenant_access(conn, username="alice", tenant_id="从未授权")
            assert await list_granted_tenant_ids(conn, "alice") == []
        finally:
            await conn.close()

    asyncio.run(run())


def test_list_by_tenant():
    async def run():
        conn = await _conn()
        try:
            await grant_tenant_access(conn, username="alice", tenant_id="muji-goods")
            await grant_tenant_access(conn, username="bob", tenant_id="muji-goods")
            await grant_tenant_access(conn, username="carol", tenant_id="other")
            assert await list_usernames_with_access(conn, "muji-goods") == ["alice", "bob"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_ensure_schema_is_idempotent_and_keeps_grants():
    async def run():
        conn = await _conn()
        try:
            await grant_tenant_access(conn, username="alice", tenant_id="muji-goods")
            await ensure_user_tenants_schema(conn)
            assert await list_granted_tenant_ids(conn, "alice") == ["muji-goods"]
        finally:
            await conn.close()

    asyncio.run(run())
```

- [ ] **Step 2: 跑测试确认它红**

Run: `.venv/Scripts/python.exe -m pytest tests/auth/test_user_tenants_store.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auth.user_tenants_store'`

`tests/auth/` 已存在且有 `__init__.py`（已核对），直接在里面建文件即可。

- [ ] **Step 3: 写实现**

创建 `app/auth/user_tenants_store.py`：

```python
from __future__ import annotations

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user_tenants (
    username   TEXT NOT NULL,
    tenant_id  TEXT NOT NULL,
    granted_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (username, tenant_id)
);
CREATE INDEX IF NOT EXISTS idx_user_tenants_tenant
    ON user_tenants (tenant_id, username);
"""
# 「这个人能访问哪些租户」的唯一事实来源。
#
# admin_users.tenant_id 保留不动，语义从「唯一租户」变成「默认租户」——
# 登录后 current_tenant_id 的初值仍取它。授权判据不再看那一列，只看这张表
# （见 deps.list_accessible_tenant_ids）。
#
# 复合主键就是幂等的实现：重复授权走 INSERT OR IGNORE 落到主键冲突上，
# 不需要先查后插那一圈，也就没有两个请求之间的竞态窗口。


async def ensure_user_tenants_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def grant_tenant_access(
    conn: aiosqlite.Connection, *, username: str, tenant_id: str
) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO user_tenants (username, tenant_id) VALUES (?, ?)",
        (username, tenant_id),
    )
    await conn.commit()


async def revoke_tenant_access(
    conn: aiosqlite.Connection, *, username: str, tenant_id: str
) -> None:
    """撤销一条授权。不存在时是 no-op，不报错——并发下两个管理员同时点
    撤销，第二个人看到 500 会以为撤销失败然后反复重试。"""
    await conn.execute(
        "DELETE FROM user_tenants WHERE username = ? AND tenant_id = ?", (username, tenant_id)
    )
    await conn.commit()


async def list_granted_tenant_ids(conn: aiosqlite.Connection, username: str) -> list[str]:
    cursor = await conn.execute(
        "SELECT tenant_id FROM user_tenants WHERE username = ? ORDER BY tenant_id", (username,)
    )
    return [row["tenant_id"] for row in await cursor.fetchall()]


async def list_usernames_with_access(conn: aiosqlite.Connection, tenant_id: str) -> list[str]:
    cursor = await conn.execute(
        "SELECT username FROM user_tenants WHERE tenant_id = ? ORDER BY username", (tenant_id,)
    )
    return [row["username"] for row in await cursor.fetchall()]
```

- [ ] **Step 4: 跑测试确认它绿**

Run: `.venv/Scripts/python.exe -m pytest tests/auth/test_user_tenants_store.py -q -p no:cacheprovider`
Expected: 6 passed

- [ ] **Step 5: 变异验证**

```bash
# 变异 A：list_granted_tenant_ids 去掉 WHERE username = ?
#   预期红：test_grant_and_list_by_user
# 变异 B：revoke 去掉 AND tenant_id = ?（撤销该用户全部授权）
#   预期红：test_revoke_only_removes_that_one_pair
# 变异 C：INSERT OR IGNORE 改成 INSERT
#   预期红：test_grant_is_idempotent
```

- [ ] **Step 6: 接进启动**

修改 `app/main.py`，在 `await ensure_admin_users_schema(admin_conn)` 之后追加：

```python
    await ensure_user_tenants_schema(admin_conn)
```

import 区加：

```python
from app.auth.user_tenants_store import ensure_user_tenants_schema
```

注意这一段**不在 try/except 里**（原注释说明账号体系失败必须让启动失败），保持原样。

- [ ] **Step 7: 确认应用还能起来**

Run: `.venv/Scripts/python.exe -c "import app.main; print('import ok')"`

- [ ] **Step 8: 提交**

```bash
git add app/auth/user_tenants_store.py tests/auth/test_user_tenants_store.py app/main.py
git commit -m "feat(auth): 一个账号能被授权访问多个租户"
```

---

## Task 3: 授权收敛（本计划最危险的一步）

**Files:**
- Modify: `app/api/deps.py:432-459`（`require_tenant_access`）
- Modify: `app/api/admin_auth_routes.py:175-193`（`switch_current_tenant`）
- Test: `tests/api/test_tenant_access.py`（新建；若已有同名文件则追加）

**Interfaces:**
- Consumes: Task 2 的 `list_granted_tenant_ids(conn, username) -> list[str]`
- Produces:
  - `async def list_accessible_tenant_ids(conn: aiosqlite.Connection, session: AdminSession) -> list[str] | None`
    ——返回 `None` 表示「全部租户」（admin），返回列表表示「只有这些」
  - `async def assert_tenant_accessible(conn: aiosqlite.Connection, session: AdminSession, tenant_id: str) -> None`
    ——不通过时 `raise HTTPException(403, "无权访问该租户")`

**为什么必须收敛成一个函数**：`session.tenant_id` 作为授权判据今天在两处，逻辑逐字相同。
加了 `user_tenants` 之后两处各改各的，就会出现「切得过去但读不到」或更糟的「读得到但本
不该」。本项目今年已经出过两次同类的洞（CSRF 只挂三个路由、会话消息无归属校验）。

- [ ] **Step 1: 写失败测试**

创建 `tests/api/test_tenant_access.py`：

```python
import asyncio

import aiosqlite
import pytest
from fastapi import HTTPException

from app.api.admin_session import AdminSession
from app.api.deps import assert_tenant_accessible, list_accessible_tenant_ids
from app.auth.user_tenants_store import ensure_user_tenants_schema, grant_tenant_access


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await ensure_user_tenants_schema(conn)
    return conn


def _session(username: str, role: str, tenant_id: str | None) -> AdminSession:
    return AdminSession(
        username=username, role=role, tenant_id=tenant_id, expires_at=1e18,
        current_tenant_id=tenant_id,
    )


def test_admin_can_reach_every_tenant():
    """admin 得能进入自己刚新建的租户，否则建完就管不了。None 表示
    「不设限」，不是「一个都没有」。"""

    async def run():
        conn = await _conn()
        try:
            session = _session("root", "admin", None)
            assert await list_accessible_tenant_ids(conn, session) is None
            await assert_tenant_accessible(conn, session, "从来没授权过的租户")
        finally:
            await conn.close()

    asyncio.run(run())


def test_member_reaches_exactly_what_was_granted():
    async def run():
        conn = await _conn()
        try:
            await grant_tenant_access(conn, username="alice", tenant_id="muji-goods")
            await grant_tenant_access(conn, username="alice", tenant_id="muji-store")
            await grant_tenant_access(conn, username="bob", tenant_id="secret")
            session = _session("alice", "member", "muji-goods")
            assert await list_accessible_tenant_ids(conn, session) == ["muji-goods", "muji-store"]
            await assert_tenant_accessible(conn, session, "muji-store")
        finally:
            await conn.close()

    asyncio.run(run())


def test_member_is_refused_a_tenant_someone_else_was_granted():
    """这是整个账号体系唯一真正的安全边界。bob 有 secret 的授权不等于
    alice 有——「表里存在这条 tenant_id」和「这个人有它」是两回事。"""

    async def run():
        conn = await _conn()
        try:
            await grant_tenant_access(conn, username="alice", tenant_id="muji-goods")
            await grant_tenant_access(conn, username="bob", tenant_id="secret")
            session = _session("alice", "member", "muji-goods")
            with pytest.raises(HTTPException) as excinfo:
                await assert_tenant_accessible(conn, session, "secret")
            assert excinfo.value.status_code == 403
        finally:
            await conn.close()

    asyncio.run(run())


def test_member_with_no_grants_reaches_nothing_not_everything():
    """一条授权都没有时返回空列表，不是 None。返回 None 的话它会被上面
    那条 admin 分支的语义吞掉，变成「不设限」——一个没有任何授权的账号
    因此能读写所有租户。"""

    async def run():
        conn = await _conn()
        try:
            session = _session("nobody", "member", "muji-goods")
            assert await list_accessible_tenant_ids(conn, session) == []
            with pytest.raises(HTTPException) as excinfo:
                await assert_tenant_accessible(conn, session, "muji-goods")
            assert excinfo.value.status_code == 403
        finally:
            await conn.close()

    asyncio.run(run())


def test_legacy_member_falls_back_to_their_own_tenant_id_column():
    """存量 member 在 user_tenants 里一条记录都没有（这张表刚建）。
    此时必须回退到 admin_users.tenant_id，否则这次升级会把所有现存
    member 一次性锁在门外——而他们昨天还能正常工作。

    回退只认自己那一个，不是放行全部。"""

    async def run():
        conn = await _conn()
        try:
            session = _session("legacy", "member", "muji-goods")
            assert await list_accessible_tenant_ids(conn, session) == ["muji-goods"]
            await assert_tenant_accessible(conn, session, "muji-goods")
            with pytest.raises(HTTPException):
                await assert_tenant_accessible(conn, session, "别人的租户")
        finally:
            await conn.close()

    asyncio.run(run())


def test_explicit_grants_replace_the_fallback_they_do_not_add_to_it():
    """一旦有了显式授权，就完全以显式授权为准。把 tenant_id 那一列并进来
    的话，「撤销 alice 对 muji-goods 的访问」这个操作永远生效不了——
    她的默认租户还在那一列里挂着。"""

    async def run():
        conn = await _conn()
        try:
            await grant_tenant_access(conn, username="alice", tenant_id="muji-store")
            session = _session("alice", "member", "muji-goods")
            assert await list_accessible_tenant_ids(conn, session) == ["muji-store"]
            with pytest.raises(HTTPException):
                await assert_tenant_accessible(conn, session, "muji-goods")
        finally:
            await conn.close()

    asyncio.run(run())
```

- [ ] **Step 2: 跑测试确认它红**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_tenant_access.py -q -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'assert_tenant_accessible' from 'app.api.deps'`

- [ ] **Step 3: 在 `deps.py` 里加两个函数**

在 `app/api/deps.py` 中 `require_tenant_access` 定义之前插入：

```python
async def list_accessible_tenant_ids(
    conn: aiosqlite.Connection, session: AdminSession
) -> list[str] | None:
    """这个登录者能碰哪些租户。

    返回 None 表示**不设限**（admin）；返回列表表示只有这些。用 None 而不是
    「全部租户的列表」是刻意的：admin 得能进入自己刚新建的租户，而那个租户
    在这次调用发生时可能还不存在。

    member 以 user_tenants 里的显式授权为准。一条显式授权都没有时回退到
    admin_users.tenant_id 那一列——存量 member 在这张表刚建时确实一条都没有，
    不回退的话这次升级会把所有现存 member 一次性锁在门外。

    回退是「一条都没有」时才生效，不是并进去：并进去的话，「撤销 alice 对
    她默认租户的访问」这个操作永远生效不了。
    """
    if session.role == "admin":
        return None
    granted = await list_granted_tenant_ids(conn, session.username)
    if granted:
        return granted
    return [session.tenant_id] if session.tenant_id is not None else []


async def assert_tenant_accessible(
    conn: aiosqlite.Connection, session: AdminSession, tenant_id: str
) -> None:
    """没资格就 403。整个账号体系唯一真正的安全边界，两条调用路径共用它。

    收敛成一个函数不是为了少写几行：此前 require_tenant_access 和切租户
    路由各写了一遍 `session.tenant_id != tenant_id`，加进 user_tenants 之后
    两处各改各的就会出现「切得过去但读不到」，或者更糟的反向。
    """
    accessible = await list_accessible_tenant_ids(conn, session)
    if accessible is None or tenant_id in accessible:
        return
    logger.warning(
        "越权访问被拒：username=%s 可访问 %s，试图访问 %s",
        session.username, accessible, tenant_id,
    )
    raise HTTPException(status_code=403, detail="无权访问该租户")
```

文件顶部 import 区加：

```python
from app.auth.user_tenants_store import list_granted_tenant_ids
```

- [ ] **Step 4: `require_tenant_access` 改用它**

把 `app/api/deps.py` 里 `require_tenant_access` 的函数体（从 `if session.role == "admin":` 到 `return tenant_id`）替换成：

```python
    await assert_tenant_accessible(review_conn, session, tenant_id)
    return tenant_id
```

并给它加上 `review_conn` 依赖参数（它此前不需要连接）：

```python
async def require_tenant_access(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(get_review_conn),
    session: AdminSession = Depends(require_admin_session),
) -> str:
```

docstring 保留原有内容，把「member 只能操作自己那一个」改成「member 只能操作被授权的那几个（见 list_accessible_tenant_ids）」——原句在加了 user_tenants 之后就是错的，而 Global Constraint 2 不许注释里留未经验证的陈述。

- [ ] **Step 5: 切租户路由改用它**

把 `app/api/admin_auth_routes.py:188` 那两行：

```python
    if session.role != "admin" and session.tenant_id != payload.tenant_id:
        raise HTTPException(status_code=403, detail="无权访问该租户")
```

替换成：

```python
    await deps.assert_tenant_accessible(review_conn, session, payload.tenant_id)
```

docstring 里「member 只能切回自己那个」同样要改成「member 只能切到被授权的那几个」。

- [ ] **Step 6: 跑测试确认它绿**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_tenant_access.py -q -p no:cacheprovider`
Expected: 6 passed

- [ ] **Step 7: 跑既有的越权用例，确认没放宽**

Run: `.venv/Scripts/python.exe -m pytest tests/api -q -p no:cacheprovider`
Expected: 全绿。既有用例里有多条钉「member 越权拿 403」的，它们必须继续绿——
**这一步比新用例更重要**：它证明这次改造没有放宽任何既有边界。

若有红的，逐条读失败信息判断是「用例假设了旧语义」还是「真的放宽了」。
前者改用例并在提交信息里说明改了什么；后者改实现。**不要为了让用例绿而放宽判据。**

- [ ] **Step 8: 变异验证**

```bash
# 变异 A：list_accessible_tenant_ids 的空列表回退改成 return None
#   预期红：test_member_with_no_grants_reaches_nothing_not_everything
# 变异 B：回退改成 granted + [session.tenant_id]（并进去而不是替代）
#   预期红：test_explicit_grants_replace_the_fallback_they_do_not_add_to_it
# 变异 C：assert_tenant_accessible 去掉 `tenant_id in accessible` 判断，一律放行
#   预期红：test_member_is_refused_a_tenant_someone_else_was_granted
# 变异 D：把切租户路由改回原来那两行（模拟「只改了一处」）
#   预期红：需要一条端到端用例覆盖切租户路径——若 tests/api 里没有，
#          在本任务补一条：member 对未授权租户发 PUT /api/admin/auth/session/tenant 得 403
```

变异 D 若发现没有用例覆盖，**必须补**——这正是「两处各改各的」那个洞会出现的地方。

- [ ] **Step 9: 提交**

```bash
git add app/api/deps.py app/api/admin_auth_routes.py tests/api/test_tenant_access.py
git commit -m "refactor(auth): 租户授权判据收敛成一个函数，两条路径不再各写一遍"
```

---

## Task 4: `tenant_personas` 表

**Files:**
- Create: `app/graphrag/tenant_personas_store.py`
- Modify: `app/main.py`
- Test: `tests/graphrag/test_tenant_personas_store.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `async def ensure_tenant_personas_schema(conn) -> None`
  - `async def upsert_persona(conn, *, tenant_id: str, avatar: str, tagline: str) -> None`
  - `async def get_persona(conn, tenant_id: str) -> dict[str, Any] | None`（`{tenant_id, avatar, tagline}`）
  - `async def get_personas(conn, tenant_ids: list[str]) -> dict[str, dict[str, Any]]`——一次问一批，按 `tenant_id` 索引

**注意**：引导问题（`questions`）**不在本任务**。它在阶段二的计划里，连同校验逻辑一起做。
本任务只建表并留出列，`questions` 列默认 `'[]'`。

- [ ] **Step 1: 写失败测试**

创建 `tests/graphrag/test_tenant_personas_store.py`：

```python
import asyncio

import aiosqlite

from app.graphrag.tenant_personas_store import (
    ensure_tenant_personas_schema,
    get_persona,
    get_personas,
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
```

- [ ] **Step 2: 跑测试确认它红**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_tenant_personas_store.py -q -p no:cacheprovider`

- [ ] **Step 3: 写实现**

创建 `app/graphrag/tenant_personas_store.py`：

```python
from __future__ import annotations

from typing import Any

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenant_personas (
    tenant_id  TEXT PRIMARY KEY,
    avatar     TEXT NOT NULL DEFAULT '',
    tagline    TEXT NOT NULL DEFAULT '',
    questions  TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
# 数字人的脸：前台右栏和欢迎语要用的那几样。
#
# questions 这一列本计划只建不用——引导问题连同它的校验逻辑在阶段二
# （见 docs/superpowers/plans/2026-09-08-guided-questions.md）。现在就留出
# 这一列是为了避免阶段二再做一次 ALTER TABLE，不是为了让它先空着。
#
# 一个租户一张脸，所以 tenant_id 直接做主键：数字人就是租户（spec D1），
# 「一个租户两张脸」在这个模型里没有意义。


async def ensure_tenant_personas_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def upsert_persona(
    conn: aiosqlite.Connection, *, tenant_id: str, avatar: str, tagline: str
) -> None:
    """写脸。questions 不在 DO UPDATE 的列里——阶段二会有单独的写入口，
    这里顺手把它清空的话，改一次头像就会把引导问题全删掉。"""
    await conn.execute(
        "INSERT INTO tenant_personas (tenant_id, avatar, tagline) VALUES (?, ?, ?) "
        "ON CONFLICT (tenant_id) DO UPDATE SET "
        "avatar = excluded.avatar, tagline = excluded.tagline, updated_at = datetime('now')",
        (tenant_id, avatar, tagline),
    )
    await conn.commit()


async def get_persona(conn: aiosqlite.Connection, tenant_id: str) -> dict[str, Any] | None:
    cursor = await conn.execute(
        "SELECT tenant_id, avatar, tagline FROM tenant_personas WHERE tenant_id = ?",
        (tenant_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row is not None else None


async def get_personas(
    conn: aiosqlite.Connection, tenant_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """一次问一批。右栏有 N 个数字人，逐个问就是 N 次查询。

    空列表直接返回、不发查询：拼出来的 `IN ()` 在 SQLite 上是语法错误。
    """
    if not tenant_ids:
        return {}
    placeholders = ",".join("?" for _ in tenant_ids)
    cursor = await conn.execute(
        f"SELECT tenant_id, avatar, tagline FROM tenant_personas "
        f"WHERE tenant_id IN ({placeholders})",
        tuple(tenant_ids),
    )
    return {row["tenant_id"]: dict(row) for row in await cursor.fetchall()}
```

- [ ] **Step 4: 跑测试确认它绿**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_tenant_personas_store.py -q -p no:cacheprovider`
Expected: 5 passed

- [ ] **Step 5: 变异验证**

```bash
# 变异 A：get_personas 去掉 WHERE 子句，返回全表
#   预期红：test_get_personas_batches_and_skips_the_unconfigured
# 变异 B：get_persona 找不到时返回 {"tenant_id": tenant_id, "avatar": "", "tagline": ""}
#   预期红：test_missing_persona_returns_none_not_a_blank_row
# 变异 C：upsert 的 ON CONFLICT 里加上 questions = '[]'
#   预期红：需要一条用例覆盖——本任务补：手写一条 questions 进库，
#          upsert 头像之后确认 questions 没被清空
```

变异 C 若无用例覆盖，**必须补**。直接 SQL 写入 questions，再调 `upsert_persona`，
断言 `SELECT questions` 仍是原值。

- [ ] **Step 6: 接进启动 + 提交**

`app/main.py` 在 `ensure_organizations_schema(review_conn)` 之后追加
`await ensure_tenant_personas_schema(review_conn)`，import 同上。

```bash
git add app/graphrag/tenant_personas_store.py tests/graphrag/test_tenant_personas_store.py app/main.py
git commit -m "feat(tenancy): 数字人的脸落一张表，引导问题那一列先留着"
```

---

## Task 5: `GET /api/admin/personas`

**Files:**
- Create: `app/api/admin_personas_routes.py`
- Modify: `app/main.py`（挂载路由）
- Test: `tests/api/test_admin_personas_routes.py`

**Interfaces:**
- Consumes:
  - Task 3 的 `deps.list_accessible_tenant_ids(conn, session) -> list[str] | None`
  - Task 4 的 `get_personas(conn, tenant_ids) -> dict[str, dict[str, Any]]`
  - 既有 `app.graphrag.tenants_store.list_tenants(conn, ...)`
- Produces: `GET /api/admin/personas` → `{"personas": [{tenant_id, name, avatar, tagline}], "current_tenant_id": str | None}`

**为什么需要新端点**：`GET /api/admin/tenants` 挂在
`APIRouter(prefix="/api/admin/tenants", dependencies=[Depends(deps.require_admin_role)])`
上（`app/api/admin_tenant_routes.py:22`）——**admin 专用**。member 今天拿不到任何租户列表。
右栏对所有角色都要出现，所以这是一个独立的、非 admin 限定的端点。

- [ ] **Step 1: 写失败测试**

创建 `tests/api/test_admin_personas_routes.py`。照抄 `tests/api/test_admin_terms_routes.py`
顶部的 `app.dependency_overrides` 模式（`deps.get_settings` / `deps.get_admin_session_store` /
`deps.get_review_conn` 三个 override，`_authed_headers(session_store)` 造请求头）。

```python
def test_member_sees_only_the_personas_they_were_granted(personas_conn):
    """右栏只列这个账号有权访问的。多列一个都是越权——即使他点进去会被
    403 挡住，那个名字本身就已经泄露了「这家公司还有一个叫供应链的领域」。"""
    # seed：三个租户 muji-goods / muji-store / secret，都建了 persona
    # alice 只被授权前两个
    body = _get_personas(personas_conn, username="alice", role="member")
    assert [p["tenant_id"] for p in body["personas"]] == ["muji-goods", "muji-store"]


def test_admin_sees_every_active_tenant(personas_conn):
    """admin 不设限——它得能进入自己刚新建的租户。"""
    body = _get_personas(personas_conn, username="root", role="admin")
    assert [p["tenant_id"] for p in body["personas"]] == ["muji-goods", "muji-store", "secret"]


def test_disabled_tenants_do_not_appear(personas_conn):
    """停用的租户不列：切过去之后所有写操作都是 404，而用户不知道为什么。
    这一条钉的是「授权还在但租户停用了」这个组合。"""
    # seed 后把 muji-store 停用
    body = _get_personas(personas_conn, username="alice", role="member")
    assert [p["tenant_id"] for p in body["personas"]] == ["muji-goods"]


def test_a_tenant_without_a_persona_still_appears_with_its_tenant_name(personas_conn):
    """没配过脸的租户照样出现在右栏，只是 avatar/tagline 为空。
    不出现的话，管理员新建一个租户、授权给用户，用户却看不见它——
    而没有任何地方告诉他「你需要先去配一张脸」。"""
    body = _get_personas(personas_conn, username="alice", role="member")
    unconfigured = next(p for p in body["personas"] if p["tenant_id"] == "muji-store")
    assert unconfigured["avatar"] == ""
    assert unconfigured["name"] == "门店"  # 退回租户名，不是空字符串


def test_anonymous_request_is_refused(personas_conn):
    """不带会话的请求得 401。右栏列的是「你能访问哪些知识库」，
    这个问题对匿名者没有答案。"""
    # 不带 Authorization 头
    assert _get_personas_raw(personas_conn, headers={}).status_code == 401
```

把上面五条补全成可运行的用例（照 `test_admin_terms_routes.py` 的夹具写法建
`personas_conn` fixture，seed 三个租户 + 授权 + persona）。

- [ ] **Step 2: 跑测试确认它红**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_personas_routes.py -q -p no:cacheprovider`

- [ ] **Step 3: 写实现**

创建 `app/api/admin_personas_routes.py`：

```python
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api import deps
from app.api.admin_session import AdminSession
from app.graphrag.tenant_personas_store import get_personas
from app.graphrag.tenants_store import list_tenants

router = APIRouter(
    prefix="/api/admin/personas", dependencies=[Depends(deps.require_admin_session)]
)
# 不挂 require_admin_role：右栏对 member 同样要出现。它是这个账号体系里
# 少数几个「所有角色都能调」的端点之一，而它安全的理由是返回内容本身就按
# list_accessible_tenant_ids 过滤过——member 拿到的只有他自己那几个。


class Persona(BaseModel):
    tenant_id: str
    name: str
    avatar: str
    tagline: str


class PersonaListResponse(BaseModel):
    personas: list[Persona]
    current_tenant_id: str | None


@router.get("", response_model=PersonaListResponse)
async def list_my_personas(
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    session: AdminSession = Depends(deps.require_admin_session),
) -> PersonaListResponse:
    """这个账号能访问的数字人。前台右栏和后台租户切换器共用它。

    只列启用中的租户：停用的切过去之后所有写操作都是 404，而用户不知道
    为什么——把它摆在可点击的位置上等于埋一个陷阱。

    没配过脸的租户照样出现，name 退回租户名、avatar/tagline 为空。不出现的
    话，管理员新建租户并授权之后用户看不见它，而没有任何地方告诉他还差一步。
    """
    accessible = await deps.list_accessible_tenant_ids(review_conn, session)
    active = await list_tenants(review_conn)
    if accessible is not None:
        allowed = set(accessible)
        active = [t for t in active if t["tenant_id"] in allowed]
    faces = await get_personas(review_conn, [t["tenant_id"] for t in active])
    return PersonaListResponse(
        personas=[
            Persona(
                tenant_id=t["tenant_id"],
                name=t["name"],
                avatar=faces.get(t["tenant_id"], {}).get("avatar", ""),
                tagline=faces.get(t["tenant_id"], {}).get("tagline", ""),
            )
            for t in active
        ],
        current_tenant_id=session.current_tenant_id,
    )
```

- [ ] **Step 4: （已核对，无需探索）**

`list_tenants(conn, *, include_disabled: bool = False) -> list[dict]`，每项是
`{tenant_id, name, status}`。默认只列 `status='active'` 的——正是这个端点要的，
所以上面的 `await list_tenants(review_conn)` 不用传参数。

**不要**传 `include_disabled=True`：停用的租户切过去之后所有写操作都是 404，
把它摆在可点击的位置上等于埋一个陷阱（用例 `test_disabled_tenants_do_not_appear`
钉的就是这一条）。

- [ ] **Step 5: 挂载路由**

`app/main.py` 里找到其它 `app.include_router(...)` 的位置，照同样方式加：

```python
app.include_router(admin_personas_routes.router)
```

注意 `/api/admin/*` 在 `main.py` 里有一层统一的 CSRF 挂载（`admin_scoped`）——
本路由只有 GET，不受影响，但**必须挂在同一个 scope 下**，跟其它 admin 路由一致。
照抄 `admin_tenant_routes` 的挂载写法。

- [ ] **Step 6: 跑测试确认它绿 + 跑全量 API 用例**

```
.venv/Scripts/python.exe -m pytest tests/api -q -p no:cacheprovider
```
Expected: 全绿

- [ ] **Step 7: 变异验证**

```bash
# 变异 A：去掉 accessible 过滤（member 也看到全部）
#   预期红：test_member_sees_only_the_personas_they_were_granted
# 变异 B：list_tenants 改成 include_disabled=True
#   预期红：test_disabled_tenants_do_not_appear
# 变异 C：没配 persona 的租户从结果里剔除
#   预期红：test_a_tenant_without_a_persona_still_appears_with_its_tenant_name
```

- [ ] **Step 8: 重启后端并手工验一次**

改了路由必须重启（Global Constraint 12）。重启后：

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/api/admin/personas
```
Expected: `401`（不是 404、不是 405）。**404 或 405 说明路由没挂上或后端没重启。**

- [ ] **Step 9: 提交**

```bash
git add app/api/admin_personas_routes.py tests/api/test_admin_personas_routes.py app/main.py
git commit -m "feat(api): 任何角色都能问「我能访问哪些数字人」"
```

---

## Task 6: 前台右栏

**Files:**
- Create: `frontend/src/lib/personasApi.ts`
- Create: `frontend/src/components/PersonaRail.tsx`
- Modify: `frontend/src/pages/ChatPage.tsx`
- Test: `frontend/src/components/personaRail.test.tsx`

**Interfaces:**
- Consumes: Task 5 的 `GET /api/admin/personas` → `{personas: [{tenant_id, name, avatar, tagline}], current_tenant_id}`
- Produces:
  - `export interface Persona { tenant_id: string; name: string; avatar: string; tagline: string }`
  - `export async function fetchPersonas(sessionToken: string): Promise<{ personas: Persona[]; current_tenant_id: string | null }>`
  - `export function PersonaRail(props: { personas: Persona[]; activeTenantId: string | null; onSelect: (tenantId: string) => void; loading: boolean; error: string | null }): JSX.Element`

- [ ] **Step 1: 写失败测试**

创建 `frontend/src/components/personaRail.test.tsx`。照抄
`frontend/src/admin/bulkDelete.test.tsx` 顶部的 `vi.stubGlobal('fetch', ...)` +
`renderPage()` 模式（`SkinProvider` / `ConfirmProvider` / `ToastProvider` / `MemoryRouter`）。

```tsx
describe('数字人右栏', () => {
  it('列出这个账号能访问的数字人，当前那个是选中态', async () => {
    renderChat()
    await waitFor(() => expect(screen.getByText('导购小美')).toBeTruthy())
    expect(screen.getByText('店务老张')).toBeTruthy()
    const active = screen.getByRole('button', { name: /导购小美/ })
    expect(active.getAttribute('aria-current')).toBe('true')
    // 非当前那个不能也是 aria-current，否则「哪个是当前」这件事就没说清楚
    expect(
      screen.getByRole('button', { name: /店务老张/ }).getAttribute('aria-current'),
    ).not.toBe('true')
  })

  it('点另一个数字人会发切租户请求', async () => {
    const user = userEvent.setup()
    renderChat()
    await waitFor(() => expect(screen.getByText('店务老张')).toBeTruthy())
    await user.click(screen.getByRole('button', { name: /店务老张/ }))
    await waitFor(() =>
      expect(
        switchRequests.some(
          (r) => r.method === 'PUT' && JSON.parse(String(r.body)).tenant_id === 'muji-store',
        ),
      ).toBe(true),
    )
  })

  it('切换之后会话历史跟着换成新数字人的', async () => {
    // chat_sessions 主键是 (tenant_id, session_id)，切租户后 GET /sessions
    // 必须重新拉。不重拉的话左栏还挂着上一个数字人的会话，点进去是空的。
    const user = userEvent.setup()
    renderChat()
    await waitFor(() => expect(screen.getByText('店务老张')).toBeTruthy())
    sessionsRequestCount = 0
    await user.click(screen.getByRole('button', { name: /店务老张/ }))
    await waitFor(() => expect(sessionsRequestCount).toBeGreaterThan(0))
  })

  it('只有一个数字人时右栏不出现', async () => {
    // 一个选项的选择器不是选择器，是噪音。它还会误导用户以为「还有别的，
    // 只是我没权限」——而实际上这个部署就只有一个知识库。
    personasResponse = { personas: [ONE_PERSONA], current_tenant_id: 'muji-goods' }
    renderChat()
    await waitFor(() => expect(screen.getByText('导购小美')).toBeTruthy())
    expect(screen.queryByRole('complementary', { name: '数字人' })).toBeNull()
  })

  it('拉取失败时说出来，不是静默空栏', async () => {
    // 空栏和「拉取失败」在界面上长得一样，而它们要用户做的事完全不同。
    personasStatus = 500
    renderChat()
    await waitFor(() => expect(screen.getByText(/数字人列表加载失败/)).toBeTruthy())
  })

  it('没配脸的数字人显示租户名和一个占位头像，不是空白', async () => {
    personasResponse = {
      personas: [
        { tenant_id: 'muji-goods', name: '商品', avatar: '', tagline: '' },
        { tenant_id: 'muji-store', name: '门店', avatar: '🏪', tagline: '门店的事问我' },
      ],
      current_tenant_id: 'muji-goods',
    }
    renderChat()
    await waitFor(() => expect(screen.getByText('商品')).toBeTruthy())
  })
})
```

把上面补全成可运行的用例：定义 `personasResponse` / `personasStatus` /
`switchRequests` / `sessionsRequestCount` 四个可变夹具，在 `beforeEach` 里重置，
`stubApi()` 里按 URL 分派（`/api/admin/personas`、`/api/admin/auth/session/tenant`、
`/agent/sessions`、`/auth/whoami`）。

**注意分派顺序**：`/api/admin/personas` 的判断必须排在任何 `url.includes('/api/admin')`
的兜底之前。

- [ ] **Step 2: 跑测试确认它红**

Run: `cd frontend && npx vitest run src/components/personaRail.test.tsx`

- [ ] **Step 3: 写 API 客户端**

创建 `frontend/src/lib/personasApi.ts`：

```ts
import { adminFetch, extractErrorDetail } from '../admin/adminApi'

/**
 * 数字人 = 一个租户 = 一整套本体 + 一整张图。
 *
 * 这不是新概念：前台「还没选租户」的空态里那句话原文就是「租户就是知识库」。
 * 右栏做的事是把已经存在的租户切换器从左下角账号块里的下拉框，升级成常驻的、
 * 看得见的一栏。
 *
 * 后端对应 GET /api/admin/personas——注意不是 /api/admin/tenants，
 * 那个挂在 require_admin_role 上，member 调不了。
 */
export interface Persona {
  tenant_id: string
  name: string
  /** 没配过脸时是空串。前端负责给一个占位，不是显示空白。 */
  avatar: string
  tagline: string
}

export interface PersonaListResponse {
  personas: Persona[]
  current_tenant_id: string | null
}

export async function fetchPersonas(sessionToken: string): Promise<PersonaListResponse> {
  const response = await adminFetch('/api/admin/personas', sessionToken)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '数字人列表加载失败'))
  }
  return (await response.json()) as PersonaListResponse
}
```

- [ ] **Step 4: 写右栏组件**

创建 `frontend/src/components/PersonaRail.tsx`：

```tsx
import type { Persona } from '../lib/personasApi'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface PersonaRailProps {
  personas: Persona[]
  activeTenantId: string | null
  onSelect: (tenantId: string) => void
  loading: boolean
  error: string | null
}

/**
 * 前台右栏：这个账号能访问的数字人。
 *
 * 只有一个数字人时整栏不渲染——一个选项的选择器不是选择器，是噪音，
 * 而且它会误导用户以为「还有别的，只是我没权限」。这个判断在组件内部而不是
 * 调用方，是为了让「什么时候不显示」只有一处定义。
 *
 * 拉取失败时说出来而不是渲染空栏：空栏和失败在界面上长得一样，
 * 而它们要用户做的事完全不同（一个是「你只有一个知识库」，一个是「重试」）。
 */
export function PersonaRail({
  personas,
  activeTenantId,
  onSelect,
  loading,
  error,
}: PersonaRailProps) {
  if (error !== null) {
    return (
      <aside
        aria-label="数字人"
        className="border-t border-subtle bg-card p-3 md:w-44 md:flex-shrink-0 md:border-l md:border-t-0"
      >
        <p role="status" className="text-sm text-ink-soft">
          {error}
        </p>
      </aside>
    )
  }
  if (loading) return null
  if (personas.length <= 1) return null
  return (
    <aside
      aria-label="数字人"
      className="flex flex-col gap-1 border-t border-subtle bg-card p-3 md:w-44 md:flex-shrink-0 md:border-l md:border-t-0"
    >
      <p className="mb-1 px-1 text-xs font-bold uppercase tracking-wide text-ink-soft">数字人</p>
      {personas.map((persona) => {
        const isActive = persona.tenant_id === activeTenantId
        return (
          <button
            key={persona.tenant_id}
            type="button"
            aria-current={isActive ? 'true' : undefined}
            onClick={() => onSelect(persona.tenant_id)}
            className={`flex cursor-pointer items-center gap-2 rounded-control px-2 py-2 text-left text-sm transition ${focusRing} ${
              isActive
                ? 'bg-accent-primary font-bold text-on-accent'
                : 'text-ink hover:bg-interactive-hover'
            }`}
          >
            {/* 没配过脸时给一个占位符号而不是空白：空白让用户以为这一行坏了。 */}
            <span aria-hidden="true" className="text-lg leading-none">
              {persona.avatar || '◍'}
            </span>
            <span className="min-w-0 flex-1 truncate">{persona.name}</span>
          </button>
        )
      })}
    </aside>
  )
}
```

- [ ] **Step 5: 接进 ChatPage**

修改 `frontend/src/pages/ChatPage.tsx` 的 `ChatWorkspace`：在 `<ChatSidebar ... />` 和
中间那个 `<div className="flex flex-1 flex-col">...</div>` 之后追加 `<PersonaRail ... />`。

取数与切换的状态放在 `ChatWorkspace` 里（不放进 `useAgentChat`——那个 hook 管的是
一次会话内的消息，数字人是会话之外的作用域）：

```tsx
const { tenantId, setTenantId } = useAdminTenant()
const [personas, setPersonas] = useState<Persona[]>([])
const [personasLoading, setPersonasLoading] = useState(true)
const [personasError, setPersonasError] = useState<string | null>(null)

useEffect(() => {
  let cancelled = false
  fetchPersonas('')
    .then((data) => {
      if (cancelled) return
      setPersonas(data.personas)
      setPersonasError(null)
    })
    .catch((err: unknown) => {
      if (cancelled) return
      setPersonasError(err instanceof Error ? err.message : '数字人列表加载失败')
    })
    .finally(() => {
      if (!cancelled) setPersonasLoading(false)
    })
  return () => {
    cancelled = true
  }
}, [])
```

切换直接复用 `TenantContext` 的 `setTenantId`——它内部已经会 PUT
`/api/admin/auth/session/tenant`（见 `frontend/src/admin/TenantContext.tsx:48`）。
**不要另写一份切租户请求**：两份实现会在「切了但没生效」这个 bug 上分叉。

会话历史跟着切（已定位，无需探索）：`frontend/src/hooks/useAgentChat.ts:78-90`——
`refreshSessions` 是一个 `useCallback(..., [])`，`useEffect(() => { refreshSessions() },
[refreshSessions])` 因此只在挂载时跑一次。

改法：`useAgentChat` 接受一个 `tenantId` 参数，把它加进 `refreshSessions` 的依赖数组
（`useCallback(..., [tenantId])`）。这样 tenantId 变了 → callback 身份变了 →
effect 重跑。**不要**只在 effect 的依赖里加 tenantId 而不动 callback：
callback 的依赖是空数组时它的身份永不变，effect 照样只跑一次。

同时要清掉 `sessionsData`——切了数字人之后，上一个数字人的消息不能还挂在
新数字人的会话 id 下。

- [ ] **Step 6: 跑测试 + 类型检查**

```
cd frontend && npx vitest run src/components/personaRail.test.tsx
cd frontend && npx tsc --noEmit
```
Expected: 6 passed；tsc 无输出

- [ ] **Step 7: 跑既有前台用例，确认没打破**

```
cd frontend && npx vitest run src
```
Expected: 全绿。`ChatPage` 的既有用例（`App.pages.test.tsx` 等）可能因为多了一个
fetch 调用而红——若红，是因为它们的 stub 没有 `/api/admin/personas` 分支，
给那些 stub 补上分支，**不要**给组件加「拉不到就当没有」的静默降级。

- [ ] **Step 8: 变异验证**

```bash
# 变异 A：personas.length <= 1 改成 <= 0（一个也渲染整栏）
#   预期红：test「只有一个数字人时右栏不出现」
# 变异 B：aria-current 恒为 'true'
#   预期红：test「当前那个是选中态」
# 变异 C：error 分支改成 return null（静默空栏）
#   预期红：test「拉取失败时说出来」
# 变异 D：useAgentChat 的会话 effect 去掉 tenantId 依赖
#   预期红：test「切换之后会话历史跟着换」
```

- [ ] **Step 9: 提交**

```bash
git add frontend/src/lib/personasApi.ts frontend/src/components/PersonaRail.tsx frontend/src/components/personaRail.test.tsx frontend/src/pages/ChatPage.tsx
git commit -m "feat(chat): 前台右栏列出能访问的数字人，切换带着会话历史一起走"
```

若 Step 5 改了 `useAgentChat`，把那个文件也点名加进来。

---

## Task 7: 后台的组织与授权管理

**Files:**
- Create: `app/api/admin_org_routes.py`
- Modify: `app/main.py`（挂载）
- Modify: `frontend/src/admin/AccountsPage.tsx`
- Test: `tests/api/test_admin_org_routes.py`
- Test: `frontend/src/admin/accountTenantGrants.test.tsx`

**Interfaces:**
- Consumes: Task 1 / Task 2 的全部 store 函数；`deps.require_admin_role`
- Produces:
  - `POST /api/admin/organizations` `{org_id, name}` → 201
  - `GET /api/admin/organizations` → `{organizations: [{org_id, name, status, tenant_ids}]}`
  - `PUT /api/admin/tenants/{tenant_id}/organization` `{org_id: str | null}` → 200
  - `GET /api/admin/accounts/{username}/tenants` → `{tenant_ids: [str]}`
  - `PUT /api/admin/accounts/{username}/tenants` `{tenant_ids: [str]}` → 200（全量替换）

- [ ] **Step 1: 写失败测试**

创建 `tests/api/test_admin_org_routes.py`，覆盖：

```python
def test_member_cannot_create_an_organization(org_conn):
    """组织管理是 admin 专属。member 能建组织的话，他就能把别人的租户
    挂到自己建的组织下——那是一条绕开 user_tenants 的路。"""
    assert _post_org(org_conn, role="member").status_code == 403


def test_admin_creates_an_org_and_lists_its_tenants(org_conn):
    ...


def test_putting_tenants_on_an_account_replaces_the_whole_set(org_conn):
    """全量替换而不是追加：界面上是一组复选框，用户取消勾选的那个必须
    真的被撤销。追加语义下「取消勾选」永远不生效，而界面看起来生效了。"""
    _put_account_tenants(org_conn, "alice", ["a", "b"])
    _put_account_tenants(org_conn, "alice", ["b", "c"])
    assert _get_account_tenants(org_conn, "alice") == ["b", "c"]


def test_granting_a_nonexistent_tenant_is_refused(org_conn):
    """授权一个不存在的租户要报错。放行的话，管理员以为自己给了权限，
    用户的右栏里却什么都没多——而没有任何地方说明为什么。"""
    assert _put_account_tenants_raw(org_conn, "alice", ["不存在"]).status_code == 400


def test_granting_to_a_nonexistent_account_is_refused(org_conn):
    """同理：拼错用户名不该静默成功。"""
    assert _put_account_tenants_raw(org_conn, "拼错了", ["a"]).status_code == 404


def test_admin_accounts_cannot_be_granted_tenants(org_conn):
    """admin 本来就不设限，给它「授权」是个无意义操作，而它会让管理员
    以为自己限制住了这个 admin——实际上没有。宁可报错也不要制造这个错觉。"""
    assert _put_account_tenants_raw(org_conn, "root", ["a"]).status_code == 400
```

补全成可运行用例。

- [ ] **Step 2: 跑测试确认它红**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_org_routes.py -q -p no:cacheprovider`

- [ ] **Step 3: 写路由实现**

创建 `app/api/admin_org_routes.py`，router 定义为：

```python
router = APIRouter(prefix="/api/admin", dependencies=[Depends(deps.require_admin_role)])
```

五个端点按上面 Interfaces 的形状实现。`PUT /accounts/{username}/tenants` 的实现要点：

```python
@router.put("/accounts/{username}/tenants")
async def replace_account_tenants(
    username: str,
    payload: AccountTenantsRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, list[str]]:
    """全量替换这个账号的租户授权。

    全量替换而不是追加：界面上是一组复选框，用户取消勾选的那个必须真的被
    撤销。追加语义下「取消勾选」永远不生效，而界面看起来生效了——这正是
    本项目最在意的那类静默失败。

    先校验后写：账号不存在、租户不存在、或者对象是个 admin，三种都在写入
    之前挡住。写一半再失败会留下「一部分授权生效了」的状态，而调用方拿到
    的是一个错误码，他会以为什么都没发生。
    """
    user = await get_admin_user(review_conn, username)
    if user is None:
        raise HTTPException(status_code=404, detail=f"账号 {username!r} 不存在")
    if user["role"] == "admin":
        raise HTTPException(
            status_code=400,
            detail="admin 本来就能访问全部租户，给它单独授权不会产生任何限制效果",
        )
    known = {t["tenant_id"] for t in await list_tenants(review_conn)}
    unknown = sorted(set(payload.tenant_ids) - known)
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"这些租户不存在或已停用：{'、'.join(unknown)}"
        )
    current = set(await list_granted_tenant_ids(review_conn, username))
    wanted = set(payload.tenant_ids)
    for tenant_id in sorted(wanted - current):
        await grant_tenant_access(review_conn, username=username, tenant_id=tenant_id)
    for tenant_id in sorted(current - wanted):
        await revoke_tenant_access(review_conn, username=username, tenant_id=tenant_id)
    return {"tenant_ids": sorted(wanted)}
```

- [ ] **Step 4: 挂载 + 跑测试确认它绿**

`app/main.py` 里加 `app.include_router(admin_org_routes.router)`，位置与
`admin_tenant_routes` 相邻。

Run: `.venv/Scripts/python.exe -m pytest tests/api -q -p no:cacheprovider`
Expected: 全绿

- [ ] **Step 5: 前端 —— 账号页加「可访问的数字人」**

修改 `frontend/src/admin/AccountsPage.tsx`：每个 member 行下加一组复选框，
列出全部启用中的租户，勾选状态来自 `GET /api/admin/accounts/{username}/tenants`，
保存调 `PUT`。admin 行不显示这组复选框（后端会 400，前端不该给一个必然失败的控件）。

保存成功后 toast「已更新 alice 可访问的数字人」。失败时把后端的 detail 原样显示——
「这些租户不存在或已停用：xxx」这句话里有用户需要的全部信息，包装成「保存失败」
等于把它扔掉。

- [ ] **Step 6: 写前端测试**

创建 `frontend/src/admin/accountTenantGrants.test.tsx`：

```tsx
it('取消勾选之后保存，发出去的是不含它的完整列表', async () => {
  // 追加语义的实现会发 ['b'] 或 ['a','b']；全量替换发的是 ['b']。
  // 这条用例要能区分它们——所以初始必须是两个勾选，取消一个。
})

it('admin 账号不显示这组复选框', async () => {
  // 后端对 admin 返回 400。给一个必然失败的控件，等于教用户去点一个坑。
})

it('保存失败时把后端那句话原样显示出来', async () => {
  // 「这些租户不存在或已停用：xxx」里有用户需要的全部信息。
  // 包装成「保存失败」等于把它扔掉。
})
```

补全成可运行用例。

- [ ] **Step 7: 跑前端全量 + 类型检查**

```
cd frontend && npx vitest run src/admin
cd frontend && npx tsc --noEmit
```

- [ ] **Step 8: 变异验证**

```bash
# 变异 A：PUT 改成只 grant 不 revoke（追加语义）
#   预期红：test_putting_tenants_on_an_account_replaces_the_whole_set
#           以及前端「取消勾选之后保存」那条
# 变异 B：去掉 unknown 校验
#   预期红：test_granting_a_nonexistent_tenant_is_refused
# 变异 C：router 的 require_admin_role 换成 require_admin_session
#   预期红：test_member_cannot_create_an_organization
```

- [ ] **Step 9: 重启后端 + 提交**

```bash
git add app/api/admin_org_routes.py tests/api/test_admin_org_routes.py app/main.py frontend/src/admin/AccountsPage.tsx frontend/src/admin/accountTenantGrants.test.tsx
git commit -m "feat(admin): 组织与租户授权的管理入口，全量替换而不是追加"
```

---

## Task 8: 收尾 —— 全量验证与文档

**Files:**
- Modify: `app/auth/admin_users_store.py:45-55`（只改注释）

- [ ] **Step 1: 更正 `admin_users` 的 CHECK 注释**

那条 CHECK 约束本身**不改**（`member` 仍必须有一个 `tenant_id`，它现在的语义是
「默认租户」）。但它上面那段注释说的是「member 必须属于一个租户」，加了
`user_tenants` 之后这句话会误导人。改成：

```python
    -- admin 是全局的，member 必须有一个默认租户。放在 CHECK 里而不是只靠
    -- 应用层校验：绕过应用层直接写库的路径（迁移脚本、手工修数据）同样
    -- 会被挡住。
    --
    -- 注意这一列**不再是授权判据**。2026-09-08 起「这个人能访问哪些租户」
    -- 由 user_tenants 表回答（见 deps.list_accessible_tenant_ids）；这一列
    -- 退化成「登录后默认落在哪个租户」，以及 user_tenants 里一条显式授权
    -- 都没有时的回退值（存量 member 就是这种情况）。
```

- [ ] **Step 2: 跑后端全量**

```bash
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider > "$TEMP/full.txt" 2>&1
tail -5 "$TEMP/full.txt"
```

Expected: 1893 + 新增用例数，全绿，约 80 秒。
**不要用 `| tail`**（Global Constraint 9）——重定向到文件再读。

- [ ] **Step 3: 跑前端全量**

```
cd frontend && npx vitest run src
cd frontend && npx tsc --noEmit
```

- [ ] **Step 4: 重启前后端并手工走一遍**

八项手工验证，逐项确认：

1. admin 登录 → 账号页能建组织
2. admin 把两个租户挂到同一个组织下
3. admin 给 member `alice` 勾选两个数字人，保存
4. 用 `alice` 登录前台 → 右栏出现两个数字人
5. 点第二个 → 中间的欢迎语换了，左栏会话历史也换了
6. 用只有一个租户的 member 登录 → 右栏**不出现**
7. 用 `alice` 直接改 URL 访问未授权的第三个租户 → 403
8. 用存量 member（`user_tenants` 里没有记录的）登录 → 仍能访问自己那个租户

第 7 和第 8 项是这个计划的安全核心，**不能跳过**。

- [ ] **Step 5: 提交**

```bash
git add app/auth/admin_users_store.py
git commit -m "docs(auth): 更正 admin_users.tenant_id 的注释——它不再是授权判据"
```

---

## Self-Review

**1. Spec coverage**

| Spec 要求 | 落在哪 |
|---|---|
| D1 数字人 = 租户 + 组织层 | Task 1 |
| `user_tenants` 多对多 | Task 2 |
| 授权面收敛（spec §3 「本设计最危险的地方」） | Task 3 |
| `tenant_personas` 表 | Task 4 |
| 前台右栏 | Task 5 + Task 6 |
| 「看板只显示有权访问的领域」的数据来源 | Task 3 的 `list_accessible_tenant_ids`（阶段三消费） |
| 「一个会话属于一个数字人」 | Task 6 Step 5 的 `tenantId` 依赖 |
| 「组织管理页只有 admin 能进」 | Task 7 的 `require_admin_role` |
| 引导问题 | **不在本计划**，见阶段二 |
| `qa_diagnostics.outcome` / `db_sources` / `attribute_conflicts` | 不在本计划，见阶段四 / 五 / 六 |

**2. Placeholder scan**：Task 5 Step 1、Task 7 Step 1/Step 6 的用例给的是骨架 + 每条的
理由，需要执行者补全成可运行代码。这是有意为之——那几条用例依赖仓库里既有的夹具写法
（`tests/api/test_admin_terms_routes.py` 的 `dependency_overrides` 模式、
`frontend/src/admin/bulkDelete.test.tsx` 的 `stubApi` 模式），照抄比重写更可靠，
计划里点名了要照抄哪个文件。每条用例的**断言意图和它防的是什么**都写全了，没有 TBD。

**3. Type consistency**

- `list_accessible_tenant_ids(conn, session) -> list[str] | None`：Task 3 定义，
  Task 5 消费，签名一致。
- `get_personas(conn, tenant_ids: list[str]) -> dict[str, dict[str, Any]]`：Task 4 定义，
  Task 5 消费，一致。
- `Persona` 的四个字段 `tenant_id / name / avatar / tagline`：Task 5 后端与 Task 6
  前端一致。
- `list_granted_tenant_ids` / `grant_tenant_access` / `revoke_tenant_access`：
  Task 2 定义，Task 3 与 Task 7 消费，一致。

---

## 已知的执行前不确定项

这几处计划里给了处理办法，执行者遇到时按办法走，不要自行发挥：

只剩一条，且它不是「去查一下」而是「需要判断」：

1. **既有越权用例可能因语义变更而红**（Task 3 Step 7）：逐条判断是「用例假设了旧语义」
   还是「真的放宽了」。**不要为了让用例绿而放宽判据。**

其余原本列在这里的四项（`ensure_column` 的位置、`list_tenants` 的签名、
`tests/auth/` 是否存在、`useAgentChat` 的 effect 在哪）已在写计划时核对完毕，
结论直接写进了对应的 Step，执行者不需要再探索。
