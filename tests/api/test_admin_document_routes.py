import asyncio
import io

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.config.settings import Settings
from app.graphrag.ontology import Term
from app.graphrag.review_queue import ensure_review_schema
from app.ingestion.ingestion_queue import ensure_ingestion_queue_schema
from app.ingestion.tracking import ensure_tracking_schema, record_ingested
from app.main import app
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[0.1, 0.2] for _ in request.texts])


class FixedLLMProvider:
    """图谱抽取用的假 LLM：不管输入是什么都返回同一段候选关系 JSON。"""

    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._text)


class SpyGraphClient:
    def __init__(self) -> None:
        self.written: list[dict] = []
        self.deleted_sources: list[tuple[str, str]] = []

    async def merge_relation(
        self, *, subject_standard_name, object_standard_name, relation_type, source, tenant_id
    ) -> None:
        self.written.append(
            {
                "subject": subject_standard_name,
                "object": object_standard_name,
                "relation_type": relation_type,
                "tenant_id": tenant_id,
            }
        )

    async def delete_relations_by_source(self, source: str, *, tenant_id: str) -> None:
        self.deleted_sources.append((source, tenant_id))


_TERMS = [
    Term(
        standard_name="示例错误码E502",
        aliases=["网关超时示例"],
        term_type="error_code",
        product_line="示例产品线",
    ),
    Term(
        standard_name="示例登录模块",
        aliases=["示例认证模块"],
        term_type="module",
        product_line="示例产品线",
    ),
]

_RESOLVABLE_RELATION_JSON = (
    '{"relations": [{"subject": "网关超时示例", '
    '"object": "示例认证模块", "relation_type": "RELATED_TO"}]}'
)


def _llm_registry_returning(text: str) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(
        ProviderCapability.LLM, deps.DEFAULT_LLM_PROVIDER_NAME, FixedLLMProvider(text)
    )
    return registry


def _settings(**overrides) -> Settings:
    defaults = dict(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=2,
        admin_token="tok",
    )
    defaults.update(overrides)
    return Settings(**defaults)


async def _open_ingestion_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_tracking_schema(conn)
    await ensure_ingestion_queue_schema(conn)
    return conn


@pytest.fixture
def ingestion_conn():
    """摄取库连接。必须显式 close：aiosqlite 的后台工作线程不是 daemon
    线程，泄漏一个未关闭的连接会让 pytest 进程在跑完全部用例后卡在解释器
    退出阶段（threading._shutdown 等这个线程），表现为"测试全绿但命令不返回"。
    """
    conn = asyncio.run(_open_ingestion_conn())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


async def _open_review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(conn)
    return conn


@pytest.fixture
def review_conn():
    """图谱人工审核队列连接。close 的理由同 ingestion_conn。"""
    conn = asyncio.run(_open_review_conn())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


def _authed_headers(session_store: AdminSessionStore) -> dict[str, str]:
    token = session_store.create_session()
    return {"Authorization": f"Bearer {token}"}


def _upload_overrides(
    session_store,
    ingestion_conn,
    upload_dir,
    *,
    vector_store=None,
    llm_registry=None,
    graph_client=None,
    review_conn=None,
    terms=None,
) -> None:
    """上传接口依赖的全部 provider 覆盖。

    图谱那四项（llm_registry/terms/graph_client/review_conn）现在是上传
    路由的无条件依赖（build_graph 是逐任务判断的，资源必须先备好），
    不覆盖的话测试会去真建 Neo4j driver、真开仓库里的 SQLite 文件。
    """
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider())
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store or InMemoryVectorStore()
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    app.dependency_overrides[deps.get_llm_registry] = lambda: (
        llm_registry or _llm_registry_returning('{"relations": []}')
    )
    app.dependency_overrides[deps.get_terms] = lambda: (_TERMS if terms is None else terms)
    app.dependency_overrides[deps.get_graph_client] = lambda: (
        graph_client if graph_client is not None else SpyGraphClient()
    )
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn


