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
from datetime import datetime
from typing import Iterator

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.session_cookie import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from app.auth.admin_users_store import create_admin_user, ensure_admin_users_schema
from app.main import app
from app.memory.chat_sessions import touch_session
from app.memory.schema import ensure_schema
from tests.settings_factory import build_settings


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


# ---------------------------------------------------------------------------
# 前台五个接口（/qa、/agent/chat、三个 /agent/sessions）的登录夹具
#
# 这五个接口装门之后，测试必须真的走一遍"登录、拿 Cookie、再请求"。不用
# dependency_overrides 把 require_chat_session 顶掉：那样等于在测试里把刚
# 立起来的门拆掉，以后谁改坏了认证也不会有任何测试变红。
# ---------------------------------------------------------------------------


def seed_member(username: str, *, tenant_id: str = "demo", password: str = "password1") -> None:
    """往当前 get_review_conn 指向的那个库里加一个 member 账号。

    照 tests/api/test_admin_auth_routes.py 里 _seed_member 的做法，直接对
    override 之后的连接调 create_admin_user，不自己拼 SQL。
    """
    conn = app.dependency_overrides[deps.get_review_conn]()
    asyncio.run(
        create_admin_user(
            conn, username=username, password=password, role="member", tenant_id=tenant_id
        )
    )


def login_client(username: str, *, password: str = "password1") -> TestClient:
    """登录并返回一个带会话 Cookie 的 TestClient。

    登录之后把 CSRF 令牌固定进这个 client 的默认请求头：有会话 Cookie 时
    require_csrf 会校验写方法的 X-CSRF-Token，不带的话 POST/DELETE 会 403。
    """
    client = TestClient(app)
    response = client.post(
        "/api/admin/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    client.headers[CSRF_HEADER_NAME] = client.cookies.get(CSRF_COOKIE_NAME)
    return client


@pytest.fixture
def chat_settings(default_admin_users_conn) -> Iterator[None]:
    """把 Settings 钉死成测试构造的那份。

    不 override 的话 get_settings 会读开发者本机的 .env，一旦那里配了
    CUSTOMER_RAG_GATEWAY_SHARED_SECRET，这些跟网关无关的用例会因为缺少
    网关凭证被 401 拒绝（理由同 test_qa_routes.py 里既有的说明）。
    """
    app.dependency_overrides[deps.get_settings] = lambda: build_settings()
    try:
        yield
    finally:
        app.dependency_overrides.pop(deps.get_settings, None)


@pytest.fixture
def chat_memory_conn(default_admin_users_conn) -> Iterator[None]:
    """一个播了两条会话的 memory 库：s-alice 属于 alice，s-bob 属于 bob。

    连接惰性创建（推迟到 FastAPI 第一次解析这个依赖时），并在同一个测试内
    复用同一个实例——alice 和 bob 各有自己的 TestClient，两边必须看到同一
    份数据，否则"互相看不见"这个断言是假的。
    """
    state: dict[str, aiosqlite.Connection] = {}

    async def _get() -> aiosqlite.Connection:
        if "conn" not in state:
            conn = await aiosqlite.connect(":memory:")
            await ensure_schema(conn)
            await touch_session(
                conn, tenant_id="demo", session_id="s-alice", user_id="alice",
                first_message="alice 的问题", now=datetime(2026, 9, 4, 10, 0, 0),
            )
            await touch_session(
                conn, tenant_id="demo", session_id="s-bob", user_id="bob",
                first_message="bob 的问题", now=datetime(2026, 9, 4, 10, 0, 0),
            )
            state["conn"] = conn
        return state["conn"]

    app.dependency_overrides[deps.get_memory_conn] = _get
    try:
        yield
    finally:
        app.dependency_overrides.pop(deps.get_memory_conn, None)


@pytest.fixture
def client(chat_settings) -> TestClient:
    """没登录的客户端。"""
    return TestClient(app)


@pytest.fixture
def client_alice(chat_settings, chat_memory_conn) -> TestClient:
    seed_member("alice")
    return login_client("alice")


@pytest.fixture
def client_bob(chat_settings, chat_memory_conn) -> TestClient:
    seed_member("bob")
    return login_client("bob")
