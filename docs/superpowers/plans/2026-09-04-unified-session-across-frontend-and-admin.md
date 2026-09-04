# 前台与管理后台共用一套会话 —— 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把前台问答从「匿名、租户硬编码」改成「登录后使用、身份与租户来自服务端会话」，并让前台与管理后台共用同一套 HttpOnly Cookie 会话。

**Architecture:** 七个任务分两阶段。阶段一（Task 1-4）在服务端立起认证边界：Cookie 载体、CSRF、当前租户进会话、五个前台接口装门并把身份来源从「客户端自报」改成「从会话取」。阶段二（Task 5-7）改前端：`adminFetch` 换成 Cookie + CSRF、会话状态改由 `whoami` 驱动、前台加登录门与账号块。**写成一份计划而不是两份**：服务端加门之后前台会 401，阶段一单独交付会让应用不可用，两阶段只是提交与回滚的粒度。

**Tech Stack:** Python 3.12 / FastAPI / pytest（后端）；React 18 + TypeScript + Vite + vitest + @testing-library/react（前端）

**Spec:** [docs/superpowers/specs/2026-09-04-unified-session-across-frontend-and-admin-design.md](../specs/2026-09-04-unified-session-across-frontend-and-admin-design.md)

## Global Constraints

- **坐席复用现有 `admin`/`member` 账号体系**，`member` 即坐席。不新增「坐席」角色。
- **既有匿名会话历史直接废弃，不迁移。**
- **不改 `AdminSessionStore` 的进程内字典实现，不引入 JWT。** Cookie 里装的仍是那个不透明 token，服务端仍按字典查、仍能即时吊销。
- **不动 `X-Tenant-Id` 网关认证路径。** 会话认证优先，没有会话时才回落到网关头。
- **不把账号管理 / 租户管理入口放进前台。**
- **会话失效的界面契约**：前端拿到 401 必须主动清本地状态并跳登录页，**不许停在「显示已登录但什么都点不动」的界面**，也不许反复重试。
- 后端全量：`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`，当前基线 **1708 passed**。
- 前端全量：`cd frontend && npx vitest run`，当前基线 **38 文件 355 tests**；另需 `npx tsc --noEmit` 和 `npx vite build` 各自通过。
- **每条负向断言都要故意改坏实现确认它变红。** **跑变异要跑整个测试文件，不要用 `-k` / `-t` 名字过滤器**——过滤器没选中你指望变红的那条时，输出和「变异通过」一模一样。
- **本仓库源码行尾 CRLF 与 LF 混用**（`app/agent/graph.py` 是 LF，`app/agent/planner.py` 是 CRLF）。脚本改写前**先读出文件确认真实行尾**，锚点按真实行尾拼。
- **注释里不许写未经验证的因果**。写之前先让它失败一次。

---

## 已核实的现状（写计划时逐条读过，实施者可直接依赖）

| 事实 | 位置 |
|---|---|
| `AdminSession` 是 `@dataclass(frozen=True)`，字段 `username` / `role` / `tenant_id` / `expires_at` | `app/api/admin_session.py:8-19` |
| `AdminSessionStore` 进程内字典 `_sessions: dict[str, AdminSession]`，方法 `create_session(*, username, role, tenant_id, ttl_seconds=28800) -> str`、`get_session(token) -> AdminSession \| None`、`revoke_session(token) -> None` | `app/api/admin_session.py:21-75` |
| `require_admin_session(authorization: str \| None = Header(...), session_store, review_conn) -> AdminSession`，只认 `Authorization: Bearer <token>` | `app/api/deps.py:338-366` |
| `require_tenant_access(tenant_id, session) -> str`，`admin`（`tenant_id is None`）放行任意租户；`member` 不符即 403 | `app/api/deps.py:369-395` |
| `LoginResponse` 字段：`session_token` / `username` / `role` / `tenant_id` | `app/api/admin_auth_routes.py:34-38` |
| 五个待装门的接口：`POST /agent/chat`、`POST /qa`、`GET /agent/sessions`、`GET /agent/sessions/{id}/messages`、`DELETE /agent/sessions/{id}`；三个 router 均为裸 `APIRouter()` | `app/api/agent_routes.py:30,47`、`app/api/qa_routes.py:18`、`app/api/session_routes.py:11` |
| 前台租户硬编码 | `frontend/src/hooks/useAgentChat.ts:12` `const TENANT_ID = 'demo'` |
| 前台身份来自 localStorage 随机 UUID | `frontend/src/lib/identity.ts:9` `getAnonymousUserId()` |
| 后台 token 存 sessionStorage | `frontend/src/admin/useAdminAuth.ts:4` `SESSION_STORAGE_KEY` |
| 当前租户存 sessionStorage | `frontend/src/admin/TenantContext.tsx:4` `TENANT_STORAGE_KEY` |

**`frozen=True` 的后果**：`current_tenant_id` 不能就地改，必须用 `dataclasses.replace()` 生成新实例替换字典里那一条。Task 2 按这个来。

---

## File Structure

**新建：**

| 文件 | 职责 |
|---|---|
| `app/api/session_cookie.py` | Cookie 与 CSRF 的名字、属性、读写辅助。集中一处，避免名字散落在登录/登出/依赖三处各写一遍 |
| `tests/api/test_session_cookie.py` | 上面这些辅助的单元测试 |
| `frontend/src/lib/csrf.ts` | 读取非 HttpOnly 的 CSRF Cookie，供 `adminFetch` 与前台 API 共用 |
| `frontend/src/authGate.test.tsx` | 前台登录门的测试 |

**修改：**

