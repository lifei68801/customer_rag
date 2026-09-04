import aiosqlite
from fastapi.testclient import TestClient

from app.api import deps
from app.auth.admin_users_store import create_admin_user, ensure_admin_users_schema
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.terms_store import ensure_terms_schema
from app.main import app
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord
from tests.api.conftest import login_client
from tests.settings_factory import build_settings as _settings


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
        tenant_id="t1",
        metadata={},
    )
]


async def _fake_vector_store() -> InMemoryVectorStore:
    store = InMemoryVectorStore()
    await store.upsert(_FAKE_RECORDS)
    return store


def _fake_bm25_index() -> BM25Index:
    index = BM25Index()
    index.index(_FAKE_RECORDS)
    return index


def _review_conn_override():
    """qa_endpoint 现在直接用 review_conn 查术语表（不再经过已删除的
    deps.get_terms，见 app/api/deps.py 顶部说明）——空 schema 的
    review_conn 语义等价于之前 `dependency_overrides[deps.get_terms] =
    lambda: []`，这几个测试都不关心具体术语内容。

    /qa 装上认证门之后这个连接还要装下 admin_users：登录和每个请求的会话
    校验都查它。连接因此必须在同一个测试内复用同一个实例（惰性创建，见
    test_session_routes.py 里同样的闭包写法），否则登录写进去的账号下一个
    请求就查不到了。

    account 只播两个 member：member-t1 绑租户 t1、member-t2 绑租户 t2。
    以哪个租户的身份请求，就登录哪个账号——不再靠请求体里的 tenant_id。
    """
    state: dict[str, aiosqlite.Connection] = {}

    async def _get() -> aiosqlite.Connection:
        if "conn" not in state:
            conn = await aiosqlite.connect(":memory:")
            await ensure_terms_schema(conn)
            # Task 3：qa_endpoint 现在经 list_terms_merged() 读术语表，测试连接要
            # 把 term_edits 表也建好，否则会报 "no such table: term_edits"。
            await ensure_term_edits_schema(conn)
            await ensure_admin_users_schema(conn)
            for username, tenant_id in (("member-t1", "t1"), ("member-t2", "t2")):
                await create_admin_user(
                    conn,
                    username=username,
                    password="password1",
                    role="member",
                    tenant_id=tenant_id,
                )
            state["conn"] = conn
        return state["conn"]

    return _get




def test_qa_endpoint_returns_answer_and_used_sources():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider()
    )

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, deps.DEFAULT_LLM_PROVIDER_NAME, FakeLLMProvider()
    )

    import asyncio

    vector_store = asyncio.run(_fake_vector_store())

    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    app.dependency_overrides[deps.get_bm25_index] = _fake_bm25_index
    app.dependency_overrides[deps.get_rerank_provider] = lambda: None
    app.dependency_overrides[deps.get_review_conn] = _review_conn_override()
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    # gateway_shared_secret 显式钉死为 None：不 override 的话 get_settings
    # 会读真实环境变量/.env，一旦开发者本机或 .env 配置了
    # CUSTOMER_RAG_GATEWAY_SHARED_SECRET（正是这个安全修复要促使运营者去做
    # 的事），这条与网关鉴权无关的测试会意外因缺少网关凭证被 401 拒绝。
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    try:
        client = login_client("member-t1")
        response = client.post(
            "/qa", json={"question": "网络连不上怎么办？", "tenant_id": "t1"}
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "按资料所述，重启路由器即可解决。"
    assert body["used_sources"] == ["faq/network.md"]


def test_qa_endpoint_does_not_leak_another_tenants_sources():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider()
    )

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, deps.DEFAULT_LLM_PROVIDER_NAME, FakeLLMProvider()
    )

    import asyncio

    vector_store = asyncio.run(_fake_vector_store())

    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    app.dependency_overrides[deps.get_bm25_index] = _fake_bm25_index
    app.dependency_overrides[deps.get_rerank_provider] = lambda: None
    app.dependency_overrides[deps.get_review_conn] = _review_conn_override()
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    # gateway_shared_secret 显式钉死为 None，理由同上一条测试。
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    try:
        client = login_client("member-t2")
        response = client.post(
            "/qa", json={"question": "网络连不上怎么办？", "tenant_id": "t2"}
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["used_sources"] == []


def test_qa_endpoint_uses_session_tenant_over_request_body_and_gateway_header():
    """租户只认会话。请求体里的 tenant_id 和网关头里的 X-Tenant-Id 都被忽略。

    这条以前叫 uses_gateway_tenant_id_over_request_body，钉的是"网关头压过
    请求体"。身份改从会话取之后优先级变成了会话 > 网关头 > 请求体，所以这里
    把两个"错的租户"同时摆上：会话是 t1，只有 t1 的资料能被检索到。
    """
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider()
    )
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, deps.DEFAULT_LLM_PROVIDER_NAME, FakeLLMProvider()
    )

    import asyncio

    vector_store = asyncio.run(_fake_vector_store())

    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    app.dependency_overrides[deps.get_bm25_index] = _fake_bm25_index
    app.dependency_overrides[deps.get_rerank_provider] = lambda: None
    app.dependency_overrides[deps.get_review_conn] = _review_conn_override()
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    app.dependency_overrides[deps.get_settings] = lambda: _settings(
        gateway_shared_secret="sekret"
    )
    try:
        client = login_client("member-t1")
        response = client.post(
            "/qa",
            json={"question": "网络连不上怎么办？", "tenant_id": "wrong-tenant"},
            headers={"X-Tenant-Id": "another-tenant", "X-Gateway-Secret": "sekret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["used_sources"] == ["faq/network.md"]


def test_qa_endpoint_rejects_wrong_gateway_secret_when_configured():
    """配了 gateway_shared_secret 就必须带有效的网关凭证——哪怕已经登录。

    先登录再请求是这条测试的要害：不登录的话 401 也可能来自会话校验，
    那样这条用例就不再证明网关那道门还在。
    """
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider()
    )
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, deps.DEFAULT_LLM_PROVIDER_NAME, FakeLLMProvider()
    )

    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_bm25_index] = lambda: BM25Index()
    app.dependency_overrides[deps.get_rerank_provider] = lambda: None
    app.dependency_overrides[deps.get_review_conn] = _review_conn_override()
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    app.dependency_overrides[deps.get_settings] = lambda: _settings(
        gateway_shared_secret="sekret"
    )
    try:
        client = login_client("member-t1")
        response = client.post(
            "/qa",
            json={"question": "网络连不上怎么办？", "tenant_id": "t1"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