def test_upload_without_session_returns_401():
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/documents",
            files={"file": ("a.md", b"## t\ncontent", "text/markdown")},
            data={"tenant_id": "t1", "build_graph": "false"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_upload_rejects_file_larger_than_100mb(tmp_path, ingestion_conn):
    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    _upload_overrides(session_store, ingestion_conn, upload_dir)
    try:
        client = TestClient(app)
        oversized = io.BytesIO(b"0" * (101 * 1024 * 1024))
        response = client.post(
            "/api/admin/documents",
            files={"file": ("big.md", oversized, "text/markdown")},
            data={"tenant_id": "t1", "build_graph": "false"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 413


def test_upload_enqueues_job_and_returns_job_id(tmp_path, ingestion_conn):
    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    _upload_overrides(session_store, ingestion_conn, upload_dir)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/documents",
            files={"file": ("a.md", b"## t\ncontent", "text/markdown")},
            data={"tenant_id": "t1", "build_graph": "false"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "job_id" in response.json()
    # 落盘路径是 <upload_dir>/<tenant_id>/<uuid>_<原文件名>，所以要递归 glob。
    assert (upload_dir / "t1").is_dir()
    assert len(list(upload_dir.rglob("*a.md"))) == 1


def test_upload_sanitizes_traversal_in_filename(tmp_path, ingestion_conn):
    """文件名里的 ../ 不能让文件落到 upload_dir 之外。"""
    session_store = AdminSessionStore()
    root = tmp_path / "root"
    upload_dir = root / "uploads"
    upload_dir.mkdir(parents=True)
    _upload_overrides(session_store, ingestion_conn, upload_dir)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/documents",
            files={"file": ("../../pwned.md", b"## t\ncontent", "text/markdown")},
            data={"tenant_id": "t1", "build_graph": "false"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    # 文件确实落在了消毒后的租户目录里，而不是"没报错"而已。
    landed = list((upload_dir / "t1").iterdir())
    assert len(landed) == 1
    assert landed[0].name.endswith(".._.._pwned.md")
    # upload_dir 之外（它的父目录）没有多出任何东西。
    assert [p.name for p in root.iterdir()] == ["uploads"]


def test_upload_rejects_tenant_id_with_path_separators(tmp_path, ingestion_conn):
    """tenant_id 里的路径分隔符要被 400 拒掉，且不能创建任何目录。"""
    session_store = AdminSessionStore()
    root = tmp_path / "root"
    upload_dir = root / "uploads"
    upload_dir.mkdir(parents=True)
    _upload_overrides(session_store, ingestion_conn, upload_dir)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/documents",
            files={"file": ("a.md", b"## t\ncontent", "text/markdown")},
            data={"tenant_id": "../../pwned", "build_graph": "false"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert list(upload_dir.iterdir()) == []
    assert [p.name for p in root.iterdir()] == ["uploads"]


def test_upload_rejects_dot_only_tenant_id(tmp_path, ingestion_conn):
    """纯点的 tenant_id（"." / ".."）也不合法——它会指向 upload_dir 自身或父目录。"""
    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(parents=True)
    _upload_overrides(session_store, ingestion_conn, upload_dir)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/documents",
            files={"file": ("a.md", b"## t\ncontent", "text/markdown")},
            data={"tenant_id": "..", "build_graph": "false"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert list(upload_dir.iterdir()) == []


def test_list_documents_returns_tracked_files_for_tenant(ingestion_conn):
    asyncio.run(
        record_ingested(
            ingestion_conn, tenant_id="t1", file_path="a.md", content_hash="h1", chunk_count=3
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/documents",
            params={"tenant_id": "t1"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["documents"][0]["file_path"] == "a.md"
    assert body["documents"][0]["chunk_count"] == 3


def test_list_documents_excludes_other_tenants_pending_jobs(ingestion_conn):
    from app.ingestion.ingestion_queue import enqueue_ingestion_job

    async def _seed() -> None:
        await enqueue_ingestion_job(
            ingestion_conn, tenant_id="t1", file_path="mine.md",
            content_hash="h1", action="ingest",
        )
        await enqueue_ingestion_job(
            ingestion_conn, tenant_id="t2", file_path="theirs.md",
            content_hash="h2", action="ingest",
        )

    asyncio.run(_seed())
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/documents",
            params={"tenant_id": "t1"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    pending = response.json()["pending_jobs"]
    assert [job["file_path"] for job in pending] == ["mine.md"]


def test_delete_document_removes_tracking_and_vectors(tmp_path, ingestion_conn):
    asyncio.run(
        record_ingested(
            ingestion_conn, tenant_id="t1", file_path="a.md", content_hash="h1", chunk_count=1
        )
    )
    vector_store = InMemoryVectorStore()
    asyncio.run(
        vector_store.upsert(
            [
                VectorRecord(
                    id="a.md#0", vector=[0.1, 0.2], text="内容",
                    tenant_id="t1", metadata={"source": "a.md"},
                )
            ]
        )
    )

    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            "/api/admin/documents",
            params={"tenant_id": "t1", "file_path": "a.md"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    remaining = asyncio.run(
        vector_store.search(query_vector=[0.1, 0.2], top_k=10, tenant_id="t1")
    )
    assert remaining == []
    assert asyncio.run(_tracked_paths(ingestion_conn, "t1")) == []


def test_delete_document_also_unlinks_uploaded_file(tmp_path, ingestion_conn):
    """删除文档要把 data/uploads 下的原始文件也删掉，不能只清索引。"""
    upload_dir = tmp_path / "uploads"
    tenant_dir = upload_dir / "t1"
    tenant_dir.mkdir(parents=True)
    uploaded = tenant_dir / "abc_a.md"
    uploaded.write_text("# t\n内容", encoding="utf-8")
    asyncio.run(
        record_ingested(
            ingestion_conn, tenant_id="t1", file_path=str(uploaded),
            content_hash="h1", chunk_count=1,
        )
    )

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            "/api/admin/documents",
            params={"tenant_id": "t1", "file_path": str(uploaded)},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert not uploaded.exists()


def test_delete_document_returns_502_with_clear_message_when_vector_store_fails(
    tmp_path, ingestion_conn
):
    """向量库删除失败时要返回带明确信息的 502，而不是裸 500。"""
    asyncio.run(
        record_ingested(
            ingestion_conn, tenant_id="t1", file_path="a.md", content_hash="h1", chunk_count=1
        )
    )

    class FailingVectorStore(InMemoryVectorStore):
        async def delete_by_source(self, *, source: str, tenant_id: str) -> None:
            raise RuntimeError("milvus 连接失败")

    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_vector_store] = lambda: FailingVectorStore()
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            "/api/admin/documents",
            params={"tenant_id": "t1", "file_path": "a.md"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert "删除向量数据失败" in response.json()["detail"]
    # 向量库那一步失败，追踪记录不应该被清理，避免"向量还在但追踪记录没了"
    assert asyncio.run(_tracked_paths(ingestion_conn, "t1")) == ["a.md"]


def test_delete_document_returns_502_when_tracking_cleanup_fails_after_vector_delete(
    tmp_path, ingestion_conn, monkeypatch
):
    """追踪记录清理失败时也要给出明确的 502，并提示已产生的不一致状态。"""
    asyncio.run(
        record_ingested(
            ingestion_conn, tenant_id="t1", file_path="a.md", content_hash="h1", chunk_count=1
        )
    )

    async def _boom(*args, **kwargs):
        raise RuntimeError("数据库锁住了")

    import app.api.admin_document_routes as routes_module

    monkeypatch.setattr(routes_module, "remove_tracked_file", _boom)

    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            "/api/admin/documents",
            params={"tenant_id": "t1", "file_path": "a.md"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert "可能需要手动核实" in response.json()["detail"]


def test_delete_document_keeps_files_outside_upload_dir(tmp_path, ingestion_conn):
    """CLI 摄取的原始语料不在 upload_dir 里，后台删除只清索引，不能删用户的文件。"""
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    outside = tmp_path / "corpus" / "a.md"
    outside.parent.mkdir()
    outside.write_text("# t\n内容", encoding="utf-8")
    asyncio.run(
        record_ingested(
            ingestion_conn, tenant_id="t1", file_path=str(outside),
            content_hash="h1", chunk_count=1,
        )
    )

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            "/api/admin/documents",
            params={"tenant_id": "t1", "file_path": str(outside)},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert outside.exists()


def test_upload_rejects_unsupported_file_type(tmp_path, ingestion_conn):
    """摄取管线不支持的扩展名要同步 400，不落盘、不入队。"""
    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    _upload_overrides(session_store, ingestion_conn, upload_dir)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/documents",
            files={"file": ("payload.exe", b"MZ\x00\x00", "application/octet-stream")},
            data={"tenant_id": "t1", "build_graph": "false"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert list(upload_dir.iterdir()) == []
    assert asyncio.run(_pending_job_paths(ingestion_conn)) == []


def test_upload_rejects_tenant_id_outside_milvus_charset(tmp_path, ingestion_conn):
    """能通过旧的 Unicode 宽松校验、但过不了 Milvus 严格白名单的 tenant_id
    必须在入口就 400——否则请求拿到 200 + job_id、文件已落盘，然后在后台
    任务/DELETE 里才炸。
    """
    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    _upload_overrides(session_store, ingestion_conn, upload_dir)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/documents",
            files={"file": ("a.md", b"## t\ncontent", "text/markdown")},
            data={"tenant_id": "租户.一", "build_graph": "false"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert list(upload_dir.iterdir()) == []
    assert asyncio.run(_pending_job_paths(ingestion_conn)) == []


def test_upload_with_build_graph_true_runs_graph_extraction(
    tmp_path, ingestion_conn, review_conn
):
    """build_graph=true 的正面路径：上传接口必须把图谱资源一路传到
    process_pending_jobs()，后台任务真的走到 LLM 抽取 + 写图谱那一步。
    """
    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    graph_client = SpyGraphClient()
    _upload_overrides(
        session_store,
        ingestion_conn,
        upload_dir,
        llm_registry=_llm_registry_returning(_RESOLVABLE_RELATION_JSON),
        graph_client=graph_client,
        review_conn=review_conn,
    )
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/documents",
            files={
                "file": (
                    "a.md",
                    "# 标题\n网关超时示例通常与示例认证模块相关".encode("utf-8"),
                    "text/markdown",
                )
            },
            data={"tenant_id": "t1", "build_graph": "true"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    # BackgroundTasks 在 TestClient 返回之前就跑完了，所以这里能直接断言结果。
    assert graph_client.deleted_sources, "图谱抽取根本没被触发"
    assert [
        (item["subject"], item["object"], item["relation_type"], item["tenant_id"])
        for item in graph_client.written
    ] == [("示例错误码E502", "示例登录模块", "RELATED_TO", "t1")]


def test_upload_with_build_graph_false_skips_graph_extraction(
    tmp_path, ingestion_conn, review_conn
):
    """反面对照：图谱资源同样传了，但这条任务没勾建图，就不该碰图谱。"""
    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    graph_client = SpyGraphClient()
    _upload_overrides(
        session_store,
        ingestion_conn,
        upload_dir,
        llm_registry=_llm_registry_returning(_RESOLVABLE_RELATION_JSON),
        graph_client=graph_client,
        review_conn=review_conn,
    )
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/documents",
            files={
                "file": (
                    "a.md",
                    "# 标题\n网关超时示例通常与示例认证模块相关".encode("utf-8"),
                    "text/markdown",
                )
            },
            data={"tenant_id": "t1", "build_graph": "false"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert graph_client.written == []
    assert graph_client.deleted_sources == []


async def _pending_job_paths(conn: aiosqlite.Connection) -> list[str]:
    from app.ingestion.ingestion_queue import list_pending_jobs

    return [job["file_path"] for job in await list_pending_jobs(conn, limit=50)]


async def _tracked_paths(conn: aiosqlite.Connection, tenant_id: str) -> list[str]:
    from app.ingestion.tracking import list_tracked_files

    rows = await list_tracked_files(conn, tenant_id=tenant_id)
    return [row["file_path"] for row in rows]