| 文件 | 改什么 |
|---|---|
| `app/api/admin_session.py` | `AdminSession` 增加 `current_tenant_id: str \| None`；store 增加 `set_current_tenant(token, tenant_id)` |
| `app/api/deps.py` | `require_admin_session` 改为「先读 Cookie，回落 Bearer」；新增 `require_csrf` 依赖；新增 `require_chat_session`（前台用，租户取自会话） |
| `app/api/admin_auth_routes.py` | 登录下发 Cookie + CSRF；登出清 Cookie；新增 `PUT /session/tenant` |
| `app/api/agent_routes.py` | `/agent/chat` 装门，租户从会话取 |
| `app/api/qa_routes.py` | `/qa` 装门 |
| `app/api/session_routes.py` | 三个接口装门，`user_id` / `tenant_id` 从会话取，移除查询参数 |
| `frontend/src/admin/adminApi.ts` | `adminFetch` 去掉 `Authorization`，加 `credentials: 'include'` 与 `X-CSRF-Token` |
| `frontend/src/admin/useAdminAuth.ts` | 不再存 token；会话状态由 `whoami` 驱动；401 时清状态跳登录页 |
| `frontend/src/admin/TenantContext.tsx` | 当前租户改走服务端，不再存 sessionStorage |
| `frontend/src/hooks/useAgentChat.ts` | 删掉 `TENANT_ID` 常量与匿名身份，请求不再带 `tenant_id` / `user_id` |
| `frontend/src/lib/sessionsApi.ts` | 三个函数去掉 `tenantId` / `userId` 参数 |
| `frontend/src/pages/ChatPage.tsx` | 未登录渲染登录表单；加账号块 |
| `frontend/src/App.tsx` | 前台路由包一层登录门 |

**删除：** `frontend/src/lib/identity.ts`（`getAnonymousUserId`）

---

## 阶段一：服务端认证边界（Task 1-4）

### Task 1: Cookie 与 CSRF 的载体

**Files:**
- Create: `app/api/session_cookie.py`
- Create: `tests/api/test_session_cookie.py`

**Interfaces:**
- Produces:
  - `SESSION_COOKIE_NAME = "customer_rag_session"`、`CSRF_COOKIE_NAME = "customer_rag_csrf"`、`CSRF_HEADER_NAME = "X-CSRF-Token"`
  - `def set_session_cookies(response: Response, *, session_token: str, csrf_token: str, secure: bool, max_age: int) -> None`
  - `def clear_session_cookies(response: Response) -> None`
  - `def new_csrf_token() -> str`
  - `def is_secure_request(request: Request) -> bool`

- [ ] **Step 1: 写失败测试**

新建 `tests/api/test_session_cookie.py`：

```python
from fastapi import Request, Response

from app.api.session_cookie import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    clear_session_cookies,
    is_secure_request,
    new_csrf_token,
    set_session_cookies,
)


def _scope(scheme: str, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "scheme": scheme,
            "headers": headers or [],
            "method": "GET",
            "path": "/",
            "query_string": b"",
        }
    )


def test_session_cookie_is_http_only_and_csrf_cookie_is_not():
    """会话 token 必须 JS 读不到；CSRF token 必须 JS 读得到。

    双提交令牌的整个机制就建立在这个不对称上：攻击者的站点能让浏览器带上
    会话 Cookie，但读不到 CSRF 值、也就填不出那个请求头。两个都设成
    HttpOnly 的话前端拿不到 CSRF 值，机制直接失效；两个都不设的话 XSS
    能把会话 token 偷走。
    """
    response = Response()
    set_session_cookies(
        response, session_token="tok", csrf_token="csrf", secure=False, max_age=28800
    )
    raw = [v.decode() for k, v in response.raw_headers if k == b"set-cookie"]
    session_line = next(line for line in raw if line.startswith(SESSION_COOKIE_NAME))
    csrf_line = next(line for line in raw if line.startswith(CSRF_COOKIE_NAME))
    assert "HttpOnly" in session_line
    assert "HttpOnly" not in csrf_line


def test_session_cookie_declares_samesite_lax():
    response = Response()
    set_session_cookies(
        response, session_token="tok", csrf_token="csrf", secure=False, max_age=28800
    )
    raw = [v.decode() for k, v in response.raw_headers if k == b"set-cookie"]
    session_line = next(line for line in raw if line.startswith(SESSION_COOKIE_NAME))
    assert "SameSite=lax" in session_line.lower()


def test_secure_flag_follows_the_request_scheme():
    """本地 http 开发时带上 Secure 会让 Cookie 直接不生效，所以这个标志
    按请求实际用的协议决定，不写死。"""
    assert is_secure_request(_scope("https")) is True
    assert is_secure_request(_scope("http")) is False


def test_clear_session_cookies_expires_both():
    response = Response()
    clear_session_cookies(response)
    raw = [v.decode() for k, v in response.raw_headers if k == b"set-cookie"]
    assert any(line.startswith(SESSION_COOKIE_NAME) for line in raw)
    assert any(line.startswith(CSRF_COOKIE_NAME) for line in raw)
    assert all('Max-Age=0' in line or 'max-age=0' in line.lower() for line in raw)


def test_new_csrf_token_is_unguessable_and_unique():
    a, b = new_csrf_token(), new_csrf_token()
    assert a != b
    assert len(a) >= 32
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/api/test_session_cookie.py -q`

Expected: FAIL，`ModuleNotFoundError: No module named 'app.api.session_cookie'`

- [ ] **Step 3: 实现**

新建 `app/api/session_cookie.py`：

