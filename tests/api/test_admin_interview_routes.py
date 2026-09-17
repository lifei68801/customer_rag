from __future__ import annotations

import json

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSession
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.main import app
from app.providers.base import ProviderResult

pytestmark = pytest.mark.anyio

AUTH = {"Authorization": "Bearer x"}


class _ScriptedLLM:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.calls = 0

    async def run(self, capability, request, *, provider_name):
        self.calls += 1
        return ProviderResult(text=self.replies.pop(0))


async def _review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    await create_tenants_table(conn)
    await create_tenant(conn, tenant_id="t1", name="t1")
    return conn


@pytest.fixture
def holder() -> dict:
    return {}


@pytest.fixture
def llm() -> _ScriptedLLM:
    return _ScriptedLLM()


@pytest.fixture
def client(holder, llm):
    async def _get_conn():
        if "conn" not in holder:
            holder["conn"] = await _review_conn()
        return holder["conn"]

    app.dependency_overrides[deps.get_review_conn] = _get_conn
    # 身份用 admin：理由同 test_admin_modeling_workspace_routes.py——member 会去读
    # 测试连接里没有的 user_tenants 表；"对 member 开放"靠 tenant_scoped 无
    # require_admin_role 这一结构事实保证。
    app.dependency_overrides[deps.require_admin_session] = lambda: AdminSession(
        username="alice", role="admin", tenant_id=None, expires_at=1e18
    )
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm
    yield TestClient(app)
    app.dependency_overrides.clear()


_REPLY = json.dumps({
    "question": "商品分品类吗？",
    "add": {"term_types": [{"value": "商品", "rationale": "卖服装"}], "relation_types": [], "constraints": []},
    "done": False,
}, ensure_ascii=False)


def test_get_absent_returns_null(client):
    resp = client.get("/api/admin/ontology/t1/interview", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"session": None}


def test_create_starts_with_the_opening_question_without_calling_the_llm(client, llm):
    resp = client.post("/api/admin/ontology/t1/interview", headers=AUTH)
    assert resp.status_code == 200
    turns = resp.json()["session"]["state"]["turns"]
    assert turns[0]["role"] == "assistant"
    assert llm.calls == 0
    assert client.post("/api/admin/ontology/t1/interview", headers=AUTH).status_code == 409


def test_answer_appends_turns_and_merges_skeleton(client, llm):
    llm.replies = [_REPLY]
    created = client.post("/api/admin/ontology/t1/interview", headers=AUTH).json()["session"]
    resp = client.post(
        "/api/admin/ontology/t1/interview/answer",
        json={"answer": "我们卖服装", "updated_at": created["updated_at"]},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    turns = body["session"]["state"]["turns"]
    assert [t["role"] for t in turns] == ["assistant", "user", "assistant"]
    assert turns[-1]["text"] == "商品分品类吗？"
    assert body["session"]["state"]["skeleton"]["term_types"][0]["value"] == "商品"
    assert body["session"]["state"]["skeleton"]["term_types"][0]["from_turn"] == 1
    assert body["turn"]["question"] == "商品分品类吗？"
    assert body["turn"]["added_count"] == 1
    assert llm.calls == 1


def test_answer_with_stale_updated_at_is_409_before_calling_the_llm(client, llm):
    created = client.post("/api/admin/ontology/t1/interview", headers=AUTH).json()["session"]
    resp = client.post(
        "/api/admin/ontology/t1/interview/answer",
        json={"answer": "x", "updated_at": "stale"},
        headers=AUTH,
    )
    assert resp.status_code == 409
    # 冲突要在调模型之前判出来——调完再发现冲突等于白等一分钟
    assert llm.calls == 0
    assert created is not None


def test_answer_when_llm_returns_garbage_keeps_the_answer_and_explains(client, llm):
    llm.replies = ["我觉得需要一个商品"]
    created = client.post("/api/admin/ontology/t1/interview", headers=AUTH).json()["session"]
    resp = client.post(
        "/api/admin/ontology/t1/interview/answer",
        json={"answer": "我们卖服装", "updated_at": created["updated_at"]},
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    # 用户的回答不能丢；没加东西要说出来
    assert body["session"]["state"]["turns"][-1] == {"role": "user", "text": "我们卖服装"}
    assert body["turn"]["added_count"] == 0
    assert body["turn"]["note"]


def test_answer_without_session_is_404(client):
    resp = client.post("/api/admin/ontology/t1/interview/answer", json={"answer": "x", "updated_at": "y"}, headers=AUTH)
    assert resp.status_code == 404


def test_put_saves_review_decisions(client, llm):
    llm.replies = [_REPLY]
    created = client.post("/api/admin/ontology/t1/interview", headers=AUTH).json()["session"]
    answered = client.post(
        "/api/admin/ontology/t1/interview/answer",
        json={"answer": "我们卖服装", "updated_at": created["updated_at"]},
        headers=AUTH,
    ).json()["session"]
    state = answered["state"]
    state["skeleton"]["term_types"][0]["review"] = "accepted"
    resp = client.put(
        "/api/admin/ontology/t1/interview",
        json={"state": state, "updated_at": answered["updated_at"]},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["session"]["state"]["skeleton"]["term_types"][0]["review"] == "accepted"


def test_put_malformed_state_is_400(client):
    created = client.post("/api/admin/ontology/t1/interview", headers=AUTH).json()["session"]
    resp = client.put(
        "/api/admin/ontology/t1/interview",
        json={"state": {"done": "yes"}, "updated_at": created["updated_at"]},
        headers=AUTH,
    )
    assert resp.status_code == 400


def test_questions_endpoint_records_needs_and_computes_missing(client, llm):
    llm.replies = [json.dumps({"needs": {"term_types": ["品类", "商品"], "relation_types": []}}, ensure_ascii=False)]
    created = client.post("/api/admin/ontology/t1/interview", headers=AUTH).json()["session"]
    resp = client.post(
        "/api/admin/ontology/t1/interview/questions",
        json={"text": "哪个品类卖得最好？", "updated_at": created["updated_at"]},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["question"]["needs"]["term_types"] == ["品类", "商品"]
    # 骨架里什么都没有，两个都缺——由后端算，不信模型自报
    assert body["question"]["missing"] == ["品类", "商品"]
    assert body["session"]["state"]["questions"][0]["text"] == "哪个品类卖得最好？"


def test_delete_then_recreate(client):
    client.post("/api/admin/ontology/t1/interview", headers=AUTH)
    assert client.delete("/api/admin/ontology/t1/interview", headers=AUTH).status_code == 200
    assert client.get("/api/admin/ontology/t1/interview", headers=AUTH).json()["session"] is None
    assert client.post("/api/admin/ontology/t1/interview", headers=AUTH).status_code == 200


def test_unknown_tenant_is_404(client):
    assert client.get("/api/admin/ontology/nope/interview", headers=AUTH).status_code == 404
