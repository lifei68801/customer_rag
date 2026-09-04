"""/agent/chat 的认证门。

流式行为、Planner 路径、语音合成那些用例在 test_agent_chat_routes.py 里，
这里只管"谁能进来、进来之后算谁"。
"""
from __future__ import annotations

from typing import Iterator

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
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
def client_member_demo(default_admin_users_conn) -> Iterator[TestClient]:
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
            from app.auth.admin_users_store import create_admin_user, ensure_admin_users_schema

            await ensure_admin_users_schema(conn)
            await create_admin_user(
                conn,
                username="demo_member",
                password="password1",
                role="member",
                tenant_id="demo",
            )
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
        yield login_client("demo_member")
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