```python
"""会话 Cookie 与 CSRF 令牌的名字、属性与读写。

集中在一个模块而不是散在登录/登出/依赖三处：Cookie 的名字和属性只要有
一处对不上（比如登出时 Path 写得和登录时不一样），浏览器就不会删掉它，
表现是"点了登出但还是登录着"——而三处各写一遍时这种不一致很难看出来。
"""

from __future__ import annotations

import secrets

from fastapi import Request, Response

SESSION_COOKIE_NAME = "customer_rag_session"
CSRF_COOKIE_NAME = "customer_rag_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"

_COOKIE_PATH = "/"


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def is_secure_request(request: Request) -> bool:
    """这个请求是不是经 HTTPS 到达的。

    Secure 标志不能写死：本地 http://localhost 开发时带上它，浏览器会
    直接忽略这个 Cookie，登录后表现为"登录成功但立刻又是未登录"。
    """
    return request.url.scheme == "https"


def set_session_cookies(
    response: Response,
    *,
    session_token: str,
    csrf_token: str,
    secure: bool,
    max_age: int,
) -> None:
    """下发会话 Cookie（HttpOnly）与 CSRF Cookie（非 HttpOnly）。

    两者的 HttpOnly 不对称是双提交令牌机制的基础：攻击者站点能让浏览器
    带上会话 Cookie，但读不到 CSRF 值、填不出那个请求头。
    """
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
        path=_COOKIE_PATH,
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        max_age=max_age,
        httponly=False,
        samesite="lax",
        secure=secure,
        path=_COOKIE_PATH,
    )


def clear_session_cookies(response: Response) -> None:
    """删掉两个 Cookie。

    用 max_age=0 而不是 delete_cookie：属性（path/samesite）必须和下发时
    完全一致，浏览器才认这是同一个 Cookie，否则删不掉。
    """
    for name in (SESSION_COOKIE_NAME, CSRF_COOKIE_NAME):
        response.set_cookie(
            name, "", max_age=0, samesite="lax", path=_COOKIE_PATH
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/api/test_session_cookie.py -q`

Expected: 5 passed

- [ ] **Step 5: 两次变异验证**

跑整份 `tests/api/test_session_cookie.py`（不加 `-k`/`-t`），确认变红后还原：

1. 把 CSRF Cookie 的 `httponly=False` 改成 `True` → `test_session_cookie_is_http_only_and_csrf_cookie_is_not` 应 FAIL
2. 把 `is_secure_request` 改成恒 `return True` → `test_secure_flag_follows_the_request_scheme` 应 FAIL

- [ ] **Step 6: 提交**

```bash
git add app/api/session_cookie.py tests/api/test_session_cookie.py
git commit -m "feat(api): 会话 Cookie 与 CSRF 令牌的载体

Cookie 的名字和属性集中一处：只要有一处对不上（比如登出时 Path 和登录时
不一样），浏览器就不删它，表现是点了登出还是登录着。

会话 Cookie 设 HttpOnly、CSRF Cookie 不设——双提交令牌就建立在这个不对称
上：攻击者站点能让浏览器带上会话 Cookie，但读不到 CSRF 值、填不出请求头。

Secure 按请求协议动态决定，不写死：本地 http 开发时带上它浏览器会直接忽略
这个 Cookie，表现为登录成功后立刻又是未登录。"
```

---

### Task 2: 当前租户进会话

**Files:**
- Modify: `app/api/admin_session.py`（`AdminSession` 在 `:8-19`，`AdminSessionStore` 在 `:21`）
- Test: `tests/api/test_admin_session.py`（不存在则新建）

**Interfaces:**
- Produces:
  - `AdminSession` 增加字段 `current_tenant_id: str | None = None`
  - `AdminSessionStore.set_current_tenant(token: str, tenant_id: str) -> AdminSession | None`——token 无效返回 `None`

- [ ] **Step 1: 写失败测试**

追加到 `tests/api/test_admin_session.py`（新建时先加 `from app.api.admin_session import AdminSessionStore`）：

```python
def test_set_current_tenant_replaces_the_frozen_session_in_place():
    """AdminSession 是 frozen dataclass，改不了字段——必须整条替换。

    直接赋值会抛 FrozenInstanceError；而如果实现是"新建一条但没写回字典"，
    下一次 get_session 拿到的还是旧值，界面上表现为"切了租户但没切"。
    """
    store = AdminSessionStore()
    token = store.create_session(username="admin", role="admin", tenant_id=None)

    updated = store.set_current_tenant(token, "acme")

    assert updated is not None
    assert updated.current_tenant_id == "acme"
    assert store.get_session(token).current_tenant_id == "acme"


def test_set_current_tenant_returns_none_for_unknown_token():
    store = AdminSessionStore()
    assert store.set_current_tenant("no-such-token", "acme") is None


def test_new_session_starts_with_member_tenant_as_current():
    """member 绑定一个租户，登录后当前租户就该是它，不该是 None——
    否则前台第一次问答时没有租户可用。"""
    store = AdminSessionStore()
    token = store.create_session(username="alice", role="member", tenant_id="demo")
    assert store.get_session(token).current_tenant_id == "demo"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/api/test_admin_session.py -q`

Expected: FAIL，`AttributeError: 'AdminSessionStore' object has no attribute 'set_current_tenant'`

- [ ] **Step 3: 实现**

`app/api/admin_session.py`：给 `AdminSession` 加字段（放在最后，带默认值，避免打断既有位置参数调用）：

```python
    username: str
    role: str  # "admin" | "member"
    tenant_id: str | None
    expires_at: float
    # 当前正在操作的租户。前台问答与后台页面共用这一个值——前端不再自己
    # 在 sessionStorage 里记，否则它会按标签页隔离、和 Cookie 会话不同步。
    #
    # 与 tenant_id 的区别：tenant_id 是"你属于哪个租户"（member 固定、
    # admin 为 None），current_tenant_id 是"你现在在看哪个租户"。member
    # 两者恒等；admin 的 tenant_id 永远是 None，current_tenant_id 才是
    # 他切到的那个。
    current_tenant_id: str | None = None
```

`create_session` 里初始化——`member` 直接用它绑定的租户：

