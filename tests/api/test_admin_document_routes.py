import asyncio
import io

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.config.settings import Settings
from app.ingestion.ingestion_queue import ensure_ingestion_queue_schema
from app.ingestion.tracking import ensure_tracking_schema, record_ingested
from app.main import app
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[0.1, 0.2] for _ in request.texts])


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


def _authed_headers(session_store: AdminSessionStore) -> dict[str, str]:
    token = session_store.create_session()
    return {"Authorization": f"Bearer {token}"}


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
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    upload_dir = tmp_path / "uploads"
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
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
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider())
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    upload_dir = tmp_path / "uploads"
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
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


def _upload_overrides(session_store, ingestion_conn, upload_dir) -> None:
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider())
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir


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


def test_delete_document_removes_tracking_and_vectors(ingestion_conn):
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
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
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


async def _tracked_paths(conn: aiosqlite.Connection, tenant_id: str) -> list[str]:
    from app.ingestion.tracking import list_tracked_files

    rows = await list_tracked_files(conn, tenant_id=tenant_id)
    return [row["file_path"] for row in rows]
