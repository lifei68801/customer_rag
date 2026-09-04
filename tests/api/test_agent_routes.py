"""/agent/chat 的认证门。

流式行为、Planner 路径、语音合成那些用例在 test_agent_chat_routes.py 里，
这里只管"谁能进来、进来之后算谁"。
"""
from __future__ import annotations

import json
from typing import Iterator

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.session_cookie import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from app.auth.admin_users_store import create_admin_user, ensure_admin_users_schema
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.tenants_store import create_tenants_table
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.terms_store import ensure_terms_schema
from app.main import app
from app.memory.schema import ensure_schema
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord
from tests.api.conftest import login_client
from tests.settings_factory import build_settings


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


class FakeLLMProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text="按资料所述，重启路由器即可解决。")


_FAKE_RECORDS = [
    VectorRecord(
        id="faq/network.md",
        vector=[1.0, 0.0],
        text="网络断开时，请先重启路由器。",
        tenant_id="demo",
        metadata={},
    )
]


@pytest.fixture
def agent_chat_env(default_admin_users_conn) -> Iterator[None]:
    """一个登录成 demo 租户 member 的客户端，且 /agent/chat 需要的 provider
    都换成了假实现。

    review_conn 要同时装得下 admin_users（登录与会话校验查它）和术语/本体
    那几张表（agent_chat_endpoint 直接用 review_conn 查它们），所以这里整个
    换掉 conftest 那个只有 admin_users 的兜底连接。连接惰性创建、在同一个
    测试内复用同一个实例：登录写进去的账号，后续请求还要能查到。
    """
    state: dict[str, aiosqlite.Connection] = {}

    async def _review_conn() -> aiosqlite.Connection:
        if "conn" not in state:
            conn = await aiosqlite.connect(":memory:")
            await ensure_terms_schema(conn)
            await ensure_term_edits_schema(conn)
            await ensure_ontology_schema(conn)
            await ensure_admin_users_schema(conn)
            await create_admin_user(
                conn,
                username="demo_member",
                password="password1",
                role="member",
                tenant_id="demo",
            )
            # admin 的 tenant_id 恒为 None，它在前台必须先切租户——设计约束 3
            # 与"没选租户就 400"那条分支都只在 admin 这条路径上才看得见。
            await create_admin_user(
                conn,
                username="admin",
                password="password1",
                role="admin",
                tenant_id=None,
            )
            # 切租户走 require_active_tenant_or_404，要有 tenants 表和这一行；
            # 照 test_admin_auth_routes.py 里 _seed_tenant_row 的做法。
            await create_tenants_table(conn)
            await conn.execute(
                "INSERT OR REPLACE INTO tenants (tenant_id, name, status) VALUES (?, ?, ?)",
                ("demo", "demo", "active"),
            )
            await conn.commit()
            state["conn"] = conn
        return state["conn"]

    memory_state: dict[str, aiosqlite.Connection] = {}

    async def _memory_conn() -> aiosqlite.Connection:
        if "conn" not in memory_state:
            conn = await aiosqlite.connect(":memory:")
            await ensure_schema(conn)
            memory_state["conn"] = conn
        return memory_state["conn"]

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider()
    )
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, deps.DEFAULT_LLM_PROVIDER_NAME, FakeLLMProvider()
    )

    async def _vector_store() -> InMemoryVectorStore:
        store = InMemoryVectorStore()
        await store.upsert(_FAKE_RECORDS)
        return store

    def _bm25_index() -> BM25Index:
        index = BM25Index()
        index.index(_FAKE_RECORDS)
        return index

    app.dependency_overrides[deps.get_review_conn] = _review_conn
    app.dependency_overrides[deps.get_memory_conn] = _memory_conn
    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm_registry
    app.dependency_overrides[deps.get_vector_store] = _vector_store
    app.dependency_overrides[deps.get_bm25_index] = _bm25_index
    app.dependency_overrides[deps.get_rerank_provider] = lambda: None
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    app.dependency_overrides[deps.get_tts_provider] = lambda: None
    app.dependency_overrides[deps.get_settings] = lambda: build_settings()
    try:
        yield
    finally:
        for dep in (
            deps.get_review_conn,
            deps.get_memory_conn,
            deps.get_embedding_registry,
            deps.get_llm_registry,
            deps.get_vector_store,
            deps.get_bm25_index,
            deps.get_rerank_provider,
            deps.get_graph_client,
            deps.get_tts_provider,
            deps.get_settings,
        ):
            app.dependency_overrides.pop(dep, None)