```python
        self._sessions[token] = AdminSession(
            username=username,
            role=role,
            tenant_id=tenant_id,
            expires_at=time.time() + ttl_seconds,
            current_tenant_id=tenant_id,
        )
```

新增方法（`dataclasses.replace` 需要在文件顶部 `from dataclasses import dataclass, replace`）：

```python
    def set_current_tenant(self, token: str, tenant_id: str) -> AdminSession | None:
        """切换这个会话的当前租户。

        AdminSession 是 frozen 的，改不了字段——用 replace 生成新实例并
        写回字典。只 replace 不写回的话，下次 get_session 拿到的还是旧值，
        界面上表现为"切了租户但没切"。

        不做权限判断：谁能切到哪个租户由路由层的 require_tenant_access
        决定，这里只负责存。
        """
        session = self.get_session(token)
        if session is None:
            return None
        updated = replace(session, current_tenant_id=tenant_id)
        self._sessions[token] = updated
        return updated
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/api/test_admin_session.py -q`

Expected: 3 passed

- [ ] **Step 5: 两次变异验证**

跑整份文件（不加 `-k`/`-t`）：

1. 把 `set_current_tenant` 里的 `self._sessions[token] = updated` 删掉（只 replace 不写回）→ `test_set_current_tenant_replaces_the_frozen_session_in_place` 应 FAIL
2. 把 `create_session` 里的 `current_tenant_id=tenant_id` 改成 `current_tenant_id=None` → `test_new_session_starts_with_member_tenant_as_current` 应 FAIL

- [ ] **Step 6: 全量与提交**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`

```bash
git add app/api/admin_session.py tests/api/test_admin_session.py
git commit -m "feat(api): 当前租户进会话

前端此前把当前租户存在 sessionStorage 里，它按标签页隔离，跟即将改成
Cookie 的会话不同步——同一个人在两个标签页里会看到两个不同的当前租户。

AdminSession 是 frozen dataclass，改不了字段，用 dataclasses.replace 整条
替换并写回字典。只 replace 不写回的话下次读到的还是旧值，界面上表现为
切了租户但没切。

member 登录后当前租户直接是它绑定的那个，不是 None——否则前台第一次问答
没有租户可用。"
```

---

### Task 3: 鉴权改读 Cookie，并加 CSRF 依赖

**Files:**
- Modify: `app/api/deps.py`（`require_admin_session` 在 `:338-366`）
- Modify: `app/api/admin_auth_routes.py`（`login` 在 `:53`，`logout` 在 `:124`）
- Test: `tests/api/test_admin_auth_routes.py`

**Interfaces:**
- Consumes: Task 1 的 `SESSION_COOKIE_NAME` / `CSRF_COOKIE_NAME` / `CSRF_HEADER_NAME` / `set_session_cookies` / `clear_session_cookies` / `new_csrf_token` / `is_secure_request`；Task 2 的 `set_current_tenant`
- Produces:
  - `require_admin_session` 改为「先读 Cookie，没有再回落 `Authorization: Bearer`」
  - `async def require_csrf(request: Request) -> None`——写方法缺/错 `X-CSRF-Token` 时 403
  - `PUT /api/admin/session/tenant`，body `{"tenant_id": "..."}`

- [ ] **Step 1: 写失败测试**

追加到 `tests/api/test_admin_auth_routes.py`（照该文件既有用例的 `client` fixture 与调用形状）：

```python
def test_login_sets_session_and_csrf_cookies(client):
    response = client.post(
        "/api/admin/login", json={"username": "admin", "password": "test-token"}
    )
    assert response.status_code == 200
    assert "customer_rag_session" in response.cookies
    assert "customer_rag_csrf" in response.cookies


def test_cookie_session_is_accepted_without_authorization_header(client):
    """装上 Cookie 之后，同源请求不再需要手工带 Bearer 头——这正是
    前台走到后台不用二次登录的机制。"""
    client.post("/api/admin/login", json={"username": "admin", "password": "test-token"})
    response = client.get("/api/admin/whoami")
    assert response.status_code == 200
    assert response.json()["username"] == "admin"


def test_logout_revokes_the_token_server_side_not_only_the_cookie(client):
    """只清 Cookie 不够：token 还活在服务端字典里，8 小时内谁拿到它仍然
    有效。登出必须两件事都做。"""
    login = client.post(
        "/api/admin/login", json={"username": "admin", "password": "test-token"}
    )
    token = login.json()["session_token"]
    client.post("/api/admin/logout", headers={"Authorization": f"Bearer {token}"})

    # 用原来那个 token 直接敲（绕开 Cookie），服务端应当已经不认了
    response = client.get(
        "/api/admin/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


def test_write_request_without_csrf_header_is_rejected(client):
    client.post("/api/admin/login", json={"username": "admin", "password": "test-token"})
    response = client.put(
        "/api/admin/session/tenant", json={"tenant_id": "demo"}
    )
    assert response.status_code == 403
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/api/test_admin_auth_routes.py -q`

Expected: FAIL——Cookie 未下发、`/session/tenant` 404

- [ ] **Step 3: 实现**

`app/api/deps.py` 的 `require_admin_session`：把取 token 那一段换成先 Cookie 后 Bearer，其余（查库确认 active、撤销）**完全不动**：

```python
async def require_admin_session(
    request: Request,
    authorization: str | None = Header(default=None),
    session_store: AdminSessionStore = Depends(get_admin_session_store),
    review_conn: aiosqlite.Connection = Depends(get_review_conn),
) -> AdminSession:
```

函数体开头改为：

```python
    # 先 Cookie 后 Bearer：浏览器走 Cookie（前台与后台同源共享，这正是
    # 不用二次登录的机制）；Bearer 留给脚本与既有测试，两者并存。
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="缺少管理员登录凭证")
        token = authorization.removeprefix("Bearer ")
