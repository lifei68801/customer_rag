from datetime import datetime
from typing import Iterator

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.main import app
from app.memory.chat_sessions import touch_session
from app.memory.schema import ensure_schema
from app.memory.session_window import append_turn
from tests.api.conftest import login_client, seed_member


def _seeded_memory_conn_override():
    """惰性创建并播种一个 :memory: SQLite 连接，且同一个测试内多次请求
    复用同一个连接实例（不是每次都建一个新的空库）——用来支持"先删除再
    查列表"这类需要跨请求共享状态的测试。

    连接必须在 TestClient 实际处理请求的那个事件循环内创建，不能提前用
    asyncio.run() 建好再传入（aiosqlite 内部有个绑定到"创建时那个循环"的
    后台线程，见 test_agent_chat_routes.py 里同样问题的说明），所以这里
    包一层闭包，把 aiosqlite.connect() 推迟到 FastAPI 第一次解析这个依赖时
    才执行。
    """
    state: dict[str, aiosqlite.Connection] = {}

    async def _get() -> aiosqlite.Connection:
        if "conn" not in state:
            conn = await aiosqlite.connect(":memory:")
            await ensure_schema(conn)
            await touch_session(
                conn, tenant_id="t1", session_id="s1", user_id="user1",
                first_message="网络连不上怎么办？", now=datetime(2026, 8, 12, 10, 0, 0),
            )
            await append_turn(
                conn, tenant_id="t1", session_id="s1", user_id="user1",
                role="user", content="网络连不上怎么办？",
            )
            await append_turn(
                conn, tenant_id="t1", session_id="s1", user_id="user1",
                role="assistant", content="请先重启路由器。",
            )
            state["conn"] = conn
        return state["conn"]

    return _get


@pytest.fixture
def seeded_memory(default_admin_users_conn) -> Iterator[None]:
    app.dependency_overrides[deps.get_memory_conn] = _seeded_memory_conn_override()
    try:
        yield
    finally:
        app.dependency_overrides.pop(deps.get_memory_conn, None)


@pytest.fixture
def client_user1(chat_settings, seeded_memory) -> TestClient:
    """播种数据的主人：租户 t1、用户 user1。

    身份从会话取之后，"以 user1 的身份请求"只能靠真的用这个账号登录——
    不能再靠 URL 上的 user_id=user1，也不用 dependency_overrides 把
    require_chat_session 顶掉。
    """
    seed_member("user1", tenant_id="t1")
    return login_client("user1")


@pytest.fixture
def client_someone_else(chat_settings, seeded_memory) -> TestClient:
    """同一个租户里的另一个账号，用来替代原先"把 user_id 换成别人"的写法。"""
    seed_member("someone-else", tenant_id="t1")
    return login_client("someone-else")


def test_list_sessions_returns_sessions_for_tenant_and_user(client_user1):
    response = client_user1.get("/agent/sessions")

    assert response.status_code == 200
    body = response.json()
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["session_id"] == "s1"
    assert body["sessions"][0]["title"] == "网络连不上怎么办？"


def test_list_sessions_does_not_leak_another_users_sessions(client_someone_else):
    response = client_someone_else.get("/agent/sessions")

    assert response.status_code == 200
    assert response.json()["sessions"] == []


def test_get_session_messages_returns_full_turn_history(client_user1):
    response = client_user1.get("/agent/sessions/s1/messages")

    assert response.status_code == 200
    messages = response.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "网络连不上怎么办？"
    assert messages[1]["content"] == "请先重启路由器。"


def test_get_session_messages_returns_empty_for_unknown_session(client_user1):
    response = client_user1.get("/agent/sessions/no-such-session/messages")

    assert response.status_code == 200
    assert response.json()["messages"] == []


def test_delete_session_removes_it_from_the_list(client_user1):
    delete_response = client_user1.delete("/agent/sessions/s1")
    list_response = client_user1.get("/agent/sessions")

    assert delete_response.status_code == 200
    assert delete_response.json() == {"deleted": True}
    assert list_response.json()["sessions"] == []


def test_delete_session_returns_404_when_not_owned_by_this_user(client_someone_else):
    response = client_someone_else.delete("/agent/sessions/s1")

    assert response.status_code == 404


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


def test_sessions_show_the_logged_in_users_own_history(client_alice, client_bob):
    """上一条断言的是"互相看不见"，两边都空也满足它。这条补上"各自看得见
    自己的"，否则一个把所有人都返回空列表的实现同样能让上一条变绿。"""
    alice_ids = {s["session_id"] for s in client_alice.get("/agent/sessions").json()["sessions"]}
    bob_ids = {s["session_id"] for s in client_bob.get("/agent/sessions").json()["sessions"]}

    assert alice_ids == {"s-alice"}
    assert bob_ids == {"s-bob"}
