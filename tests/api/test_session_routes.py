from datetime import datetime

import aiosqlite
from fastapi.testclient import TestClient

from app.api import deps
from app.main import app
from app.memory.chat_sessions import touch_session
from app.memory.schema import ensure_schema
from app.memory.session_window import append_turn


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
                conn, tenant_id="t1", session_id="s1", user_id="u1",
                first_message="网络连不上怎么办？", now=datetime(2026, 8, 12, 10, 0, 0),
            )
            await append_turn(
                conn, tenant_id="t1", session_id="s1", user_id="u1",
                role="user", content="网络连不上怎么办？",
            )
            await append_turn(
                conn, tenant_id="t1", session_id="s1", user_id="u1",
                role="assistant", content="请先重启路由器。",
            )
            state["conn"] = conn
        return state["conn"]

    return _get


def test_list_sessions_returns_sessions_for_tenant_and_user():
    app.dependency_overrides[deps.get_memory_conn] = _seeded_memory_conn_override()
    try:
        with TestClient(app) as client:
            response = client.get(
                "/agent/sessions", params={"tenant_id": "t1", "user_id": "u1"}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["session_id"] == "s1"
    assert body["sessions"][0]["title"] == "网络连不上怎么办？"


def test_list_sessions_does_not_leak_another_users_sessions():
    app.dependency_overrides[deps.get_memory_conn] = _seeded_memory_conn_override()
    try:
        with TestClient(app) as client:
            response = client.get(
                "/agent/sessions", params={"tenant_id": "t1", "user_id": "someone-else"}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["sessions"] == []


def test_get_session_messages_returns_full_turn_history():
    app.dependency_overrides[deps.get_memory_conn] = _seeded_memory_conn_override()
    try:
        with TestClient(app) as client:
            response = client.get(
                "/agent/sessions/s1/messages", params={"tenant_id": "t1", "user_id": "u1"}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    messages = response.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "网络连不上怎么办？"
    assert messages[1]["content"] == "请先重启路由器。"


def test_get_session_messages_returns_empty_for_unknown_session():
    app.dependency_overrides[deps.get_memory_conn] = _seeded_memory_conn_override()
    try:
        with TestClient(app) as client:
            response = client.get(
                "/agent/sessions/no-such-session/messages",
                params={"tenant_id": "t1", "user_id": "u1"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["messages"] == []


def test_delete_session_removes_it_from_the_list():
    app.dependency_overrides[deps.get_memory_conn] = _seeded_memory_conn_override()
    try:
        with TestClient(app) as client:
            delete_response = client.delete(
                "/agent/sessions/s1", params={"tenant_id": "t1", "user_id": "u1"}
            )
            list_response = client.get(
                "/agent/sessions", params={"tenant_id": "t1", "user_id": "u1"}
            )
    finally:
        app.dependency_overrides.clear()

    assert delete_response.status_code == 200
    assert delete_response.json() == {"deleted": True}
    assert list_response.json()["sessions"] == []


def test_delete_session_returns_404_when_not_owned_by_this_user():
    app.dependency_overrides[deps.get_memory_conn] = _seeded_memory_conn_override()
    try:
        with TestClient(app) as client:
            response = client.delete(
                "/agent/sessions/s1", params={"tenant_id": "t1", "user_id": "someone-else"}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