```

新增 CSRF 依赖：

```python
async def require_csrf(request: Request) -> None:
    """双提交令牌校验，只作用于写方法。

    SameSite=Lax 已经挡掉绝大部分跨站写请求，这是第二道：Lax 对老浏览器
    不完全可靠。成本只有前端一个请求头加这里一次比对。

    只在有会话 Cookie 时校验——纯 Bearer 调用方（脚本、既有测试）不经过
    浏览器，不存在 CSRF 场景，要求它们带这个头只会平白打断。
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if SESSION_COOKIE_NAME not in request.cookies:
        return
    header_value = request.headers.get(CSRF_HEADER_NAME)
    cookie_value = request.cookies.get(CSRF_COOKIE_NAME)
    if not header_value or not cookie_value or header_value != cookie_value:
        raise HTTPException(status_code=403, detail="CSRF 校验失败")
```

`app/api/admin_auth_routes.py` 的 `login` 增加 `request: Request` 与 `response: Response` 参数，创建会话后下发 Cookie：

```python
    csrf_token = new_csrf_token()
    set_session_cookies(
        response,
        session_token=session_token,
        csrf_token=csrf_token,
        secure=is_secure_request(request),
        max_age=28800,
    )
```

`logout` 增加 `response: Response`，在既有的撤销之后加 `clear_session_cookies(response)`。

新增租户切换路由：

```python
class SwitchTenantRequest(BaseModel):
    tenant_id: str


@router.put(
    "/session/tenant",
    dependencies=[Depends(deps.require_csrf)],
)
async def switch_current_tenant(
    request: Request,
    payload: SwitchTenantRequest,
    session: AdminSession = Depends(deps.require_admin_session),
    session_store: AdminSessionStore = Depends(deps.get_admin_session_store),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, str]:
    """切换当前租户。

    权限判据复用 require_tenant_access 那一套：admin 可切任意（但仍要确认
    租户启用着），member 只能切回自己那个。
    """
    await require_active_tenant_or_404(review_conn, payload.tenant_id)
    if session.role != "admin" and session.tenant_id != payload.tenant_id:
        raise HTTPException(status_code=403, detail="无权访问该租户")
    token = request.cookies.get(SESSION_COOKIE_NAME) or ""
    if not session_store.set_current_tenant(token, payload.tenant_id):
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return {"tenant_id": payload.tenant_id}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/api/test_admin_auth_routes.py -q`

Expected: 全部 passed

- [ ] **Step 5: 三次变异验证**

跑整份文件（不加 `-k`/`-t`）：

1. `login` 里不调 `set_session_cookies` → `test_login_sets_session_and_csrf_cookies` 应 FAIL
2. `logout` 里去掉服务端 `revoke_session`（只清 Cookie）→ `test_logout_revokes_the_token_server_side_not_only_the_cookie` 应 FAIL
3. `require_csrf` 改成直接 `return` → `test_write_request_without_csrf_header_is_rejected` 应 FAIL

- [ ] **Step 6: 全量与提交**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`

```bash
git add app/api/deps.py app/api/admin_auth_routes.py tests/api/test_admin_auth_routes.py
git commit -m "feat(api): 会话改走 HttpOnly Cookie，加 CSRF 双提交校验

先 Cookie 后 Bearer：浏览器走 Cookie（前后台同源共享，这就是不用二次登录
的机制），Bearer 留给脚本与既有测试，两者并存。查库确认账号 active 那一段
完全不动——禁用账号立即生效这条保证不受影响。

CSRF 只在有会话 Cookie 时校验：纯 Bearer 调用方不经过浏览器，不存在 CSRF
场景，要求它们带这个头只会平白打断。

登出必须两件事都做——只清 Cookie 的话 token 还活在服务端字典里，8 小时内
谁拿到它仍然有效。"
```

---

### Task 4: 五个前台接口装门

**Files:**
- Modify: `app/api/agent_routes.py`（`AgentChatRequest` 在 `:33`，路由在 `:47`）
- Modify: `app/api/qa_routes.py`（`:18`）
- Modify: `app/api/session_routes.py`（`:11`）
- Modify: `app/api/deps.py`（新增 `require_chat_session`）
- Test: `tests/api/test_session_routes.py`、`tests/api/test_agent_routes.py`

**Interfaces:**
- Consumes: Task 3 的 `require_admin_session`、`require_csrf`
- Produces: `async def require_chat_session(session: AdminSession = Depends(require_admin_session)) -> tuple[str, str]`——返回 `(tenant_id, user_id)`，租户取 `current_tenant_id`，用户取 `username`；`current_tenant_id` 为 `None` 时 400

- [ ] **Step 1: 写失败测试**

追加到 `tests/api/test_session_routes.py`：

```python
def test_sessions_require_login(client):
    """未登录必须 401。这五个接口此前完全敞开——后端启动时那条
    「任何调用方都可以伪造租户身份绕过多租户隔离」的警告说的就是它们。"""
    response = client.get("/agent/sessions")
    assert response.status_code == 401


def test_sessions_are_scoped_to_the_logged_in_user(client_alice, client_bob):
    """user_id 从会话取，不再是 URL 参数。

    此前 user_id 是明文查询参数且没有归属校验——换一个值就能读别人的会话
    历史。这条测试钉的就是那个洞：bob 无论如何都不该看到 alice 的会话。
    """
    alice_sessions = client_alice.get("/agent/sessions").json()["sessions"]
    bob_sessions = client_bob.get("/agent/sessions").json()["sessions"]
    alice_ids = {s["session_id"] for s in alice_sessions}
    bob_ids = {s["session_id"] for s in bob_sessions}
    assert alice_ids.isdisjoint(bob_ids)
```

追加到 `tests/api/test_agent_routes.py`（不存在则新建）：

```python
def test_chat_requires_login(client):
    response = client.post("/agent/chat", json={"question": "你好"})
    assert response.status_code == 401


def test_chat_ignores_tenant_id_in_body(client_member_demo):
    """租户从会话取，body 里的 tenant_id 被忽略。

    此前 member 只要把 body 里的 tenant_id 换成别的租户就能读写那个租户，
    返回 200、没有日志也没有报错——deps.py:384 那道越权校验只保护
    /api/admin/*，前台完全绕过它。
    """
    response = client_member_demo.post(
        "/agent/chat", json={"question": "你好", "tenant_id": "another-tenant"}
    )
    assert response.status_code != 403
    # 实际落到的租户是会话里的 demo，不是 body 里那个
    assert "another-tenant" not in response.text
```

**实施者注意**：`client_alice` / `client_bob` / `client_member_demo` 这三个 fixture 需要你新建——照 `tests/api/test_admin_auth_routes.py` 里既有 `client` fixture 的写法，分别用不同账号登录后返回带 Cookie 的 `TestClient`。**先读那个文件确认真实写法再照抄**。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/api/test_session_routes.py tests/api/test_agent_routes.py -q`

Expected: FAIL——未登录时返回 200 而不是 401

- [ ] **Step 3: 实现**

`app/api/deps.py` 新增：

```python
async def require_chat_session(
    session: AdminSession = Depends(require_admin_session),
) -> tuple[str, str]:
    """前台问答的身份：返回 (tenant_id, user_id)。

    租户取 current_tenant_id 而不是 tenant_id——admin 的 tenant_id 恒为
    None，用它的话 admin 在前台根本问不了任何问题。member 两者恒等。

    user_id 取 username：会话历史此后按账号归属，不再是客户端自报的
    随机 UUID。既有的匿名会话因此变成孤儿，这是设计里明确接受的代价
    （见 spec 决定 2）。
    """
    if session.current_tenant_id is None:
        raise HTTPException(status_code=400, detail="请先选择一个租户")
    return session.current_tenant_id, session.username
```

三个 router 加全局依赖：

```python
# agent_routes.py / qa_routes.py / session_routes.py
router = APIRouter(dependencies=[Depends(deps.require_csrf)])
```

各路由签名加 `identity: tuple[str, str] = Depends(deps.require_chat_session)`，函数体里 `tenant_id, user_id = identity`。

`AgentChatRequest` 的 `tenant_id` 字段**保留但不再使用**（删掉会让既有客户端 422，而忽略它是无声的兼容），在字段上加注释说明。

`session_routes.py` 三个接口**移除** `tenant_id` / `user_id` 查询参数。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/api/test_session_routes.py tests/api/test_agent_routes.py -q`

- [ ] **Step 5: 两次变异验证**

跑整份文件（不加 `-k`/`-t`）：

1. 把三个 router 的 `dependencies` 去掉、路由签名里的 `require_chat_session` 去掉 → 「未登录 401」那两条应 FAIL
2. 把 `require_chat_session` 返回的 `user_id` 改成从查询参数取 → 「会话按登录者隔离」那条应 FAIL

- [ ] **Step 6: 全量与提交**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`

**预期这一步会打断一批既有测试**——直连这五个接口且未覆盖依赖的那些。逐个改成先登录再请求，**不要**用 `dependency_overrides` 绕过认证：那样等于把刚立起来的门在测试里拆掉，以后有人改坏认证不会被发现。

```bash
git add app/api/deps.py app/api/agent_routes.py app/api/qa_routes.py app/api/session_routes.py tests/api/
git commit -m "feat(api): 前台五个接口装上认证门，身份从会话取

/agent/chat、/qa、三个 /agent/sessions 此前完全无鉴权。租户从会话取、忽略
body 里的 tenant_id；user_id 从会话取、移除查询参数。

后者堵的是一个今天就存在的洞：user_id 是 URL 明文参数且没有归属校验，换一
个值就能读别人的会话历史。

租户取 current_tenant_id 而不是 tenant_id——admin 的 tenant_id 恒为 None，
用它的话 admin 在前台问不了任何问题。"
```

---

## 阶段二：前端（Task 5-7）

### Task 5: `adminFetch` 改走 Cookie

**Files:**
- Create: `frontend/src/lib/csrf.ts`
- Modify: `frontend/src/admin/adminApi.ts`
- Test: `frontend/src/admin/adminApi.test.ts`（不存在则新建）

**Interfaces:**
- Produces: `export function readCsrfToken(): string | null`——从 `document.cookie` 读 `customer_rag_csrf`

- [ ] **Step 1: 写失败测试**

```ts
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { adminFetch } from './adminApi'

describe('adminFetch', () => {
  beforeEach(() => {
    document.cookie = 'customer_rag_csrf=tok123; path=/'
  })

  it('带上 Cookie，不再手工塞 Authorization 头', async () => {
    // 会话改成 HttpOnly Cookie 之后 JS 读不到 token，只能靠浏览器自动
    // 携带——credentials 不设成 include 的话同源请求也不会带 Cookie。
    const fetchMock = vi.fn(() => Promise.resolve(new Response('{}')))
    vi.stubGlobal('fetch', fetchMock)
    await adminFetch('/api/admin/whoami', '')
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.credentials).toBe('include')
    expect(new Headers(init.headers).get('Authorization')).toBeNull()
  })

  it('写请求带上 X-CSRF-Token，读请求不带', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(new Response('{}')))
    vi.stubGlobal('fetch', fetchMock)

    await adminFetch('/api/admin/x', '', { method: 'POST' })
    expect(new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers).get('X-CSRF-Token')).toBe('tok123')

    fetchMock.mockClear()
    await adminFetch('/api/admin/x', '')
    expect(new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers).get('X-CSRF-Token')).toBeNull()
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/admin/adminApi.test.ts`

- [ ] **Step 3: 实现**

新建 `frontend/src/lib/csrf.ts`：

```ts
const CSRF_COOKIE_NAME = 'customer_rag_csrf'

/**
 * 读 CSRF 令牌。
 *
 * 这个 Cookie 刻意不是 HttpOnly——双提交令牌就建立在「会话 Cookie 读不到、
 * CSRF Cookie 读得到」这个不对称上：攻击者站点能让浏览器带上会话 Cookie，
 * 但读不到这个值、也就填不出请求头。
 */