@pytest.fixture
def client_member_demo(agent_chat_env) -> TestClient:
    return login_client("demo_member")


@pytest.fixture
def client_admin(agent_chat_env) -> TestClient:
    """admin 登录之后 current_tenant_id 仍是 None——它得先显式切租户。"""
    return login_client("admin")


def _final_event(body: str) -> dict:
    """从 SSE 响应体里取那个权威的 final 事件。"""
    events = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        assert block.startswith("data: ")
        events.append(json.loads(block[len("data: ") :]))
    finals = [e for e in events if e.get("type") == "final"]
    assert len(finals) == 1, body
    return finals[0]


def _switch_tenant(client: TestClient, tenant_id: str) -> None:
    response = client.put(
        "/api/admin/auth/session/tenant",
        json={"tenant_id": tenant_id},
        headers={CSRF_HEADER_NAME: client.cookies.get(CSRF_COOKIE_NAME)},
    )
    assert response.status_code == 200, response.text


def test_chat_requires_login(client):
    response = client.post("/agent/chat", json={"question": "你好"})
    assert response.status_code == 401


def test_chat_ignores_tenant_id_in_body(client_member_demo):
    """租户从会话取，body 里的 tenant_id 被忽略。

    此前 member 只要把 body 里的 tenant_id 换成别的租户就能读写那个租户，
    返回 200、没有日志也没有报错——deps.py 那道越权校验只保护 /api/admin/*，
    前台完全绕过它。

    断言必须看**检索结果**而不是状态码或响应文本：_FAKE_RECORDS 只挂在
    tenant_id="demo" 下，body 里的租户一旦被采信，used_sources 就会是空的。
    brief 原来给的写法（"assert 状态码 != 403 且响应里不含 another-tenant"）
    在"body 租户被采信"时同样成立，是假绿——复审实测确认过。
    """
    response = client_member_demo.post(
        "/agent/chat", json={"question": "网络连不上怎么办？", "tenant_id": "another-tenant"}
    )

    assert response.status_code == 200
    assert _final_event(response.text)["used_sources"] == ["faq/network.md"]


def test_chat_uses_the_tenant_the_admin_switched_to(client_admin):
    """admin 的租户取 current_tenant_id，不是 tenant_id。

    这条是设计约束 3 唯一走得到的路径：member 两者恒等，只有 admin 的
    tenant_id 恒为 None，改用它的话 admin 在前台一句话也问不出来（会掉进
    "请先选择一个租户"那个 400）。
    """
    _switch_tenant(client_admin, "demo")

    response = client_admin.post(
        "/agent/chat", json={"question": "网络连不上怎么办？"}
    )

    assert response.status_code == 200
    assert _final_event(response.text)["used_sources"] == ["faq/network.md"]


def test_chat_refuses_when_no_current_tenant_is_selected(client_admin):
    """没选租户就必须报错，不能悄悄挑一个。

    admin 刚登录时 current_tenant_id 是 None。这里要的是一个明确的 400——
    静默回落到某个默认租户会让 admin 在毫不知情的情况下读写错租户的数据，
    而且不会有任何日志或报错。
    """
    response = client_admin.post(
        "/agent/chat", json={"question": "网络连不上怎么办？"}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "请先选择一个租户"
