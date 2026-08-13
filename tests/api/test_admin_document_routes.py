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
        self,
        *,
        subject_standard_name,
        object_standard_name,
        relation_type,
        source,
        tenant_id,
        provenance,
        recorded_at,
    ) -> None:
        self.written.append(
            {
                "subject": subject_standard_name,
                "object": object_standard_name,
                "relation_type": relation_type,
                "tenant_id": tenant_id,
                "provenance": provenance,
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


def test_list_documents_includes_dead_jobs(ingestion_conn):
    from app.ingestion.ingestion_queue import enqueue_ingestion_job, mark_job_failed

    job_id = asyncio.run(
        enqueue_ingestion_job(
            ingestion_conn, tenant_id="t1", file_path="a.md",
            content_hash="h1", action="ingest",
        )
    )
    asyncio.run(mark_job_failed(ingestion_conn, job_id, error="解析失败", max_attempts=1))

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/documents", params={"tenant_id": "t1"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert len(body["dead_jobs"]) == 1
    assert body["dead_jobs"][0]["job_id"] == job_id
    assert body["dead_jobs"][0]["last_error"] == "解析失败"


def test_retry_job_resets_to_pending_and_returns_200(tmp_path, ingestion_conn):
    from app.ingestion.ingestion_queue import (
        enqueue_ingestion_job,
        list_pending_jobs,
        mark_job_failed,
    )

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    job_id = asyncio.run(
        enqueue_ingestion_job(
            ingestion_conn, tenant_id="t1", file_path="a.md",
            content_hash="h1", action="ingest",
        )
    )
    asyncio.run(mark_job_failed(ingestion_conn, job_id, error="解析失败", max_attempts=1))

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    app.dependency_overrides[deps.get_embedding_registry] = lambda: EmbeddingRegistry()
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_llm_registry] = lambda: ProviderRegistry()
    app.dependency_overrides[deps.get_terms] = lambda: []
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    app.dependency_overrides[deps.get_review_conn] = lambda: None
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/admin/documents/jobs/{job_id}/retry",
            params={"tenant_id": "t1"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    # a.md 实际不存在磁盘上，所以下面两个断言一起证明了两件事：(1) retry_job()
    # 真的把 dead 重置回了 pending（不是仍然 dead 或者被删掉了），(2)
    # background_tasks.add_task(_run_pending_jobs, ...) 真的被调用并且
    # TestClient 同步跑完了它——如果这次重试处理从没被触发，attempts 会
    # 停在 retry_job() 刚重置时的 0，而不是变成 1（一次失败的处理尝试）。
    # 重试耗尽变 dead 的行为本身已经在 tests/ingestion/test_ingestion_queue.py
    # 里覆盖过，这里不重复断言那一层。
    pending = asyncio.run(list_pending_jobs(ingestion_conn, tenant_id="t1"))
    assert [j["job_id"] for j in pending] == [job_id]
    assert pending[0]["attempts"] == 1


def test_retry_job_returns_404_for_unknown_job(ingestion_conn):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_embedding_registry] = lambda: EmbeddingRegistry()
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_llm_registry] = lambda: ProviderRegistry()
    app.dependency_overrides[deps.get_terms] = lambda: []
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    app.dependency_overrides[deps.get_review_conn] = lambda: None
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/documents/jobs/unknown-id/retry",
            params={"tenant_id": "t1"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_retry_job_returns_409_when_job_is_not_dead(ingestion_conn):
    from app.ingestion.ingestion_queue import enqueue_ingestion_job

    job_id = asyncio.run(
        enqueue_ingestion_job(
            ingestion_conn, tenant_id="t1", file_path="a.md",
            content_hash="h1", action="ingest",
        )
    )

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_embedding_registry] = lambda: EmbeddingRegistry()
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_llm_registry] = lambda: ProviderRegistry()
    app.dependency_overrides[deps.get_terms] = lambda: []
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    app.dependency_overrides[deps.get_review_conn] = lambda: None
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/admin/documents/jobs/{job_id}/retry",
            params={"tenant_id": "t1"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409


def test_delete_job_removes_it_and_unlinks_orphaned_file(tmp_path, ingestion_conn):
    from app.ingestion.ingestion_queue import enqueue_ingestion_job, mark_job_failed

    upload_dir = tmp_path / "uploads"
    tenant_dir = upload_dir / "t1"
    tenant_dir.mkdir(parents=True)
    orphaned = tenant_dir / "abc_a.md"
    orphaned.write_text("内容", encoding="utf-8")
    job_id = asyncio.run(
        enqueue_ingestion_job(
            ingestion_conn, tenant_id="t1", file_path=str(orphaned),
            content_hash="h1", action="ingest",
        )
    )
    asyncio.run(mark_job_failed(ingestion_conn, job_id, error="解析失败", max_attempts=1))

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            f"/api/admin/documents/jobs/{job_id}",
            params={"tenant_id": "t1"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert not orphaned.exists()


def test_delete_job_removes_orphaned_vector_chunks(tmp_path, ingestion_conn):
    from app.ingestion.ingestion_queue import enqueue_ingestion_job, mark_job_failed

    upload_dir = tmp_path / "uploads"
    tenant_dir = upload_dir / "t1"
    tenant_dir.mkdir(parents=True)
    orphaned = tenant_dir / "abc_a.md"
    orphaned.write_text("内容", encoding="utf-8")
    job_id = asyncio.run(
        enqueue_ingestion_job(
            ingestion_conn, tenant_id="t1", file_path=str(orphaned),
            content_hash="h1", action="ingest",
        )
    )
    asyncio.run(mark_job_failed(ingestion_conn, job_id, error="解析失败", max_attempts=1))

    # 模拟一个在"部分 chunk 已经写进向量库"之后才失败的任务——record_ingested
    # 从没跑过（所以不在已摄取文档列表里），但这些 chunk 已经真实存在于
    # 向量库中，删除失败任务时必须一并清掉。
    vector_store = InMemoryVectorStore()
    asyncio.run(
        vector_store.upsert(
            [
                VectorRecord(
                    id=f"{orphaned}#0", vector=[0.1, 0.2], text="部分写入的内容",
                    tenant_id="t1", metadata={"source": str(orphaned)},
                )
            ]
        )
    )

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            f"/api/admin/documents/jobs/{job_id}",
            params={"tenant_id": "t1"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    remaining = asyncio.run(
        vector_store.search(query_vector=[0.1, 0.2], top_k=10, tenant_id="t1")
    )
    assert remaining == []


def test_delete_job_returns_409_when_job_is_not_dead(ingestion_conn):
    from app.ingestion.ingestion_queue import enqueue_ingestion_job

    job_id = asyncio.run(
        enqueue_ingestion_job(
            ingestion_conn, tenant_id="t1", file_path="a.md",
            content_hash="h1", action="ingest",
        )
    )

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_upload_dir] = lambda: None
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            f"/api/admin/documents/jobs/{job_id}",
            params={"tenant_id": "t1"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409


def test_delete_document_does_not_unlink_file_under_a_different_tenant_directory(
    tmp_path, ingestion_conn
):
    """跨租户越权删除的回归测试：file_path 指向 t2 的子目录，但请求用
    tenant_id=t1——向量库/追踪表两处因为 tenant_id 不匹配会是空操作，
    磁盘文件这一步在修复前不会做同样的租户校验，直接被删掉；修复后
    应该被拦下来，文件保持原样。
    """
    upload_dir = tmp_path / "uploads"
    t2_dir = upload_dir / "t2"
    t2_dir.mkdir(parents=True)
    other_tenants_file = t2_dir / "abc_secret.md"
    other_tenants_file.write_text("t2 的私有内容", encoding="utf-8")

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
            params={"tenant_id": "t1", "file_path": str(other_tenants_file)},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert other_tenants_file.exists()


def test_list_document_chunks_returns_texts_and_total(ingestion_conn):
    vector_store = InMemoryVectorStore()
    asyncio.run(
        vector_store.upsert(
            [
                VectorRecord(
                    id="a.md#0", vector=[0.1], text="第一段",
                    tenant_id="t1", metadata={"source": "a.md"},
                ),
                VectorRecord(
                    id="a.md#1", vector=[0.1], text="第二段",
                    tenant_id="t1", metadata={"source": "a.md"},
                ),
            ]
        )
    )

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/documents/chunks",
            params={"tenant_id": "t1", "file_path": "a.md"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [c["text"] for c in body["chunks"]] == ["第一段", "第二段"]


def test_list_document_chunks_caps_at_200_but_reports_true_total(ingestion_conn):
    vector_store = InMemoryVectorStore()
    asyncio.run(
        vector_store.upsert(
            [
                VectorRecord(
                    id=f"a.md#{i}", vector=[0.1], text=f"第{i}段",
                    tenant_id="t1", metadata={"source": "a.md"},
                )
                for i in range(250)
            ]
        )
    )

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/documents/chunks",
            params={"tenant_id": "t1", "file_path": "a.md"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    body = response.json()
    assert len(body["chunks"]) == 200
    assert body["total"] == 250


def test_download_document_file_returns_file_content(tmp_path, ingestion_conn):
    upload_dir = tmp_path / "uploads"
    tenant_dir = upload_dir / "t1"
    tenant_dir.mkdir(parents=True)
    the_file = tenant_dir / "abc_a.md"
    the_file.write_text("文件内容", encoding="utf-8")

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/documents/file",
            params={"tenant_id": "t1", "file_path": str(the_file)},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.content.decode("utf-8") == "文件内容"


def test_download_document_file_returns_404_for_file_outside_own_tenant_directory(
    tmp_path, ingestion_conn
):
    upload_dir = tmp_path / "uploads"
    t2_dir = upload_dir / "t2"
    t2_dir.mkdir(parents=True)
    other_tenants_file = t2_dir / "abc_secret.md"
    other_tenants_file.write_text("t2 的私有内容", encoding="utf-8")

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/documents/file",
            params={"tenant_id": "t1", "file_path": str(other_tenants_file)},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_get_terms_queries_the_review_conn_terms_table():
    """回归测试：get_terms() 迁移前是进程级 YAML 缓存，迁移后必须真的从
    传入的连接查 terms 表——这里往一个全新连接插入一条术语，直接调用
    get_terms() 本身（不经过任何路由），验证它查到了这条数据（如果
    get_terms 还在读旧缓存/YAML，这条术语不会出现）。
    """
    from app.graphrag.terms_store import create_term, ensure_terms_schema

    async def _open_conn() -> aiosqlite.Connection:
        # aiosqlite.connect() 返回的是一个自带 __await__ 的 Connection 代理对象，
        # 不是真正的 coroutine，不能直接传给 asyncio.run()（Python 3.12 严格要求
        # coroutine），所以这里包一层跟本文件其它地方（_open_review_conn 等）
        # 一致的小 async helper。
        return await aiosqlite.connect(":memory:")

    review_conn = asyncio.run(_open_conn())
    try:
        asyncio.run(ensure_terms_schema(review_conn))
        asyncio.run(
            create_term(
                review_conn, standard_name="集成测试术语", aliases=[],
                term_type="t", product_line="p",
            )
        )

        resolved_terms = asyncio.run(deps.get_terms(review_conn=review_conn))
    finally:
        asyncio.run(review_conn.close())

    assert [t.standard_name for t in resolved_terms] == ["集成测试术语"]