export function readCsrfToken(): string | null {
  const hit = document.cookie
    .split(';')
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${CSRF_COOKIE_NAME}=`))
  return hit ? decodeURIComponent(hit.slice(CSRF_COOKIE_NAME.length + 1)) : null
}
```

`adminApi.ts` 的 `adminFetch`：`import { readCsrfToken } from '../lib/csrf'`，去掉 `Authorization` 头，加 `credentials: 'include'`；方法不是 GET/HEAD 时，用 `readCsrfToken()` 的返回值填 `X-CSRF-Token` 头（返回 `null` 时不加这个头——未登录本来就没有令牌，硬塞一个空值只会把 401 变成更难懂的 403）。`sessionToken` 参数**暂时保留**（21 个调用方都在传），只是不再使用——Task 6 一并清理。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/admin/adminApi.test.ts`

- [ ] **Step 5: 两次变异验证**

跑整份文件（不加 `-t`）：

1. 去掉 `credentials: 'include'` → 第一条应 FAIL
2. 让 GET 请求也带 `X-CSRF-Token` → 第二条应 FAIL

- [ ] **Step 6: 提交**

```bash
git add frontend/src/lib/csrf.ts frontend/src/admin/adminApi.ts frontend/src/admin/adminApi.test.ts
git commit -m "feat(frontend): adminFetch 改走 Cookie 与 CSRF 头

会话改成 HttpOnly Cookie 后 JS 读不到 token，只能靠浏览器自动携带——
credentials 不设成 include 的话同源请求也不会带 Cookie。

CSRF 头只加在写请求上：读请求本来就不在 CSRF 的威胁模型里，无差别加只是
噪音。"
```

---

### Task 6: 会话状态由 whoami 驱动，租户走服务端

**Files:**
- Modify: `frontend/src/admin/useAdminAuth.ts`（`SESSION_STORAGE_KEY` 在 `:4`）
- Modify: `frontend/src/admin/TenantContext.tsx`（`TENANT_STORAGE_KEY` 在 `:4`）
- Test: `frontend/src/admin/sessionExpiry.test.tsx`

- [ ] **Step 1: 写失败测试**

```tsx
it('会话在服务端已失效时，清掉本地状态跳登录页', async () => {
  // 会话是进程内的，后端一重启所有人都要重新登录。Cookie 还在、界面看起来
  // 像登录着，服务端却已不认——不处理的话用户会卡在一个「显示已登录但什么
  // 都点不动」的界面里。
  signInWithCookie()
  stubApi({ whoami: 401 })
  renderAt(ADMIN_ROUTES.ontology)
  expect(await screen.findByLabelText(/用户名/)).toBeTruthy()
})

it('切换租户走服务端，不写 sessionStorage', async () => {
  signInWithCookie()
  const requests = stubApi({ whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: 'demo' } })
  renderAt(ADMIN_ROUTES.ontology)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: /demo/ }))
  await user.click(await screen.findByRole('menuitemradio', { name: /acme/ }))
  expect(requests.some((r) => r.url.endsWith('/session/tenant') && r.method === 'PUT')).toBe(true)
  expect(sessionStorage.getItem('admin_current_tenant')).toBeNull()
})
```

**实施者注意**：`signInWithCookie` / `stubApi` / `renderAt` 需要你自建——本仓库前端测试**没有共享 render 助手**，每个文件自定义一份。照 `frontend/src/admin/guidedOntology/guidedPage.test.tsx` 开头那一段的写法。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/admin/sessionExpiry.test.tsx`

- [ ] **Step 3: 实现**

`useAdminAuth.ts`：删掉三个 `sessionStorage` 读写（token/username/role），改为挂载时调 `GET /api/admin/whoami`；401 时清状态并跳 `/admin/login`。`login()` 不再存 token（Cookie 由服务端下发），只刷新 whoami 状态。

`TenantContext.tsx`：`tenantId` 初值来自 whoami 的 `current_tenant_id`；`setTenantId` 改为 `PUT /api/admin/session/tenant` 成功后再更新本地状态——**失败时不更新**，否则界面显示的租户和服务端生效的会不一致。

`WhoAmIResponse` 需要在后端加上 `current_tenant_id` 字段（`app/api/admin_auth_routes.py:40-43`）。

- [ ] **Step 4: 跑测试确认通过**

- [ ] **Step 5: 两次变异验证**

1. 401 时不跳转（只清状态）→ 第一条应 FAIL
2. `setTenantId` 改成先更新本地再发请求、且不管成败 → 第二条应 FAIL

- [ ] **Step 6: 全量与提交**

Run: `cd frontend && npx vitest run && npx tsc --noEmit`

**预期这一步会打断 28 个前端测试文件里的 `signIn()` 助手**——它们往 `sessionStorage` 塞 token。逐个改成 stub `whoami`。

```bash
git add frontend/src/admin/useAdminAuth.ts frontend/src/admin/TenantContext.tsx frontend/src/admin/sessionExpiry.test.tsx app/api/admin_auth_routes.py frontend/src/
git commit -m "feat(frontend): 会话状态由 whoami 驱动，租户切换走服务端

token 不再存 sessionStorage（它按标签页隔离，达不到前后台共用一套会话的
目标），会话状态改由 whoami 驱动。

切换租户先发请求、成功后才更新本地状态：反过来的话请求失败时界面显示的
租户和服务端生效的会不一致。

401 时清本地状态并跳登录页——Cookie 还在、界面看起来像登录着而服务端已
不认，不处理的话用户会卡在显示已登录但什么都点不动的界面里。"
```

---

### Task 7: 前台登录门与账号块

**Files:**
- Modify: `frontend/src/App.tsx`（前台路由在 `:29`）
- Modify: `frontend/src/pages/ChatPage.tsx`
- Modify: `frontend/src/hooks/useAgentChat.ts`（`TENANT_ID` 在 `:12`）
- Modify: `frontend/src/lib/sessionsApi.ts`
- Delete: `frontend/src/lib/identity.ts`
- Create: `frontend/src/authGate.test.tsx`

- [ ] **Step 1: 写失败测试**

```tsx
it('未登录时前台渲染登录表单，不渲染问答界面', async () => {
  stubApi({ whoami: 401 })
  renderAt('/')
  expect(await screen.findByLabelText(/用户名/)).toBeTruthy()
  expect(screen.queryByPlaceholderText(/输入你的问题/)).toBeNull()
})

it('登录后前台显示账号块，但没有账号管理和租户管理', async () => {
  // 前台是「用知识库」的地方，后台是「管知识库」的地方。把管理入口塞进
  // 问答界面，等于把建模→接入→审核这条流程的入口散回一个不属于它的页面。
  stubApi({ whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: 'demo' } })
  renderAt('/')
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: /admin/ }))
  expect(screen.getByRole('menuitem', { name: '设置' })).toBeTruthy()
  expect(screen.queryByRole('menuitem', { name: '账号管理' })).toBeNull()
  expect(screen.queryByRole('menuitem', { name: '租户管理' })).toBeNull()
})

it('前台给 admin 显示租户切换器', async () => {
  // 换租户即换知识库，admin 需要验证「我刚配好的本体，问答到底通不通」。
  stubApi({ whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: 'demo' } })
  renderAt('/')
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: /admin/ }))
  expect(await screen.findByRole('menuitemradio', { name: /demo/ })).toBeTruthy()
})

it('member 看不到租户切换器', async () => {
  stubApi({ whoami: { username: 'alice', role: 'member', tenant_id: 'demo', current_tenant_id: 'demo' } })
  renderAt('/')
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: /alice/ }))
  expect(screen.queryByRole('menuitemradio')).toBeNull()
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/authGate.test.tsx`

- [ ] **Step 3: 实现**

`App.tsx`：`/` 路由包一层登录门——未登录渲染 `LoginPage`（**复用现有组件，不新写一套**），已登录渲染 `ChatPage`。

`ChatPage.tsx`：加账号块，复用 `AccountMenu` 组件但按场景裁剪——传一个 prop 控制是否渲染账号管理/租户管理两项。

`useAgentChat.ts`：删掉 `TENANT_ID` 常量、`getAnonymousUserId` 调用与 `userIdRef`；请求不再带 `tenant_id` / `user_id`。

`sessionsApi.ts`：三个函数去掉 `tenantId` / `userId` 参数。

删除 `frontend/src/lib/identity.ts`。

- [ ] **Step 4: 跑测试确认通过**

- [ ] **Step 5: 三次变异验证**

跑整份文件（不加 `-t`）：

1. 未登录时也渲染 `ChatPage` → 第一条应 FAIL
2. 前台的账号块不裁剪（照搬后台那份）→ 第二条应 FAIL
3. 给 member 也渲染租户切换器 → 第四条应 FAIL

- [ ] **Step 6: 全量与提交**

Run: `cd frontend && npx vitest run && npx tsc --noEmit && npx vite build`

```bash
git add frontend/src/App.tsx frontend/src/pages/ChatPage.tsx frontend/src/hooks/useAgentChat.ts frontend/src/lib/sessionsApi.ts frontend/src/authGate.test.tsx
git rm frontend/src/lib/identity.ts
git commit -m "feat(frontend): 前台加登录门与账号块

前台面向内部坐席，本来就该登录。租户与用户身份改从服务端会话取，删掉
硬编码的 TENANT_ID 与 localStorage 里的匿名 UUID（换浏览器就换个身份、
看不到旧会话，那对坐席本身就是缺陷）。

账号块不是把后台那份挪过来，是共用组件、两处渲染、内容按场景裁剪：账号
管理与租户管理不进前台——前台是用知识库的地方，后台是管知识库的地方。

给 admin 保留租户切换器：换租户即换知识库，而 admin 需要验证刚配好的本体
问答到底通不通。"
```

---

## 阶段验收

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q
cd frontend && npx vitest run && npx tsc --noEmit && npx vite build
```

**手工验证**（自动化测不到真实浏览器的 Cookie 行为）：

1. 未登录访问 `http://localhost:5173/`，应看到登录表单而不是问答界面
2. 登录后**新开一个标签页**打开 `http://localhost:5173/admin`，应直接进后台、不再要求登录（这是本设计的核心目标，`sessionStorage` 时代做不到）
3. 前台左下角账号块：有设置/登出，**没有**账号管理和租户管理
4. 用 `admin` 登录，在前台切到另一个租户，问一个只有那个租户才答得出的问题，确认答案来自新租户
5. 用 `member` 登录，确认前台**没有**租户切换器
6. 重启后端（`powershell -File scripts/stop-backend.ps1; powershell -File scripts/start-backend.ps1`），刷新页面——应被跳回登录页，**不能停在「显示已登录但点不动」的界面**
7. 点登出后，用浏览器开发者工具确认两个 Cookie 都没了
8. 开发者工具里删掉 `customer_rag_csrf` Cookie，再触发一次写操作（比如切租户），应被拒绝

**若第 2 步仍要求登录**：查 Cookie 的 `Path` 是不是 `/`——写成 `/api` 的话 `/admin` 这个路径带不上它。
